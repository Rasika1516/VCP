"""VCP Registry Store — SQLAlchemy async CRUD layer.

All database interactions for the control plane go through this class.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from vcp_control_plane.db.models import (
    ChargerProfile,
    CommandAudit,
    TestExecution,
    VcpInstance,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class VcpRegistryStore:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    # ------------------------------------------------------------------
    # VCP Instance
    # ------------------------------------------------------------------

    async def create_vcp(self, body) -> VcpInstance:
        """Create a new VCP registry entry."""
        row = VcpInstance(
            vcp_id=body.vcp_id or str(uuid.uuid4()),
            charge_point_id=body.charge_point_id,
            profile_id=body.profile_id or "",
            profile_version=getattr(body, "profile_version", "latest") or "latest",
            ocpp_version=body.ocpp_version or "1.6J",
            worker_id=body.worker_id,
            group_id=body.group_id,
            desired_state=getattr(body, "desired_state", None) or "idle",
            actual_state="unknown",
            connection_state="disconnected",
            connector_state={},
            tags=body.tags or [],
            meta=body.meta or {},
            created_at=_now(),
            updated_at=_now(),
        )
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)
        return row

    async def get_vcp(self, vcp_id: str) -> VcpInstance | None:
        result = await self._db.execute(
            select(VcpInstance).where(VcpInstance.vcp_id == vcp_id)
        )
        return result.scalar_one_or_none()

    async def list_vcps(
        self,
        page: int = 1,
        page_size: int = 50,
        group_id: str | None = None,
        worker_id: str | None = None,
        actual_state: str | None = None,
        profile_id: str | None = None,
    ) -> tuple[list[VcpInstance], int]:
        q = select(VcpInstance)
        count_q = select(func.count()).select_from(VcpInstance)

        if group_id:
            q = q.where(VcpInstance.group_id == group_id)
            count_q = count_q.where(VcpInstance.group_id == group_id)
        if worker_id:
            q = q.where(VcpInstance.worker_id == worker_id)
            count_q = count_q.where(VcpInstance.worker_id == worker_id)
        if actual_state:
            q = q.where(VcpInstance.actual_state == actual_state.lower())
            count_q = count_q.where(VcpInstance.actual_state == actual_state.lower())
        if profile_id:
            q = q.where(VcpInstance.profile_id == profile_id)
            count_q = count_q.where(VcpInstance.profile_id == profile_id)

        total_result = await self._db.execute(count_q)
        total = total_result.scalar_one()

        q = q.order_by(VcpInstance.created_at.desc())
        q = q.offset((page - 1) * page_size).limit(page_size)
        rows_result = await self._db.execute(q)
        rows = list(rows_result.scalars().all())
        return rows, total

    async def patch_vcp(self, vcp_id: str, body) -> VcpInstance | None:
        row = await self.get_vcp(vcp_id)
        if row is None:
            return None
        updates: dict[str, Any] = {"updated_at": _now()}
        if body.desired_state is not None:
            updates["desired_state"] = body.desired_state
        if body.worker_id is not None:
            updates["worker_id"] = body.worker_id
        if body.group_id is not None:
            updates["group_id"] = body.group_id
        if body.tags is not None:
            updates["tags"] = body.tags
        if body.meta is not None:
            updates["meta"] = body.meta
        if body.profile_id is not None:
            updates["profile_id"] = body.profile_id

        await self._db.execute(
            update(VcpInstance).where(VcpInstance.vcp_id == vcp_id).values(**updates)
        )
        await self._db.commit()
        return await self.get_vcp(vcp_id)

    async def delete_vcp(self, vcp_id: str) -> bool:
        result = await self._db.execute(
            delete(VcpInstance).where(VcpInstance.vcp_id == vcp_id)
        )
        await self._db.commit()
        return result.rowcount > 0

    async def upsert_vcp_registration(
        self,
        vcp_id: str,
        charge_point_id: str,
        worker_id: str | None,
        profile_id: str = "",
        profile_version: str = "latest",
        ocpp_version: str = "1.6J",
        firmware: str | None = None,
        security_profile: int = 0,
    ) -> bool:
        """Called by EventSubscriber when a worker announces a VCP it owns.

        Creates the vcp_instance row if missing; otherwise refreshes the
        worker-owned fields and leaves desired_state, group, tags and meta alone.
        Returns True if a new row was created.
        """
        fields: dict[str, Any] = {
            "charge_point_id": charge_point_id,
            "profile_id": profile_id,
            "profile_version": profile_version,
            "ocpp_version": ocpp_version,
            "firmware": firmware,
            "security_profile": security_profile,
            "worker_id": worker_id,
            "updated_at": _now(),
        }
        row = await self.get_vcp(vcp_id)
        if row is None:
            self._db.add(VcpInstance(
                vcp_id=vcp_id,
                desired_state="idle",
                actual_state="unknown",
                connection_state="disconnected",
                connector_state={},
                tags=[],
                meta={},
                created_at=_now(),
                **fields,
            ))
            created = True
        else:
            for key, value in fields.items():
                setattr(row, key, value)
            created = False
        await self._db.commit()
        return created

    async def update_vcp_state(
        self,
        vcp_id: str,
        actual_state: str | None = None,
        connection_state: str | None = None,
        connector_state: dict | None = None,
        worker_id: str | None = None,
    ) -> int:
        """Called by EventSubscriber to apply worker-reported state updates.

        States are stored lowercase ("connected", "charging" …) — workers send
        OCPP casing, while the API and UI use lowercase throughout.
        Returns the number of rows updated (0 if the VCP is not registered).
        """
        updates: dict[str, Any] = {"updated_at": _now(), "last_seen": _now()}
        if actual_state is not None:
            updates["actual_state"] = actual_state.lower()
        if connection_state is not None:
            updates["connection_state"] = connection_state.lower()
        if connector_state is not None:
            updates["connector_state"] = connector_state
        if worker_id is not None:
            updates["worker_id"] = worker_id
        result = await self._db.execute(
            update(VcpInstance).where(VcpInstance.vcp_id == vcp_id).values(**updates)
        )
        await self._db.commit()
        return result.rowcount

    async def aggregate_state_counts(self) -> dict:
        """Return total VCP count and state distribution dicts."""
        total_r = await self._db.execute(
            select(func.count()).select_from(VcpInstance)
        )
        total = total_r.scalar_one()

        # lower(): rows written before states were normalised still count together
        conn_state = func.lower(VcpInstance.connection_state)
        conn_r = await self._db.execute(
            select(conn_state, func.count()).group_by(conn_state)
        )
        connection_states = {row[0]: row[1] for row in conn_r.all()}

        actual_state = func.lower(VcpInstance.actual_state)
        actual_r = await self._db.execute(
            select(actual_state, func.count()).group_by(actual_state)
        )
        actual_states = {row[0]: row[1] for row in actual_r.all()}

        return {
            "total": total,
            "connection_states": connection_states,
            "actual_states": actual_states,
        }

    # ------------------------------------------------------------------
    # Charger Profile
    # ------------------------------------------------------------------

    async def upsert_profile(self, body) -> ChargerProfile:
        existing = await self.get_profile(body.profile_id)
        if existing:
            updates: dict[str, Any] = {
                "version": body.version,
                "vendor": body.vendor,
                "model": body.model,
                "firmware": body.firmware,
                "ocpp_version": body.ocpp_version,
                "capability_definition": body.capability_definition or {},
                "behavior_definition": body.behavior_definition or {},
                "evse_definition": body.evse_definition or [],
                "tags": body.tags or [],
                "status": "active",
                "updated_at": _now(),
            }
            if hasattr(body, "source") and body.source:
                updates["source"] = body.source
            await self._db.execute(
                update(ChargerProfile)
                .where(ChargerProfile.profile_id == body.profile_id)
                .values(**updates)
            )
            await self._db.commit()
            return await self.get_profile(body.profile_id)  # type: ignore[return-value]

        row = ChargerProfile(
            profile_id=body.profile_id,
            version=body.version,
            vendor=body.vendor,
            model=body.model,
            firmware=body.firmware,
            ocpp_version=body.ocpp_version,
            capability_definition=body.capability_definition or {},
            behavior_definition=body.behavior_definition or {},
            evse_definition=body.evse_definition or [],
            source=getattr(body, "source", None) or "",
            tags=body.tags or [],
            status="active",
            validated=False,
            created_at=_now(),
            updated_at=_now(),
        )
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)
        return row

    async def get_profile(self, profile_id: str) -> ChargerProfile | None:
        result = await self._db.execute(
            select(ChargerProfile).where(ChargerProfile.profile_id == profile_id)
        )
        return result.scalar_one_or_none()

    async def list_profiles(self) -> list[ChargerProfile]:
        result = await self._db.execute(
            select(ChargerProfile).order_by(ChargerProfile.created_at.desc())
        )
        return list(result.scalars().all())

    async def deprecate_profile(self, profile_id: str) -> bool:
        result = await self._db.execute(
            update(ChargerProfile)
            .where(ChargerProfile.profile_id == profile_id)
            .values(status="deprecated", updated_at=_now())
        )
        await self._db.commit()
        return result.rowcount > 0

    # ------------------------------------------------------------------
    # Test Execution
    # ------------------------------------------------------------------

    async def create_execution(
        self,
        execution_id: str,
        scenario_id: str,
        scenario_name: str,
        target_scope: str,
        target_count: int,
        initiated_by: str | None = None,
        target_vcp_id: str | None = None,
        target_group_id: str | None = None,
    ) -> TestExecution:
        now = _now()
        row = TestExecution(
            execution_id=execution_id,
            scenario_id=scenario_id,
            scenario_name=scenario_name,
            target_scope=target_scope,
            target_vcp_id=target_vcp_id,
            target_group_id=target_group_id,
            target_count=target_count,
            status="running",
            start_time=now,
            initiated_by=initiated_by,
            created_at=now,
        )
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)
        return row

    async def get_execution(self, execution_id: str) -> TestExecution | None:
        result = await self._db.execute(
            select(TestExecution).where(TestExecution.execution_id == execution_id)
        )
        return result.scalar_one_or_none()

    async def list_executions(self, limit: int = 100) -> list[TestExecution]:
        result = await self._db.execute(
            select(TestExecution)
            .order_by(TestExecution.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def cancel_execution(self, execution_id: str) -> bool:
        result = await self._db.execute(
            update(TestExecution)
            .where(
                TestExecution.execution_id == execution_id,
                TestExecution.status == "running",
            )
            .values(status="cancelled", end_time=_now())
        )
        await self._db.commit()
        return result.rowcount > 0

    async def finish_execution(
        self,
        execution_id: str,
        status: str,
        result: str | None = None,
        report: dict | None = None,
    ) -> None:
        await self._db.execute(
            update(TestExecution)
            .where(TestExecution.execution_id == execution_id)
            .values(
                status=status,
                result=result,
                report=report or {},
                end_time=_now(),
            )
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Command Audit
    # ------------------------------------------------------------------

    async def record_command(
        self,
        command_id: str,
        command_type: str,
        vcp_id: str | None = None,
        payload: dict | None = None,
        issued_by: str | None = None,
        group_id: str | None = None,
    ) -> CommandAudit:
        row = CommandAudit(
            command_id=command_id,
            command_type=command_type,
            vcp_id=vcp_id,
            group_id=group_id,
            payload=payload or {},
            issued_by=issued_by,
            status="dispatched",
            issued_at=_now(),
        )
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)
        return row

    async def list_commands(
        self,
        vcp_id: str | None = None,
        limit: int = 50,
    ) -> list[CommandAudit]:
        q = select(CommandAudit).order_by(CommandAudit.issued_at.desc()).limit(limit)
        if vcp_id:
            q = q.where(CommandAudit.vcp_id == vcp_id)
        result = await self._db.execute(q)
        return list(result.scalars().all())

    async def acknowledge_command(self, command_id: str, status: str = "acknowledged") -> None:
        await self._db.execute(
            update(CommandAudit)
            .where(CommandAudit.command_id == command_id)
            .values(status=status, acknowledged_at=_now())
        )
        await self._db.commit()
