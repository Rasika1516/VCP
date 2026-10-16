"""Scenario execution endpoints.

Built-in scenario IDs (matching vcp_data_plane scenario engine):
  basic_charge_cycle      — plug → authorize → charge → stop → unplug
  rapid_reconnect         — disconnect/reconnect loop
  fault_and_recover       — inject connector fault then recover
  boot_storm              — mass reconnect of a group
  meter_value_flood       — continuous MeterValues for load test
  remote_start_stop       — CSMS-initiated start/stop sequence

Routes
------
GET  /scenarios               — list available built-in scenarios
POST /scenarios/run           — launch an execution
GET  /scenarios/executions    — list past executions
GET  /scenarios/executions/{id} — execution detail
POST /scenarios/executions/{id}/cancel — cancel a running execution
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from vcp_control_plane.api.auth import make_auth_dependency
from vcp_control_plane.api.deps import DbSession, RedisClient, SettingsDep
from vcp_control_plane.api.schemas import (
    ExecutionListOut,
    ExecutionOut,
    OkOut,
    ScenarioRunIn,
)
from vcp_control_plane.bus.publisher import CommandPublisher
from vcp_control_plane.registry.store import VcpRegistryStore

router = APIRouter()
_auth = make_auth_dependency()

# ---------------------------------------------------------------------------
# Built-in scenario catalogue
# ---------------------------------------------------------------------------

BUILT_IN_SCENARIOS = [
    {
        "scenarioId": "basic_charge_cycle",
        "name": "Basic Charge Cycle",
        "description": (
            "Plug EV → Authorize → Start transaction → Generate meter values → "
            "Stop transaction → Unplug. Verifies the full happy-path OCPP sequence."
        ),
        "parameters": [
            {"name": "duration_s", "type": "int", "default": 60, "description": "Charge duration"},
            {"name": "meter_interval_s", "type": "int", "default": 10,
             "description": "Meter value interval"},
        ],
    },
    {
        "scenarioId": "rapid_reconnect",
        "name": "Rapid Reconnect",
        "description": "Disconnect and reconnect VCP repeatedly to stress-test CSMS reconnect handling.",
        "parameters": [
            {"name": "cycles", "type": "int", "default": 5},
            {"name": "interval_s", "type": "float", "default": 2.0},
        ],
    },
    {
        "scenarioId": "fault_and_recover",
        "name": "Fault and Recover",
        "description": "Inject a connector fault, hold for a configurable duration, then recover.",
        "parameters": [
            {"name": "fault_duration_s", "type": "float", "default": 30.0},
            {"name": "error_code", "type": "str", "default": "GroundFailure"},
        ],
    },
    {
        "scenarioId": "boot_storm",
        "name": "Boot Storm",
        "description": (
            "Disconnect then reconnect all VCPs in a group simultaneously to simulate a "
            "mass-reboot event and test CSMS BootNotification surge handling."
        ),
        "parameters": [
            {"name": "jitter_ms", "type": "int", "default": 0,
             "description": "Random delay added to each reconnect"},
        ],
    },
    {
        "scenarioId": "meter_value_flood",
        "name": "Meter Value Flood",
        "description": "Send MeterValues at high frequency for a load / soak test.",
        "parameters": [
            {"name": "duration_s", "type": "int", "default": 300},
            {"name": "interval_s", "type": "float", "default": 1.0},
        ],
    },
    {
        "scenarioId": "remote_start_stop",
        "name": "Remote Start / Stop",
        "description": (
            "Simulate a CSMS-initiated RemoteStartTransaction / RemoteStopTransaction sequence. "
            "The VCP responds as a compliant charger."
        ),
        "parameters": [
            {"name": "id_tag", "type": "str", "default": "RFID-TEST"},
            {"name": "duration_s", "type": "int", "default": 60},
        ],
    },
]

_SCENARIO_IDS = {s["scenarioId"] for s in BUILT_IN_SCENARIOS}


def _exec_to_out(row) -> ExecutionOut:
    return ExecutionOut.model_validate(
        {
            "executionId": row.execution_id,
            "scenarioId": row.scenario_id,
            "scenarioName": row.scenario_name,
            "targetScope": row.target_scope,
            "status": row.status,
            "result": row.result,
            "startTime": row.start_time,
            "endTime": row.end_time,
            "targetCount": row.target_count,
            "createdAt": row.created_at,
        }
    )


@router.get("/scenarios", dependencies=[Depends(_auth)])
async def list_scenarios() -> list[dict]:
    return BUILT_IN_SCENARIOS


@router.post("/scenarios/run", response_model=ExecutionOut,
             status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(_auth)])
async def run_scenario(
    body: ScenarioRunIn,
    db: DbSession,
    redis: RedisClient,
    settings: SettingsDep,
) -> ExecutionOut:
    if body.scenario_id not in _SCENARIO_IDS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown scenario '{body.scenario_id}'. "
                   f"Available: {sorted(_SCENARIO_IDS)}",
        )

    store = VcpRegistryStore(db)
    publisher = CommandPublisher(redis_client=redis, settings=settings)

    # Determine target VCPs
    target_count = 0
    if body.target_scope == "single" and body.target_vcp_id:
        target_count = 1
        await publisher.send_to_vcp(
            vcp_id=body.target_vcp_id,
            worker_id=None,
            command_type="run_scenario",
            payload={"scenario_id": body.scenario_id, **body.parameters},
            command_id=str(uuid.uuid4()),
            issued_by=body.initiated_by,
        )
    elif body.target_scope == "group" and body.target_group_id:
        vcps, _ = await store.list_vcps(group_id=body.target_group_id, page=1, page_size=10000)
        target_count = len(vcps)
        for vcp in vcps:
            await publisher.send_to_vcp(
                vcp_id=vcp.vcp_id,
                worker_id=vcp.worker_id,
                command_type="run_scenario",
                payload={"scenario_id": body.scenario_id, **body.parameters},
                command_id=str(uuid.uuid4()),
                issued_by=body.initiated_by,
            )
    else:
        # Fleet broadcast
        target_count = (await store.aggregate_state_counts())["total"]
        await publisher.broadcast(
            command_type="run_scenario",
            payload={"scenario_id": body.scenario_id, **body.parameters},
            command_id=str(uuid.uuid4()),
            issued_by=body.initiated_by,
        )

    row = await store.create_execution(
        execution_id=str(uuid.uuid4()),
        scenario_id=body.scenario_id,
        scenario_name=body.scenario_name,
        target_scope=body.target_scope,
        target_vcp_id=body.target_vcp_id,
        target_group_id=body.target_group_id,
        target_count=target_count,
        initiated_by=body.initiated_by,
    )
    return _exec_to_out(row)


@router.get("/scenarios/executions", response_model=ExecutionListOut, dependencies=[Depends(_auth)])
async def list_executions(db: DbSession) -> ExecutionListOut:
    store = VcpRegistryStore(db)
    rows = await store.list_executions()
    return ExecutionListOut(items=[_exec_to_out(r) for r in rows], total=len(rows))


@router.get("/scenarios/executions/{execution_id}", response_model=ExecutionOut,
            dependencies=[Depends(_auth)])
async def get_execution(execution_id: str, db: DbSession) -> ExecutionOut:
    store = VcpRegistryStore(db)
    row = await store.get_execution(execution_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Execution '{execution_id}' not found")
    return _exec_to_out(row)


@router.post("/scenarios/executions/{execution_id}/cancel", response_model=OkOut,
             dependencies=[Depends(_auth)])
async def cancel_execution(execution_id: str, db: DbSession) -> OkOut:
    store = VcpRegistryStore(db)
    ok = await store.cancel_execution(execution_id)
    if not ok:
        raise HTTPException(status_code=404,
                            detail=f"Execution '{execution_id}' not found or already finished")
    return OkOut(message=f"Execution '{execution_id}' cancelled")
