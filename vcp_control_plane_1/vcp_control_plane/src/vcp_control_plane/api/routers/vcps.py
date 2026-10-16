"""VCP instance CRUD and per-VCP command endpoints.

Routes
------
GET    /vcps                  — paginated VCP list
POST   /vcps                  — register a new VCP
GET    /vcps/{vcp_id}         — single VCP detail
PATCH  /vcps/{vcp_id}         — update metadata / desired_state
DELETE /vcps/{vcp_id}         — deregister VCP
POST   /vcps/{vcp_id}/commands — dispatch a command to the owning worker
GET    /vcps/{vcp_id}/commands — audit log for this VCP
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from vcp_control_plane.api.auth import make_auth_dependency
from vcp_control_plane.api.deps import DbSession, RedisClient, SettingsDep
from vcp_control_plane.api.schemas import (
    CommandAuditOut,
    CommandOut,
    OkOut,
    VcpCommandIn,
    VcpCreateIn,
    VcpListOut,
    VcpOut,
    VcpPatchIn,
)
from vcp_control_plane.bus.publisher import CommandPublisher
from vcp_control_plane.registry.store import VcpRegistryStore

router = APIRouter()
_auth = make_auth_dependency()


def _to_vcp_out(row) -> VcpOut:
    return VcpOut.model_validate(
        {
            "vcpId": row.vcp_id,
            "chargePointId": row.charge_point_id,
            "profileId": row.profile_id,
            "profileVersion": row.profile_version,
            "ocppVersion": row.ocpp_version,
            "workerId": row.worker_id,
            "groupId": row.group_id,
            "desiredState": row.desired_state,
            "actualState": row.actual_state,
            "connectionState": row.connection_state,
            "connectorState": row.connector_state or {},
            "lastSeen": row.last_seen,
            "createdAt": row.created_at,
            "tags": row.tags or [],
            "meta": row.meta or {},
        }
    )


@router.get("/vcps", response_model=VcpListOut, dependencies=[Depends(_auth)])
async def list_vcps(
    db: DbSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500, alias="pageSize"),
    group_id: str | None = Query(default=None, alias="groupId"),
    worker_id: str | None = Query(default=None, alias="workerId"),
    actual_state: str | None = Query(default=None, alias="actualState"),
    profile_id: str | None = Query(default=None, alias="profileId"),
) -> VcpListOut:
    store = VcpRegistryStore(db)
    rows, total = await store.list_vcps(
        page=page,
        page_size=page_size,
        group_id=group_id,
        worker_id=worker_id,
        actual_state=actual_state,
        profile_id=profile_id,
    )
    return VcpListOut(
        items=[_to_vcp_out(r) for r in rows],
        total=total,
        page=page,
        **{"pageSize": page_size},
    )


@router.post("/vcps", response_model=VcpOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(_auth)])
async def create_vcp(body: VcpCreateIn, db: DbSession) -> VcpOut:
    store = VcpRegistryStore(db)
    row = await store.create_vcp(body)
    return _to_vcp_out(row)


@router.get("/vcps/{vcp_id}", response_model=VcpOut, dependencies=[Depends(_auth)])
async def get_vcp(vcp_id: str, db: DbSession) -> VcpOut:
    store = VcpRegistryStore(db)
    row = await store.get_vcp(vcp_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"VCP '{vcp_id}' not found")
    return _to_vcp_out(row)


@router.patch("/vcps/{vcp_id}", response_model=VcpOut, dependencies=[Depends(_auth)])
async def patch_vcp(vcp_id: str, body: VcpPatchIn, db: DbSession) -> VcpOut:
    store = VcpRegistryStore(db)
    row = await store.patch_vcp(vcp_id, body)
    if row is None:
        raise HTTPException(status_code=404, detail=f"VCP '{vcp_id}' not found")
    return _to_vcp_out(row)


@router.delete("/vcps/{vcp_id}", response_model=OkOut, dependencies=[Depends(_auth)])
async def delete_vcp(vcp_id: str, db: DbSession) -> OkOut:
    store = VcpRegistryStore(db)
    deleted = await store.delete_vcp(vcp_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"VCP '{vcp_id}' not found")
    return OkOut(message=f"VCP '{vcp_id}' deregistered")


@router.post("/vcps/{vcp_id}/commands", response_model=CommandOut,
             dependencies=[Depends(_auth)])
async def send_command(
    vcp_id: str,
    body: VcpCommandIn,
    db: DbSession,
    redis: RedisClient,
    settings: SettingsDep,
) -> CommandOut:
    store = VcpRegistryStore(db)
    row = await store.get_vcp(vcp_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"VCP '{vcp_id}' not found")

    command_id = str(uuid.uuid4())
    publisher = CommandPublisher(redis_client=redis, settings=settings)
    await publisher.send_to_vcp(
        vcp_id=vcp_id,
        worker_id=row.worker_id,
        command_type=body.command_type,
        payload=body.payload,
        command_id=command_id,
        issued_by=body.issued_by,
    )
    # Audit log
    await store.record_command(
        command_id=command_id,
        command_type=body.command_type,
        vcp_id=vcp_id,
        payload=body.payload,
        issued_by=body.issued_by,
    )
    return CommandOut.model_validate(
        {"commandId": command_id, "commandType": body.command_type,
         "vcpId": vcp_id, "status": "dispatched"}
    )


@router.get("/vcps/{vcp_id}/commands", response_model=list[CommandAuditOut],
            dependencies=[Depends(_auth)])
async def get_vcp_commands(
    vcp_id: str,
    db: DbSession,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[CommandAuditOut]:
    store = VcpRegistryStore(db)
    rows = await store.list_commands(vcp_id=vcp_id, limit=limit)
    return [
        CommandAuditOut.model_validate(
            {
                "commandId": r.command_id,
                "commandType": r.command_type,
                "vcpId": r.vcp_id,
                "payload": r.payload or {},
                "issuedBy": r.issued_by,
                "status": r.status,
                "issuedAt": r.issued_at,
                "acknowledgedAt": r.acknowledged_at,
            }
        )
        for r in rows
    ]
