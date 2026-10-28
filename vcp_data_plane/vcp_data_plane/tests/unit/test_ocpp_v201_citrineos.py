"""Regression tests: OCPP 2.0.1 payloads must pass CitrineOS schema validation.

CitrineOS rejected DC StatusNotification / TransactionEvent with FormatViolation
because timestamps had microsecond precision, connector statuses used OCPP 1.6
names, periodic meter values crashed, and RequestStartTransaction ignored the
requested EVSE. Schemas live in tests/fixtures/citrineos_ocpp201.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

import fakeredis.aioredis
import jsonschema
import pytest

from vcp_data_plane.behavior.engine import BehaviorEngine
from vcp_data_plane.behavior.timer import VcpTimerScheduler
from vcp_data_plane.config.loader import load_profiles_dir
from vcp_data_plane.config.schema import RedisConfig, VcpEntry
from vcp_data_plane.ocpp.common import OcppCall, OcppCallResult
from vcp_data_plane.ocpp.v201 import messages as M
from vcp_data_plane.ocpp.v201.adapter import V201Adapter
from vcp_data_plane.ocpp.v201.handlers import V201Handlers
from vcp_data_plane.state.machine import VcpState, VcpStateMachine
from vcp_data_plane.state.registry import VcpRegistry

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "tests" / "fixtures" / "citrineos_ocpp201"


def _validator(name: str) -> jsonschema.Draft7Validator:
    return jsonschema.Draft7Validator(json.loads((SCHEMAS / f"{name}.json").read_text(encoding="utf-8")))


def _errors(name: str, payload: dict[str, Any]) -> list[str]:
    return [f"{'/'.join(map(str, e.path))}: {e.message}" for e in _validator(name).iter_errors(payload)]


# ---------------------------------------------------------------------------
# Timestamp format
# ---------------------------------------------------------------------------

def test_previously_failing_timestamp_is_rejected_by_citrineos_schema() -> None:
    # Payload captured from CitrineOS before the fix
    payload = {
        "evseId": 1,
        "timestamp": "2026-10-19T05:03:42.733034+00:00",
        "connectorId": 1,
        "connectorStatus": "Available",
    }
    assert any(err.startswith("timestamp:") for err in _errors("StatusNotificationRequest", payload))


def test_now_iso_uses_milliseconds_and_utc_z() -> None:
    ts = M._now_iso()
    assert ts.endswith("Z")
    fraction = ts.split(".")[1].rstrip("Z")
    assert len(fraction) == 3
    # Still a valid ISO-8601 UTC instant
    parsed = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    assert parsed.utcoffset() == datetime.timedelta(0)


def test_status_notification_payload_passes_citrineos_schema() -> None:
    payload = M.status_notification(
        timestamp=M._now_iso(), connector_status="Available", evse_id=1, connector_id=1,
    )
    assert _errors("StatusNotificationRequest", payload) == []


# ---------------------------------------------------------------------------
# Connector status enum
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("engine_status,expected", [
    ("Available", "Available"),
    ("Preparing", "Occupied"),
    ("Charging", "Occupied"),
    ("SuspendedEVSE", "Occupied"),
    ("Finishing", "Occupied"),
    ("Faulted", "Faulted"),
    ("Unavailable", "Unavailable"),
])
def test_status_notification_maps_ocpp16_names_to_201_enum(engine_status: str, expected: str) -> None:
    payload = M.status_notification(
        timestamp=M._now_iso(), connector_status=engine_status, evse_id=1, connector_id=1,
    )
    assert payload["connectorStatus"] == expected
    assert _errors("StatusNotificationRequest", payload) == []


# ---------------------------------------------------------------------------
# Full DC charge cycle through the real engine
# ---------------------------------------------------------------------------

class _FakeRouter:
    """Records outgoing CALLs and answers like an accepting CSMS."""

    _RESPONSES = {
        "BootNotification": {"status": "Accepted", "currentTime": "2026-10-19T05:03:42.733Z", "interval": 30},
        "Authorize": {"idTokenInfo": {"status": "Accepted"}},
    }

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.handlers: dict[str, Any] = {}

    def register(self, action: str, fn: Any) -> None:
        self.handlers[action] = fn

    async def call(self, action: str, payload: dict[str, Any], *args: Any, **kwargs: Any) -> OcppCallResult:
        self.sent.append((action, json.loads(json.dumps(payload))))
        return OcppCallResult(message_id="test", payload=self._RESPONSES.get(action, {}))


@pytest.fixture
def dc_engine(request):
    # Optional per-test transaction id source: @pytest.mark.parametrize("dc_engine", [source], indirect=True)
    txn_source = getattr(request, "param", None)
    profile = load_profiles_dir(str(ROOT / "configs" / "profiles"))["generic_dc_ocpp201"]
    entry = VcpEntry(vcp_id="DC-TEST", charge_point_id="CP-DC-TEST", profile_id=profile.profile_id)
    state = VcpStateMachine(
        vcp_id=entry.vcp_id,
        connector_count=len(profile.primary_evse.connectors),
        evse_count=len(profile.evses),
    )
    router = _FakeRouter()
    adapter = V201Adapter(vcp_id=entry.vcp_id, charge_point_id=entry.charge_point_id, router=router, profile=profile)
    timers = VcpTimerScheduler(vcp_id=entry.vcp_id)
    engine = BehaviorEngine(
        vcp_entry=entry, profile=profile, state_machine=state, adapter=adapter, timer_scheduler=timers,
        transaction_id_source=txn_source,
    )
    V201Handlers(
        vcp_id=entry.vcp_id,
        on_remote_start=engine.on_remote_start_v201,
        on_remote_stop=engine.on_remote_stop_v201,
        on_reset=engine.on_reset,
        on_set_variables=engine.on_set_variables,
        on_get_variables=engine.on_get_variables,
        on_change_availability=lambda evse, conn, status: engine.on_change_availability(conn or 1, status),
        on_trigger=engine.on_trigger,
        on_unlock=lambda evse, conn: engine.on_unlock(conn or 1),
    ).register_all(router)
    yield engine, router, state
    timers.cancel_all()


async def test_dc_charge_cycle_payloads_pass_citrineos_schemas(dc_engine) -> None:
    engine, router, state = dc_engine
    await state.set_connection_state(VcpState.RECOVERING)
    engine._running = True
    await engine._boot_lifecycle()
    await engine.plug_ev(id_tag="TEST-TAG")
    await engine.authorize("TEST-TAG")
    await engine.start_charge()
    engine._meter.tick()
    await engine._do_meter_values(1, 1)   # raised AttributeError before the fix
    await engine.stop_charge(reason="Local")

    sent = [(a, p) for a, p in router.sent if a in ("StatusNotification", "TransactionEvent")]
    event_types = [p["eventType"] for a, p in sent if a == "TransactionEvent"]
    assert event_types == ["Started", "Updated", "Ended"]

    for action, payload in sent:
        assert _errors(f"{action}Request", payload) == [], (action, payload)

    updated = next(p for a, p in sent if a == "TransactionEvent" and p["eventType"] == "Updated")
    assert updated["meterValue"][0]["sampledValue"]


async def test_request_start_transaction_uses_requested_evse(dc_engine) -> None:
    engine, router, state = dc_engine
    await state.set_connection_state(VcpState.RECOVERING)
    engine._running = True
    await engine._boot_lifecycle()
    # EVSE 1 busy charging
    await engine.plug_ev(id_tag="T1", evse_id=1)
    await engine.authorize("T1", evse_id=1)
    await engine.start_charge(evse_id=1)

    call = OcppCall(
        message_id="rst-1",
        action="RequestStartTransaction",
        payload={"idToken": {"idToken": "REMOTE-2", "type": "Central"}, "remoteStartId": 8, "evse": {"id": 2}},
    )
    response = await router.handlers["RequestStartTransaction"](call)

    assert response == {"status": "Accepted"}
    assert state.get_connector(2, 1).state == VcpState.CHARGING
    started = [p for a, p in router.sent if a == "TransactionEvent" and p["eventType"] == "Started"]
    assert started[-1]["evse"]["id"] == 2


# ---------------------------------------------------------------------------
# Numeric transaction ids (match the CSMS-assigned OCPP 1.6 style)
# ---------------------------------------------------------------------------

def _counter():
    n = 0

    async def next_id() -> int:
        nonlocal n
        n += 1
        return n
    return next_id


async def _charge_once(engine, evse_id: int = 1) -> None:
    await engine.plug_ev(id_tag="TEST-TAG", evse_id=evse_id)
    await engine.authorize("TEST-TAG", evse_id=evse_id)
    await engine.start_charge(evse_id=evse_id)


@pytest.mark.parametrize("dc_engine", [_counter()], indirect=True)
async def test_dc_transaction_ids_are_sequential_numbers(dc_engine) -> None:
    engine, router, state = dc_engine
    await _charge_once(engine, evse_id=1)
    await _charge_once(engine, evse_id=2)

    started = [p for a, p in router.sent if a == "TransactionEvent" and p["eventType"] == "Started"]
    assert [p["transactionInfo"]["transactionId"] for p in started] == ["1", "2"]
    for payload in started:
        assert _errors("TransactionEventRequest", payload) == []
    assert state.get_connector(1, 1).transaction_id == "1"


async def _unavailable() -> None:
    return None


@pytest.mark.parametrize("dc_engine", [_unavailable], indirect=True)
async def test_dc_transaction_id_falls_back_to_uuid_without_counter(dc_engine) -> None:
    engine, router, state = dc_engine
    await _charge_once(engine)
    txn_id = state.get_connector(1, 1).transaction_id
    assert len(txn_id) == 36 and txn_id.count("-") == 4


async def test_registry_transaction_counter_is_per_vcp_and_persistent() -> None:
    registry = VcpRegistry("worker-test", RedisConfig())
    registry._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)  # noqa: SLF001
    assert [await registry.next_transaction_id("DC-00000") for _ in range(3)] == [1, 2, 3]
    assert await registry.next_transaction_id("DC-00001") == 1

    # A restarted worker (new registry, same Redis) continues the sequence
    restarted = VcpRegistry("worker-test", RedisConfig())
    restarted._redis = registry._redis  # noqa: SLF001
    assert await restarted.next_transaction_id("DC-00000") == 4
