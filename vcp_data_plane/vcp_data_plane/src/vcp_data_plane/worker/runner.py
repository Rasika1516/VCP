"""VCP Worker Runner — wires together all data plane components.

One Runner instance manages all VCPs assigned to this worker process.
Each VCP gets its own:
  - VcpStateMachine       (state tracking)
  - ConnectionManager     (WebSocket lifecycle)
  - OcppRouter            (message dispatch)
  - V16Adapter or V201Adapter  (protocol-specific messages)
  - V16Handlers or V201Handlers (inbound handler registration)
  - BehaviorEngine        (charger lifecycle driven by profile)
  - VcpTimerScheduler     (heartbeat, meter value, session timers)
  - FaultInjector         (fault injection)
  - ScenarioRunner        (built-in scenario execution)

The Runner also owns:
  - VcpRegistry           (state snapshot + Redis registration)
  - CommandListener       (Redis Streams command consumer)
  - TracePublisher        (Redis Streams trace output)
  - WorkerMetrics         (Prometheus)
  - Worker API            (FastAPI)

Startup sequence:
  1. Load profiles
  2. Start Redis (registry + command listener + trace publisher)
  3. Build and start each VCP runtime
  4. Register worker in Redis
  5. Start command listener
  6. Start FastAPI worker API
  7. Block until stop signal
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from vcp_data_plane.behavior.engine import BehaviorEngine
from vcp_data_plane.behavior.timer import VcpTimerScheduler
from vcp_data_plane.command.listener import CommandListener
from vcp_data_plane.config.loader import load_profiles_dir
from vcp_data_plane.config.schema import (
    ChargerProfile,
    CommandType,
    OcppVersion,
    VcpEntry,
    WorkerCommand,
    WorkerConfig,
)
from vcp_data_plane.connection.manager import ConnectionManager
from vcp_data_plane.fault.injector import FaultInjector, FaultType
from vcp_data_plane.metrics.collector import WorkerMetrics
from vcp_data_plane.ocpp.router import OcppRouter
from vcp_data_plane.ocpp.v16.adapter import V16Adapter
from vcp_data_plane.ocpp.v16.handlers import V16Handlers
from vcp_data_plane.ocpp.v201.adapter import V201Adapter
from vcp_data_plane.ocpp.v201.handlers import V201Handlers
from vcp_data_plane.scenario.scenarios import ScenarioRunner
from vcp_data_plane.state.machine import VcpState, VcpStateMachine
from vcp_data_plane.state.registry import VcpRegistry
from vcp_data_plane.trace.publisher import NullTracePublisher, TracePublisher
from vcp_data_plane.event.publisher import EventPublisher

logger = logging.getLogger(__name__)


class VcpRuntime:
    """All components for a single VCP instance."""

    def __init__(
        self,
        entry: VcpEntry,
        profile: ChargerProfile,
        state_machine: VcpStateMachine,
        connection: ConnectionManager,
        router: OcppRouter,
        adapter: Any,  # V16Adapter | V201Adapter
        behavior: BehaviorEngine,
        fault: FaultInjector,
        scenario: ScenarioRunner,
        timers: VcpTimerScheduler,
    ) -> None:
        self.entry = entry
        self.profile = profile
        self.state = state_machine
        self.connection = connection
        self.router = router
        self.adapter = adapter
        self.behavior = behavior
        self.fault = fault
        self.scenario = scenario
        self.timers = timers


class WorkerRunner:
    """Wires and runs all VCP runtimes for one worker process."""

    def __init__(self, config: WorkerConfig) -> None:
        self._config = config
        self._worker_id = config.worker_id
        self._runtimes: dict[str, VcpRuntime] = {}
        self._profiles: dict[str, ChargerProfile] = {}
        self._registry: VcpRegistry | None = None
        self._cmd_listener: CommandListener | None = None
        self._events: EventPublisher | None = None
        self._trace: TracePublisher | NullTracePublisher | None = None
        self._metrics: WorkerMetrics | None = None

    async def start(self) -> None:
        """Start all components."""
        logger.info("Worker %s starting (%d VCPs)", self._worker_id, len(self._config.vcps))

        # 1. Load profiles
        self._profiles = load_profiles_dir(self._config.profile_dir)
        logger.info("Loaded %d profiles from %s", len(self._profiles), self._config.profile_dir)

        # 2. Init metrics
        self._metrics = WorkerMetrics(worker_id=self._worker_id)
        self._metrics.vcps_total.set(len(self._config.vcps))

        # 3. Init Redis-backed components
        redis_cfg = self._config.redis
        self._registry = VcpRegistry(self._worker_id, redis_cfg)
        await self._registry.start()
        
        # RNZ (2026-10-09): Pass the configured log file so outgoing events are logged.
        self._events = EventPublisher(
            self._worker_id, redis_cfg, log_file=self._config.redis_event_log_file
        )
        await self._events.start()

        self._trace = TracePublisher(self._worker_id, redis_cfg)
        await self._trace.start()

        self._cmd_listener = CommandListener(self._worker_id, redis_cfg)
        self._register_command_handlers()

        # 4. Build VCP runtimes
        vcps = self._config.vcps
        logger.info("Building %d VCP runtimes", len(vcps))
        for vcp_entry in vcps:
            try:
                runtime = self._build_runtime(vcp_entry)
                self._runtimes[vcp_entry.vcp_id] = runtime
                self._registry.register(runtime.state)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to build runtime for %s: %s", vcp_entry.vcp_id, exc)
                continue
            # Sent before any state_change on the same stream, so the control plane
            # has the vcp_instance row by the time state updates arrive.
            await self._events.publish_vcp_registered(vcp_entry.vcp_id, {
                "charge_point_id": vcp_entry.charge_point_id,
                "profile_id": runtime.profile.profile_id,
                "profile_version": vcp_entry.profile_version,
                "ocpp_version": runtime.profile.effective_ocpp_version(vcp_entry.ocpp_version).value,
                "firmware": runtime.profile.firmware,
                "security_profile": int(runtime.profile.security_profile),
            })

        # 5. Register worker in Redis
        await self._registry.register_worker()

        # 6. Start command listener
        await self._cmd_listener.start()

        # 7. Start all VCP connections and behavior engines
        start_tasks = []
        for runtime in self._runtimes.values():
            start_tasks.append(asyncio.create_task(self._start_vcp_runtime(runtime)))

        if start_tasks:
            await asyncio.gather(*start_tasks, return_exceptions=True)

        logger.info("Worker %s running with %d VCPs", self._worker_id, len(self._runtimes))

    async def stop(self) -> None:
        """Gracefully stop all components."""
        logger.info("Worker %s stopping", self._worker_id)

        # Stop VCP runtimes
        stop_tasks = []
        for runtime in self._runtimes.values():
            stop_tasks.append(asyncio.create_task(self._stop_vcp_runtime(runtime)))
        if stop_tasks:
            await asyncio.gather(*stop_tasks, return_exceptions=True)

        # Stop command listener
        if self._cmd_listener:
            await self._cmd_listener.stop()

        # Deregister from Redis
        if self._registry:
            await self._registry.deregister_worker()
            await self._registry.stop()

        if self._events:
            await self._events.stop()
    
        # Stop trace publisher
        if self._trace:
            await self._trace.stop()

        logger.info("Worker %s stopped", self._worker_id)

    async def dispatch_command(self, vcp_id: str, command: WorkerCommand) -> dict[str, Any]:
        """Dispatch a command directly to a VCP runtime (used by Worker API)."""
        runtime = self._runtimes.get(vcp_id)
        if runtime is None:
            return {"error": f"VCP {vcp_id!r} not found"}

        return await self._execute_command(runtime, command)

    # ------------------------------------------------------------------
    # VCP Runtime builder
    # ------------------------------------------------------------------

    def _build_runtime(self, entry: VcpEntry) -> VcpRuntime:
        profile = self._resolve_profile(entry)
        ocpp_version = profile.effective_ocpp_version(entry.ocpp_version)

        # State machine
        state_machine = VcpStateMachine(
            vcp_id=entry.vcp_id,
            connector_count=len(profile.primary_evse.connectors),
            evse_count=len(profile.evses),
        )

        # OCPP Router
        router = OcppRouter(vcp_id=entry.vcp_id, request_timeout_s=30.0)

        # Connection Manager
        connection = ConnectionManager(
            vcp_id=entry.vcp_id,
            charge_point_id=entry.charge_point_id,
            csms_config=self._config.csms,
            ocpp_version=ocpp_version,
            security_profile=profile.security_profile,
            state_machine=state_machine,
            on_message=router.handle_inbound,
            on_connected=self._make_on_connected(entry.vcp_id),
            on_disconnected=self._make_on_disconnected(entry.vcp_id),
            boot_jitter_ms=self._config.boot_jitter_ms,
        )

        # Wire router ↔ connection
        router.set_send_fn(connection.send)

        # Build protocol adapter
        timers = VcpTimerScheduler(vcp_id=entry.vcp_id)

        if ocpp_version in (OcppVersion.V16, OcppVersion.V16J):
            adapter = V16Adapter(
                vcp_id=entry.vcp_id,
                charge_point_id=entry.charge_point_id,
                router=router,
                profile=profile,
            )
        else:
            adapter = V201Adapter(
                vcp_id=entry.vcp_id,
                charge_point_id=entry.charge_point_id,
                router=router,
                profile=profile,
            )

        # Behavior engine
        behavior = BehaviorEngine(
            vcp_entry=entry,
            profile=profile,
            state_machine=state_machine,
            adapter=adapter,
            timer_scheduler=timers,
            metrics_hooks=self._metrics,
        )

        # Fault injector
        fault = FaultInjector(vcp_id=entry.vcp_id, state_machine=state_machine)
        fault.bind(
            send_fn=connection.send,
            stop_fn=connection.stop,
            start_fn=connection.start,
        )

        # Scenario runner
        scenario = ScenarioRunner(
            vcp_id=entry.vcp_id,
            behavior_engine=behavior,
            fault_injector=fault,
        )

        # Register OCPP handlers
        if ocpp_version in (OcppVersion.V16, OcppVersion.V16J):
            handlers = V16Handlers(
                vcp_id=entry.vcp_id,
                on_remote_start=behavior.on_remote_start,
                on_remote_stop=behavior.on_remote_stop,
                on_reset=behavior.on_reset,
                on_unlock=behavior.on_unlock,
                on_get_config=behavior.on_get_config,
                on_change_config=behavior.on_change_config,
                on_trigger=behavior.on_trigger,
                on_change_availability=behavior.on_change_availability,
            )
            handlers.register_all(router)
        else:
            handlers = V201Handlers(
                vcp_id=entry.vcp_id,
                on_remote_start=behavior.on_remote_start_v201,
                on_remote_stop=behavior.on_remote_stop_v201,
                on_reset=behavior.on_reset,
                on_set_variables=behavior.on_set_variables,
                on_get_variables=behavior.on_get_variables,
                on_change_availability=lambda evse, conn, status: behavior.on_change_availability(conn or 1, status),
                on_trigger=behavior.on_trigger,
                on_unlock=lambda evse, conn: behavior.on_unlock(conn or 1),
            )
            handlers.register_all(router)

        # State change listener: publish to Redis
        async def _on_state_change(event: Any) -> None:
            await self._registry.publish_state(entry.vcp_id)
            if self._events:
                await self._events.publish_state_change(
                    entry.vcp_id, state_machine.snapshot(), reason=event.reason
                )
            if self._metrics:
                self._metrics.update_state_distribution(self._registry.summary())

        state_machine.add_listener(_on_state_change)

        return VcpRuntime(
            entry=entry,
            profile=profile,
            state_machine=state_machine,
            connection=connection,
            router=router,
            adapter=adapter,
            behavior=behavior,
            fault=fault,
            scenario=scenario,
            timers=timers,
        )

    def _resolve_profile(self, entry: VcpEntry) -> ChargerProfile:
        """Look up the charger profile for a VCP entry."""
        profile = self._profiles.get(entry.profile_id)
        if profile is None:
            raise ValueError(
                f"Profile {entry.profile_id!r} not found for VCP {entry.vcp_id}. "
                f"Available: {list(self._profiles)}"
            )
        return profile

    # ------------------------------------------------------------------
    # Start / stop individual VCP runtime
    # ------------------------------------------------------------------

    async def _start_vcp_runtime(self, runtime: VcpRuntime) -> None:
        try:
            await runtime.connection.start()
            await runtime.behavior.start()
            if self._metrics:
                self._metrics.vcps_connected.inc()
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] Failed to start VCP runtime: %s", runtime.entry.vcp_id, exc)

    async def _stop_vcp_runtime(self, runtime: VcpRuntime) -> None:
        try:
            await runtime.behavior.stop()
            await runtime.connection.stop()
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] Error stopping VCP runtime: %s", runtime.entry.vcp_id, exc)

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    def _register_command_handlers(self) -> None:
        """Register all command types with the CommandListener."""
        if self._cmd_listener is None:
            return

        # async def _handle(command: WorkerCommand) -> dict[str, Any]:
        #     if self._metrics:
        #         self._metrics.on_command_received(command.command_type.value)

        #     # Route to specific VCP or broadcast to all
        #     if command.vcp_id:
        #         runtime = self._runtimes.get(command.vcp_id)
        #         if runtime is None:
        #             return {"error": f"VCP {command.vcp_id!r} not found"}
                
        #     else:
        #         # Broadcast to all VCPs owned by this worker
        #         results = {}
        #         for vcp_id, runtime in self._runtimes.items():
        #             try:
        #                 results[vcp_id] = await self._execute_command(runtime, command)
        #             except Exception as exc:  # noqa: BLE001
        #                 results[vcp_id] = {"error": str(exc)}
        #         return results


        async def _handle(command: WorkerCommand) -> dict[str, Any]:
            if self._metrics:
                self._metrics.on_command_received(command.command_type.value)

            # Route to specific VCP or broadcast to all
            if command.vcp_id:
                runtime = self._runtimes.get(command.vcp_id)
                if runtime is None:
                    result = {"ok": False, "error": f"VCP {command.vcp_id!r} not found"}
                else:
                    result = await self._execute_command(runtime, command)
                if self._events:
                    await self._events.publish_command_result(command.vcp_id, command, result)
                return result

            # Broadcast to all VCPs owned by this worker
            results = {}
            for vcp_id, runtime in self._runtimes.items():
                try:
                    results[vcp_id] = await self._execute_command(runtime, command)
                except Exception as exc:  # noqa: BLE001
                    results[vcp_id] = {"ok": False, "error": str(exc)}
                if self._events:
                    await self._events.publish_command_result(vcp_id, command, results[vcp_id])
            return results

        # Register the same handler for all command types
        for cmd_type in CommandType:
            self._cmd_listener.register(cmd_type, _handle)

    async def _execute_command(self, runtime: VcpRuntime, command: WorkerCommand) -> dict[str, Any]:
        """Execute one command against a specific VCP runtime."""
        ct = command.command_type
        p = command.payload
        vcp_id = runtime.entry.vcp_id

        logger.debug("[%s] Executing command: %s", vcp_id, ct.value)

        try:
            if ct == CommandType.CONNECT:
                await runtime.connection.start()
                return {"ok": True}

            if ct == CommandType.DISCONNECT:
                await runtime.connection.stop()
                return {"ok": True}

            if ct == CommandType.RECONNECT:
                await runtime.connection.stop()
                await asyncio.sleep(0.5)
                await runtime.connection.start()
                return {"ok": True}

            if ct == CommandType.PLUG_EV:
                await runtime.behavior.plug_ev(
                    id_tag=p.get("id_tag", ""),
                    evse_id=p.get("evse_id", 1),
                    connector_id=p.get("connector_id", 1),
                )
                return {"ok": True}

            if ct == CommandType.UNPLUG_EV:
                await runtime.behavior.unplug_ev(
                    evse_id=p.get("evse_id", 1),
                    connector_id=p.get("connector_id", 1),
                )
                return {"ok": True}

            if ct == CommandType.AUTHORIZE:
                ok = await runtime.behavior.authorize(
                    id_tag=p.get("id_tag", "TAG-MANUAL"),
                    evse_id=p.get("evse_id", 1),
                    connector_id=p.get("connector_id", 1),
                )
                return {"ok": ok}

            if ct == CommandType.START_CHARGE:
                ok = await runtime.behavior.start_charge(
                    evse_id=p.get("evse_id", 1),
                    connector_id=p.get("connector_id", 1),
                )
                return {"ok": ok}

            if ct == CommandType.STOP_CHARGE:
                await runtime.behavior.stop_charge(
                    reason=p.get("reason", "Local"),
                    evse_id=p.get("evse_id", 1),
                    connector_id=p.get("connector_id", 1),
                )
                return {"ok": True}

            if ct == CommandType.INJECT_FAULT:
                fault_type = FaultType(p.get("fault_type", "network_disconnect"))
                result = await runtime.fault.inject(fault_type, p)
                if self._metrics:
                    self._metrics.on_fault_injected(fault_type.value)
                return {"ok": result.injected, "message": result.message}

            if ct == CommandType.RECOVER:
                await runtime.fault.recover()
                return {"ok": True}

            if ct == CommandType.RUN_SCENARIO:
                scenario_name = p.get("scenario", "")
                params = p.get("params", {})
                runtime.scenario.start_async(scenario_name, params)
                return {"ok": True, "scenario": scenario_name, "status": "started"}

            if ct == CommandType.STOP_SCENARIO:
                await runtime.scenario.stop_active()
                return {"ok": True}

            if ct == CommandType.SEND_METER_VALUES:
                await runtime.behavior._do_meter_values(
                    p.get("evse_id", 1), p.get("connector_id", 1)
                )
                return {"ok": True}

            if ct == CommandType.SEND_BOOT_NOTIFICATION:
                asyncio.create_task(runtime.behavior._boot_lifecycle())
                return {"ok": True}

            if ct == CommandType.GET_STATE:
                return {"ok": True, "state": runtime.state.snapshot()}

        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] Command %s failed: %s", vcp_id, ct.value, exc)
            return {"ok": False, "error": str(exc)}

        return {"ok": False, "error": f"Unknown command: {ct.value}"}

    # ------------------------------------------------------------------
    # Connection event callbacks
    # ------------------------------------------------------------------

    def _make_on_connected(self, vcp_id: str):
        async def _on_connected(_: str) -> None:
            runtime = self._runtimes.get(vcp_id)
            if runtime:
                await runtime.state.set_connection_state(VcpState.RECOVERING, "ws_connected")
                runtime.behavior.on_connected()
            if self._metrics:
                self._metrics.connections_established.inc()
        return _on_connected

    def _make_on_disconnected(self, vcp_id: str):
        async def _on_disconnected(_: str) -> None:
            runtime = self._runtimes.get(vcp_id)
            if runtime:
                runtime.router.cancel_all_pending("disconnected")
                runtime.timers.cancel_all()
                await runtime.state.set_connection_state(VcpState.OFFLINE, "ws_disconnected")
                # await self._registry.publish_state(vcp_id)
            if self._metrics:
                self._metrics.vcps_connected.dec()
        return _on_disconnected
