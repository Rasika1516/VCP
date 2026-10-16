"""Unit tests for CommandListener stream-entry parsing and dispatch."""
from __future__ import annotations

import json

import fakeredis.aioredis
import pytest

from vcp_data_plane.command.listener import CommandListener, parse_command_fields
from vcp_data_plane.config.schema import CommandType, RedisConfig, WorkerCommand


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _control_plane_fields(**overrides: str) -> dict[str, str]:
    """Fields exactly as vcp_control_plane bus/publisher.py writes them."""
    fields = {
        "command_id": "a41d19b4-8b00-4f09-840b-8d81c94aa51d",
        "command_type": "connect",
        "vcp_id": "SIM-00001",
        "payload": json.dumps({"id_tag": "TAG-1"}),
        "issued_by": "ui",
        "group_id": "",
        "ts": "2026-10-08T10:28:02.937090+00:00",
    }
    fields.update(overrides)
    return fields


# ---------------------------------------------------------------------------
# parse_command_fields
# ---------------------------------------------------------------------------

def test_parse_control_plane_flat_fields():
    cmd = parse_command_fields(_control_plane_fields())
    assert cmd is not None
    assert cmd.command_id == "a41d19b4-8b00-4f09-840b-8d81c94aa51d"
    assert cmd.command_type == CommandType.CONNECT
    assert cmd.vcp_id == "SIM-00001"
    assert cmd.group_id is None
    assert cmd.payload == {"id_tag": "TAG-1"}
    assert cmd.issued_by == "ui"
    assert cmd.issued_at == "2026-10-08T10:28:02.937090+00:00"


def test_parse_empty_vcp_id_means_broadcast():
    cmd = parse_command_fields(_control_plane_fields(vcp_id=""))
    assert cmd is not None
    assert cmd.vcp_id is None


def test_parse_group_id_kept_when_set():
    cmd = parse_command_fields(_control_plane_fields(vcp_id="", group_id="grp-1"))
    assert cmd is not None
    assert cmd.group_id == "grp-1"


def test_parse_missing_payload_defaults_to_empty_dict():
    fields = _control_plane_fields()
    del fields["payload"]
    cmd = parse_command_fields(fields)
    assert cmd is not None
    assert cmd.payload == {}


def test_parse_legacy_command_json():
    original = WorkerCommand(
        command_id="c-1",
        command_type=CommandType.PLUG_EV,
        vcp_id="SIM-00002",
        payload={"connector_id": 1},
    )
    cmd = parse_command_fields({"command_json": original.model_dump_json()})
    assert cmd == original


def test_parse_no_command_fields_returns_none():
    assert parse_command_fields({}) is None
    assert parse_command_fields({"foo": "bar"}) is None


def test_parse_unknown_command_type_raises():
    with pytest.raises(ValueError):
        parse_command_fields(_control_plane_fields(command_type="not_a_command"))


def test_parse_non_object_payload_raises():
    with pytest.raises(ValueError):
        parse_command_fields(_control_plane_fields(payload="[1, 2]"))


def test_parse_invalid_payload_json_raises():
    with pytest.raises(ValueError):
        parse_command_fields(_control_plane_fields(payload="{not json"))


# ---------------------------------------------------------------------------
# CommandListener._handle_entry (dispatch + ACK)
# ---------------------------------------------------------------------------

STREAM = "vcp:commands:worker:worker-test"
GROUP = "data-plane"


async def _listener_with_entry(fields: dict[str, str]) -> tuple[CommandListener, str]:
    listener = CommandListener("worker-test", RedisConfig())
    listener._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)  # noqa: SLF001
    await listener._ensure_group(STREAM, GROUP)  # noqa: SLF001
    msg_id = await listener._redis.xadd(STREAM, fields)  # noqa: SLF001
    # Deliver it to the consumer so it lands in the pending list
    await listener._redis.xreadgroup(GROUP, "c", {STREAM: ">"})  # noqa: SLF001
    return listener, msg_id


async def _pending_count(listener: CommandListener) -> int:
    info = await listener._redis.xpending(STREAM, GROUP)  # noqa: SLF001
    return info["pending"]


async def test_handle_entry_dispatches_control_plane_command_and_acks():
    listener, msg_id = await _listener_with_entry(_control_plane_fields())
    received: list[WorkerCommand] = []

    async def _handler(cmd: WorkerCommand) -> dict:
        received.append(cmd)
        return {"ok": True}

    listener.register(CommandType.CONNECT, _handler)
    await listener._handle_entry(STREAM, GROUP, msg_id, _control_plane_fields())  # noqa: SLF001

    assert len(received) == 1
    assert received[0].vcp_id == "SIM-00001"
    assert await _pending_count(listener) == 0


async def test_handle_entry_acks_malformed_without_dispatch():
    bad = _control_plane_fields(command_type="reset")
    listener, msg_id = await _listener_with_entry(bad)
    called = False

    async def _handler(cmd: WorkerCommand) -> dict:
        nonlocal called
        called = True
        return {}

    for ct in CommandType:
        listener.register(ct, _handler)
    await listener._handle_entry(STREAM, GROUP, msg_id, bad)  # noqa: SLF001

    assert called is False
    assert await _pending_count(listener) == 0
