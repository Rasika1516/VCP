"""Tests for the Redis command publisher."""
from __future__ import annotations

import json

import pytest

from vcp_control_plane.bus.publisher import CommandPublisher
from vcp_control_plane.config.settings import get_settings


@pytest.mark.asyncio
async def test_send_to_vcp_with_worker_id(fake_redis) -> None:
    settings = get_settings()
    publisher = CommandPublisher(redis_client=fake_redis, settings=settings)

    msg_id = await publisher.send_to_vcp(
        vcp_id="vcp-001",
        worker_id="worker-1",
        command_type="reset",
        payload={"type": "Soft"},
        command_id="cmd-001",
        issued_by="test",
    )
    assert msg_id is not None

    # Check the message landed in the correct per-worker stream
    stream_key = f"{settings.command_stream_prefix}:worker-1"
    messages = await fake_redis.xrange(stream_key, "-", "+")
    assert len(messages) == 1
    _id, fields = messages[0]
    assert fields["command_type"] == "reset"
    assert fields["vcp_id"] == "vcp-001"
    payload_parsed = json.loads(fields["payload"])
    assert payload_parsed["type"] == "Soft"


@pytest.mark.asyncio
async def test_send_to_vcp_without_worker_falls_back_to_broadcast(fake_redis) -> None:
    settings = get_settings()
    publisher = CommandPublisher(redis_client=fake_redis, settings=settings)

    await publisher.send_to_vcp(
        vcp_id="vcp-002",
        worker_id=None,
        command_type="change_state",
        payload={"state": "idle"},
        command_id="cmd-002",
    )

    messages = await fake_redis.xrange(settings.broadcast_stream, "-", "+")
    assert len(messages) >= 1
    found = any(fields.get("vcp_id") == "vcp-002" for _, fields in messages)
    assert found


@pytest.mark.asyncio
async def test_broadcast(fake_redis) -> None:
    settings = get_settings()
    publisher = CommandPublisher(redis_client=fake_redis, settings=settings)

    await publisher.broadcast(
        command_type="run_scenario",
        payload={"scenario_id": "basic_charge_cycle"},
        command_id="cmd-broadcast-1",
        issued_by="admin",
    )

    messages = await fake_redis.xrange(settings.broadcast_stream, "-", "+")
    assert len(messages) >= 1
    last_id, last_fields = messages[-1]
    assert last_fields["command_type"] == "run_scenario"
    assert last_fields["command_id"] == "cmd-broadcast-1"
    assert last_fields["vcp_id"] == ""  # broadcast has empty vcp_id


@pytest.mark.asyncio
async def test_group_broadcast(fake_redis) -> None:
    settings = get_settings()
    publisher = CommandPublisher(redis_client=fake_redis, settings=settings)

    await publisher.broadcast(
        command_type="boot_storm",
        payload={"jitter_ms": 100},
        command_id="cmd-group-1",
        group_id="group-alpha",
    )

    messages = await fake_redis.xrange(settings.broadcast_stream, "-", "+")
    assert any(
        fields.get("group_id") == "group-alpha"
        for _, fields in messages
    )
