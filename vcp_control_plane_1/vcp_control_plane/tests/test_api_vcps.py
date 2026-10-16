"""Integration tests for /api/v1/vcps endpoints."""
from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_vcps_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/vcps")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data


@pytest.mark.asyncio
async def test_create_vcp(client: AsyncClient) -> None:
    payload = {
        "vcpId": "vcp-api-001",
        "chargePointId": "CP-API-001",
        "profileId": "profile-x",
    }
    resp = await client.post("/api/v1/vcps", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["vcpId"] == "vcp-api-001"
    assert data["chargePointId"] == "CP-API-001"
    assert data["actualState"] == "unknown"
    assert data["connectionState"] == "disconnected"


@pytest.mark.asyncio
async def test_get_vcp(client: AsyncClient) -> None:
    # Create first
    await client.post("/api/v1/vcps", json={"vcpId": "vcp-get-1", "chargePointId": "CP-G1"})
    resp = await client.get("/api/v1/vcps/vcp-get-1")
    assert resp.status_code == 200
    assert resp.json()["vcpId"] == "vcp-get-1"


@pytest.mark.asyncio
async def test_get_vcp_not_found(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/vcps/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_vcp(client: AsyncClient) -> None:
    await client.post("/api/v1/vcps", json={"vcpId": "vcp-patch-1", "chargePointId": "CP-P1"})
    resp = await client.patch("/api/v1/vcps/vcp-patch-1", json={"desiredState": "charging"})
    assert resp.status_code == 200
    assert resp.json()["desiredState"] == "charging"


@pytest.mark.asyncio
async def test_delete_vcp(client: AsyncClient) -> None:
    await client.post("/api/v1/vcps", json={"vcpId": "vcp-del-1", "chargePointId": "CP-D1"})
    resp = await client.delete("/api/v1/vcps/vcp-del-1")
    assert resp.status_code == 200
    assert "deregistered" in resp.json()["message"]

    # Should be gone
    resp2 = await client.get("/api/v1/vcps/vcp-del-1")
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_list_vcps_filter_by_group(client: AsyncClient) -> None:
    await client.post("/api/v1/vcps", json={
        "vcpId": "vcp-group-1", "chargePointId": "CP-GG1", "groupId": "grp-test"
    })
    resp = await client.get("/api/v1/vcps", params={"groupId": "grp-test"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert any(v["vcpId"] == "vcp-group-1" for v in items)


@pytest.mark.asyncio
async def test_get_vcp_commands_empty(client: AsyncClient) -> None:
    await client.post("/api/v1/vcps", json={"vcpId": "vcp-cmds-1", "chargePointId": "CP-C1"})
    resp = await client.get("/api/v1/vcps/vcp-cmds-1/commands")
    assert resp.status_code == 200
    assert resp.json() == []
