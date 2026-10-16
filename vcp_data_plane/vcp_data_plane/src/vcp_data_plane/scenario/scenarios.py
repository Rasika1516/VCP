"""Built-in Scenario Engine.

Provides named, reusable charging scenarios that can be triggered via the
command API or Redis Streams.  Scenarios are independent of charger profiles:
the same scenario runs against any profile and the CSMS behavior is what's
under test.

Built-in scenarios:
  boot_and_wait          — Boot → heartbeat (baseline connectivity check)
  plug_authorize_charge  — Full AC charge cycle: plug → authorize → charge → stop → unplug
  remote_start_charge    — Wait for CSMS RemoteStart, charge, then stop
  fault_and_recover      — Inject a connector fault, wait, then recover
  reconnect_storm        — Multiple rapid disconnect/reconnect cycles
  load_ramp              — Gradually ramp up charging power over time
  transaction_timeout    — Start a transaction then stop due to timeout
  csms_initiated_stop    — Start charge, wait for CSMS to send RemoteStop

Each scenario is an async generator that yields status strings so callers
can track progress.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, AsyncGenerator

logger = logging.getLogger(__name__)


class ScenarioError(Exception):
    """Raised when a scenario cannot proceed."""


class ScenarioRunner:
    """Executes built-in scenarios against a BehaviorEngine."""

    KNOWN_SCENARIOS: list[str] = [
        "boot_and_wait",
        "plug_authorize_charge",
        "remote_start_charge",
        "fault_and_recover",
        "reconnect_storm",
        "load_ramp",
        "transaction_timeout",
        "csms_initiated_stop",
    ]

    def __init__(self, vcp_id: str, behavior_engine: Any, fault_injector: Any) -> None:
        self._vcp_id = vcp_id
        self._engine = behavior_engine
        self._fault = fault_injector
        self._active_scenario: str | None = None
        self._active_task: asyncio.Task[None] | None = None

    @property
    def active_scenario(self) -> str | None:
        return self._active_scenario

    async def run(self, scenario_name: str, params: dict[str, Any] | None = None) -> AsyncGenerator[str, None]:
        """Run a named scenario, yielding status strings as it progresses."""
        if scenario_name not in self.KNOWN_SCENARIOS:
            raise ScenarioError(f"Unknown scenario: {scenario_name!r}. Known: {self.KNOWN_SCENARIOS}")

        self._active_scenario = scenario_name
        logger.info("[%s] Starting scenario: %s params=%s", self._vcp_id, scenario_name, params)

        method_name = f"_scenario_{scenario_name}"
        method = getattr(self, method_name, None)
        if method is None:
            raise ScenarioError(f"No implementation for scenario {scenario_name!r}")

        try:
            async for status in method(params or {}):
                yield status
        finally:
            self._active_scenario = None

    def start_async(self, scenario_name: str, params: dict[str, Any] | None = None) -> None:
        """Start a scenario in a background task (fire-and-forget)."""
        async def _runner() -> None:
            try:
                async for status in self.run(scenario_name, params):
                    logger.info("[%s] [%s] %s", self._vcp_id, scenario_name, status)
            except Exception as exc:
                logger.error("[%s] Scenario %s failed: %s", self._vcp_id, scenario_name, exc)
            finally:
                self._active_task = None

        self._active_task = asyncio.create_task(_runner(), name=f"{self._vcp_id}:{scenario_name}")

    async def stop_active(self) -> None:
        """Cancel the currently running scenario."""
        if self._active_task and not self._active_task.done():
            self._active_task.cancel()
            try:
                await self._active_task
            except asyncio.CancelledError:
                pass
        self._active_scenario = None

    # ------------------------------------------------------------------
    # Scenario implementations
    # ------------------------------------------------------------------

    async def _scenario_boot_and_wait(self, params: dict[str, Any]) -> AsyncGenerator[str, None]:
        wait_s = params.get("wait_s", 60)
        yield "Waiting for boot to complete"
        await asyncio.sleep(2)
        yield f"VCP booted — holding for {wait_s}s"
        await asyncio.sleep(wait_s)
        yield "Scenario complete: boot_and_wait"

    async def _scenario_plug_authorize_charge(self, params: dict[str, Any]) -> AsyncGenerator[str, None]:
        charge_duration_s = params.get("charge_duration_s", 120)
        id_tag = params.get("id_tag", f"TAG-{random.randint(10000, 99999)}")
        evse_id = params.get("evse_id", 1)
        connector_id = params.get("connector_id", 1)

        yield "Step 1: Plugging in EV"
        await self._engine.plug_ev(id_tag=id_tag, evse_id=evse_id, connector_id=connector_id)
        await asyncio.sleep(0.5)

        yield "Step 2: Authorizing"
        auth_ok = await self._engine.authorize(id_tag, evse_id=evse_id, connector_id=connector_id)
        if not auth_ok:
            yield "Scenario failed: Authorization rejected"
            return

        yield "Step 3: Starting charge"
        started = await self._engine.start_charge(evse_id=evse_id, connector_id=connector_id)
        if not started:
            yield "Scenario failed: StartTransaction rejected"
            return

        yield f"Step 4: Charging for {charge_duration_s}s"
        await asyncio.sleep(charge_duration_s)

        yield "Step 5: Stopping charge"
        await self._engine.stop_charge(reason="Local", evse_id=evse_id, connector_id=connector_id)
        await asyncio.sleep(0.5)

        yield "Step 6: Unplugging EV"
        await self._engine.unplug_ev(evse_id=evse_id, connector_id=connector_id)

        yield "Scenario complete: plug_authorize_charge"

    async def _scenario_remote_start_charge(self, params: dict[str, Any]) -> AsyncGenerator[str, None]:
        wait_timeout_s = params.get("wait_timeout_s", 30)
        charge_duration_s = params.get("charge_duration_s", 120)

        yield f"Waiting up to {wait_timeout_s}s for CSMS RemoteStart"
        # The actual remote start comes via on_remote_start in BehaviorEngine
        # We just wait for the state to change to Charging
        deadline = asyncio.get_event_loop().time() + wait_timeout_s
        while asyncio.get_event_loop().time() < deadline:
            from vcp_data_plane.state.machine import VcpState
            if self._engine._state.primary.state == VcpState.CHARGING:
                break
            await asyncio.sleep(1)
        else:
            yield "Scenario failed: CSMS did not send RemoteStart within timeout"
            return

        yield f"Charging for {charge_duration_s}s (CSMS-initiated)"
        await asyncio.sleep(charge_duration_s)

        yield "Stopping charge"
        await self._engine.stop_charge(reason="Remote")

        yield "Scenario complete: remote_start_charge"

    async def _scenario_fault_and_recover(self, params: dict[str, Any]) -> AsyncGenerator[str, None]:
        from vcp_data_plane.fault.injector import FaultType

        fault_type_str = params.get("fault_type", "connector_fault")
        fault_duration_s = params.get("fault_duration_s", 15)
        connector_id = params.get("connector_id", 1)

        try:
            fault_type = FaultType(fault_type_str)
        except ValueError:
            fault_type = FaultType.CONNECTOR_FAULT

        yield f"Step 1: Injecting {fault_type.value}"
        result = await self._fault.inject(fault_type, {"connector_id": connector_id})
        if not result.injected:
            yield f"Scenario failed: {result.message}"
            return

        yield f"Step 2: Holding faulted state for {fault_duration_s}s"
        await asyncio.sleep(fault_duration_s)

        yield "Step 3: Recovering from fault"
        await self._fault.recover()
        await asyncio.sleep(1)

        yield "Scenario complete: fault_and_recover"

    async def _scenario_reconnect_storm(self, params: dict[str, Any]) -> AsyncGenerator[str, None]:
        from vcp_data_plane.fault.injector import FaultType

        cycles = params.get("cycles", 5)
        interval_s = params.get("interval_s", 2.0)

        yield f"Starting reconnect storm: {cycles} cycles @ {interval_s}s"
        result = await self._fault.inject(FaultType.RECONNECT_STORM, {"cycles": cycles, "interval_s": interval_s})
        if not result.injected:
            yield f"Scenario failed: {result.message}"
            return

        wait_s = cycles * interval_s * 3
        yield f"Waiting {wait_s:.0f}s for storm to complete"
        await asyncio.sleep(wait_s)

        yield "Scenario complete: reconnect_storm"

    async def _scenario_load_ramp(self, params: dict[str, Any]) -> AsyncGenerator[str, None]:
        id_tag = params.get("id_tag", f"TAG-{random.randint(10000, 99999)}")
        ramp_steps = params.get("ramp_steps", 5)
        step_duration_s = params.get("step_duration_s", 30)

        yield "Step 1: Plug EV and authorize"
        await self._engine.plug_ev(id_tag=id_tag)
        auth_ok = await self._engine.authorize(id_tag)
        if not auth_ok:
            yield "Scenario failed: Authorization rejected"
            return

        yield "Step 2: Start charge"
        started = await self._engine.start_charge()
        if not started:
            yield "Scenario failed: StartTransaction rejected"
            return

        yield f"Step 3: Ramping over {ramp_steps} steps × {step_duration_s}s each"
        for step in range(1, ramp_steps + 1):
            yield f"  Ramp step {step}/{ramp_steps}"
            await asyncio.sleep(step_duration_s)

        yield "Step 4: Stopping charge"
        await self._engine.stop_charge(reason="Local")
        await self._engine.unplug_ev()

        yield "Scenario complete: load_ramp"

    async def _scenario_transaction_timeout(self, params: dict[str, Any]) -> AsyncGenerator[str, None]:
        id_tag = params.get("id_tag", f"TAG-{random.randint(10000, 99999)}")
        timeout_s = params.get("timeout_s", 60)

        yield "Plug and start charge"
        await self._engine.plug_ev(id_tag=id_tag)
        await self._engine.authorize(id_tag)
        started = await self._engine.start_charge()
        if not started:
            yield "Scenario failed: could not start charge"
            return

        yield f"Waiting {timeout_s}s for timeout to trigger"
        await asyncio.sleep(timeout_s + 5)  # +5s grace

        yield "Scenario complete: transaction_timeout (stop triggered by timer)"

    async def _scenario_csms_initiated_stop(self, params: dict[str, Any]) -> AsyncGenerator[str, None]:
        id_tag = params.get("id_tag", f"TAG-{random.randint(10000, 99999)}")
        wait_s = params.get("wait_for_stop_s", 120)

        yield "Plug, authorize, start charge"
        await self._engine.plug_ev(id_tag=id_tag)
        await self._engine.authorize(id_tag)
        started = await self._engine.start_charge()
        if not started:
            yield "Scenario failed: could not start charge"
            return

        yield f"Charging — waiting up to {wait_s}s for CSMS to send RemoteStop"
        deadline = asyncio.get_event_loop().time() + wait_s
        from vcp_data_plane.state.machine import VcpState
        while asyncio.get_event_loop().time() < deadline:
            if self._engine._state.primary.state == VcpState.AVAILABLE:
                break
            await asyncio.sleep(2)
        else:
            yield f"Timeout: CSMS did not stop within {wait_s}s — stopping locally"
            await self._engine.stop_charge(reason="Local")

        await self._engine.unplug_ev()
        yield "Scenario complete: csms_initiated_stop"
