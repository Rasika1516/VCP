"""Fleet-level summary and bulk command endpoints.

Routes
------
GET  /fleet/summary          — aggregate counts across all workers
POST /fleet/commands         — broadcast or group-scoped bulk command
GET  /fleet/workers          — list known workers + reachability
"""
from __future__ import annotations

import asyncio
import uuid

import httpx
from fastapi import APIRouter, Depends

from vcp_control_plane.api.auth import make_auth_dependency
from vcp_control_plane.api.deps import DbSession, RedisClient, SettingsDep
from vcp_control_plane.api.schemas import (
    BulkCommandIn,
    BulkCommandOut,
    FleetSummaryOut,
    ShardSummaryOut,
)
from vcp_control_plane.bus.publisher import CommandPublisher
from vcp_control_plane.registry.store import VcpRegistryStore

router = APIRouter()
_auth = make_auth_dependency()


async def _query_worker(
    base_url: str, token: str | None
) -> dict | None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(headers=headers, timeout=5.0) as client:
            resp = await client.get(f"{base_url}/fleet/summary")
            # RNZ (2026-10-09): Added fallback for vcp_data_plane workers. They do not have
            # /fleet/summary (404), so read /status instead and map its fields
            # (vcp_total, state_distribution) to the shape used below. Without this the
            # worker always showed reachable=false with 0 VCPs in the fleet summary.
            if resp.status_code == 404:
                resp = await client.get(f"{base_url}/status")
                resp.raise_for_status()
                status = resp.json()
                return {
                    "vcpCount": status.get("vcp_total", 0),
                    "connectorStateCounts": status.get("state_distribution", {}),
                }
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return None


@router.get("/fleet/summary", response_model=FleetSummaryOut, dependencies=[Depends(_auth)])
async def fleet_summary(db: DbSession, settings: SettingsDep) -> FleetSummaryOut:
    store = VcpRegistryStore(db)
    # Aggregate from DB
    counts = await store.aggregate_state_counts()

    # Query workers for live shard data
    worker_results = await asyncio.gather(
        *[_query_worker(url, settings.worker_token) for url in settings.worker_urls]
    )
    shards = []
    for i, (url, result) in enumerate(zip(settings.worker_urls, worker_results)):
        shards.append(
            ShardSummaryOut.model_validate(
                {
                    "shardIndex": i,
                    "workerId": url,
                    "reachable": result is not None,
                    "vcpCount": result.get("vcpCount", 0) if result else 0,
                    "connectorStateCounts": result.get("connectorStateCounts", {})
                    if result
                    else {},
                }
            )
        )

    return FleetSummaryOut.model_validate(
        {
            "vcpCount": counts["total"],
            "connectionStateCounts": counts["connection_states"],
            "actualStateCounts": counts["actual_states"],
            "workerCount": len(settings.worker_urls),
            "shards": [s.model_dump(by_alias=True) for s in shards],
        }
    )


@router.post("/fleet/commands", response_model=BulkCommandOut, dependencies=[Depends(_auth)])
async def bulk_command(
    body: BulkCommandIn,
    db: DbSession,
    redis: RedisClient,
    settings: SettingsDep,
) -> BulkCommandOut:
    """Dispatch a command to a group, a list of VCPs, or broadcast to all."""
    store = VcpRegistryStore(db)
    publisher = CommandPublisher(redis_client=redis, settings=settings)

    command_ids: list[str] = []

    if body.vcp_ids:
        # Targeted list
        for vcp_id in body.vcp_ids:
            row = await store.get_vcp(vcp_id)
            cid = str(uuid.uuid4())
            await publisher.send_to_vcp(
                vcp_id=vcp_id,
                worker_id=row.worker_id if row else None,
                command_type=body.command_type,
                payload=body.payload,
                command_id=cid,
                issued_by=body.issued_by,
            )
            await store.record_command(
                command_id=cid, command_type=body.command_type,
                vcp_id=vcp_id, payload=body.payload, issued_by=body.issued_by,
            )
            command_ids.append(cid)
    elif body.group_id:
        # Group scoped
        cid = str(uuid.uuid4())
        await publisher.broadcast(
            command_type=body.command_type,
            payload={**body.payload, "group_id": body.group_id},
            command_id=cid,
            issued_by=body.issued_by,
            group_id=body.group_id,
        )
        await store.record_command(
            command_id=cid, command_type=body.command_type,
            vcp_id=None, payload=body.payload, issued_by=body.issued_by,
            group_id=body.group_id,
        )
        command_ids.append(cid)
    else:
        # Fleet-wide broadcast
        cid = str(uuid.uuid4())
        await publisher.broadcast(
            command_type=body.command_type,
            payload=body.payload,
            command_id=cid,
            issued_by=body.issued_by,
        )
        await store.record_command(
            command_id=cid, command_type=body.command_type,
            vcp_id=None, payload=body.payload, issued_by=body.issued_by,
        )
        command_ids.append(cid)

    return BulkCommandOut.model_validate(
        {"dispatched": len(command_ids), "commandIds": command_ids}
    )


@router.get("/fleet/workers", dependencies=[Depends(_auth)])
async def list_workers(settings: SettingsDep) -> list[dict]:
    """Return reachability status for each configured worker URL."""
    results = await asyncio.gather(
        *[_query_worker(url, settings.worker_token) for url in settings.worker_urls]
    )
    return [
        {
            "url": url,
            "reachable": result is not None,
            "vcpCount": result.get("vcpCount", 0) if result else 0,
        }
        for url, result in zip(settings.worker_urls, results)
    ]
