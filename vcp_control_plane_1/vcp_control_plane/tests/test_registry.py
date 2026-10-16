"""Tests for VcpRegistryStore CRUD operations."""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from vcp_control_plane.api.schemas import ProfileCreateIn, VcpCreateIn, VcpPatchIn
from vcp_control_plane.registry.store import VcpRegistryStore


@pytest.mark.asyncio
async def test_create_and_get_vcp(db_session: AsyncSession) -> None:
    store = VcpRegistryStore(db_session)
    body = VcpCreateIn.model_validate({
        "vcpId": "vcp-001",
        "chargePointId": "CP-001",
        "profileId": "profile-a",
    })
    row = await store.create_vcp(body)
    assert row.vcp_id == "vcp-001"
    assert row.charge_point_id == "CP-001"

    fetched = await store.get_vcp("vcp-001")
    assert fetched is not None
    assert fetched.vcp_id == "vcp-001"


@pytest.mark.asyncio
async def test_get_nonexistent_vcp(db_session: AsyncSession) -> None:
    store = VcpRegistryStore(db_session)
    result = await store.get_vcp("does-not-exist")
    assert result is None


@pytest.mark.asyncio
async def test_list_vcps_pagination(db_session: AsyncSession) -> None:
    store = VcpRegistryStore(db_session)
    for i in range(5):
        body = VcpCreateIn.model_validate({
            "vcpId": f"vcp-list-{i}",
            "chargePointId": f"CP-LIST-{i}",
        })
        await store.create_vcp(body)

    rows, total = await store.list_vcps(page=1, page_size=3)
    assert len(rows) <= 3
    assert total >= 5


@pytest.mark.asyncio
async def test_list_vcps_filter_by_group(db_session: AsyncSession) -> None:
    store = VcpRegistryStore(db_session)
    body = VcpCreateIn.model_validate({
        "vcpId": "vcp-grp-1",
        "chargePointId": "CP-GRP-1",
        "groupId": "group-alpha",
    })
    await store.create_vcp(body)

    rows, _ = await store.list_vcps(group_id="group-alpha")
    assert any(r.vcp_id == "vcp-grp-1" for r in rows)

    rows_other, _ = await store.list_vcps(group_id="group-beta")
    assert not any(r.vcp_id == "vcp-grp-1" for r in rows_other)


@pytest.mark.asyncio
async def test_patch_vcp(db_session: AsyncSession) -> None:
    store = VcpRegistryStore(db_session)
    body = VcpCreateIn.model_validate({"vcpId": "vcp-patch", "chargePointId": "CP-PATCH"})
    await store.create_vcp(body)

    patch = VcpPatchIn.model_validate({"desiredState": "charging", "groupId": "g1"})
    updated = await store.patch_vcp("vcp-patch", patch)
    assert updated is not None
    assert updated.desired_state == "charging"
    assert updated.group_id == "g1"


@pytest.mark.asyncio
async def test_delete_vcp(db_session: AsyncSession) -> None:
    store = VcpRegistryStore(db_session)
    body = VcpCreateIn.model_validate({"vcpId": "vcp-del", "chargePointId": "CP-DEL"})
    await store.create_vcp(body)

    deleted = await store.delete_vcp("vcp-del")
    assert deleted is True
    assert await store.get_vcp("vcp-del") is None

    # Idempotent delete returns False
    assert await store.delete_vcp("vcp-del") is False


@pytest.mark.asyncio
async def test_aggregate_state_counts(db_session: AsyncSession) -> None:
    store = VcpRegistryStore(db_session)
    counts = await store.aggregate_state_counts()
    assert "total" in counts
    assert "connection_states" in counts
    assert "actual_states" in counts


@pytest.mark.asyncio
async def test_upsert_profile(db_session: AsyncSession) -> None:
    store = VcpRegistryStore(db_session)
    body = ProfileCreateIn.model_validate({
        "profileId": "profile-upsert",
        "vendor": "Acme",
        "model": "EV-100",
    })
    row = await store.upsert_profile(body)
    assert row.profile_id == "profile-upsert"

    # Upsert again — should update, not create duplicate
    body2 = ProfileCreateIn.model_validate({
        "profileId": "profile-upsert",
        "vendor": "Acme",
        "model": "EV-200",
        "version": "2.0.0",
    })
    row2 = await store.upsert_profile(body2)
    assert row2.model == "EV-200"
    assert row2.version == "2.0.0"


@pytest.mark.asyncio
async def test_create_and_cancel_execution(db_session: AsyncSession) -> None:
    store = VcpRegistryStore(db_session)
    row = await store.create_execution(
        execution_id="exec-001",
        scenario_id="basic_charge_cycle",
        scenario_name="Basic Charge Cycle",
        target_scope="fleet",
        target_count=10,
        initiated_by="test-user",
    )
    assert row.execution_id == "exec-001"
    assert row.status == "running"

    cancelled = await store.cancel_execution("exec-001")
    assert cancelled is True

    fetched = await store.get_execution("exec-001")
    assert fetched.status == "cancelled"

    # Can't cancel an already-cancelled execution
    assert await store.cancel_execution("exec-001") is False


@pytest.mark.asyncio
async def test_record_and_list_commands(db_session: AsyncSession) -> None:
    store = VcpRegistryStore(db_session)
    body = VcpCreateIn.model_validate({"vcpId": "vcp-cmd", "chargePointId": "CP-CMD"})
    await store.create_vcp(body)

    await store.record_command(
        command_id="cmd-001",
        command_type="reset",
        vcp_id="vcp-cmd",
        payload={"type": "Soft"},
        issued_by="admin",
    )

    cmds = await store.list_commands(vcp_id="vcp-cmd")
    assert len(cmds) >= 1
    assert cmds[0].command_id == "cmd-001"


@pytest.mark.asyncio
async def test_upsert_vcp_registration_creates_then_refreshes(db_session: AsyncSession) -> None:
    store = VcpRegistryStore(db_session)
    created = await store.upsert_vcp_registration(
        vcp_id="AC-00000",
        charge_point_id="CP-AC-00000",
        worker_id="worker-ac",
        profile_id="generic_ac_ocpp16",
        ocpp_version="1.6J",
    )
    assert created is True
    row = await store.get_vcp("AC-00000")
    assert row is not None
    assert row.worker_id == "worker-ac"
    assert row.profile_id == "generic_ac_ocpp16"

    await store.patch_vcp("AC-00000", VcpPatchIn.model_validate({"groupId": "group-keep"}))
    created = await store.upsert_vcp_registration(
        vcp_id="AC-00000",
        charge_point_id="CP-AC-00000",
        worker_id="worker-ac-2",
        profile_id="generic_ac_ocpp16",
    )
    assert created is False
    row = await store.get_vcp("AC-00000")
    assert row.worker_id == "worker-ac-2"
    assert row.group_id == "group-keep"


@pytest.mark.asyncio
async def test_state_updates_are_stored_lowercase(db_session: AsyncSession) -> None:
    store = VcpRegistryStore(db_session)
    await store.upsert_vcp_registration(
        vcp_id="DC-00000", charge_point_id="CP-DC-00000", worker_id="worker-dc",
    )
    await store.update_vcp_state("DC-00000", actual_state="Charging", connection_state="Connected")
    row = await store.get_vcp("DC-00000")
    assert row.actual_state == "charging"
    assert row.connection_state == "connected"

    counts = await store.aggregate_state_counts()
    assert counts["connection_states"].get("connected", 0) >= 1
    assert "Connected" not in counts["connection_states"]
