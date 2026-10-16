"""Worker Event Publisher — writes worker events to a Redis Stream.

Consumed by vcp_control_plane bus/subscriber.py, which reads:
  vcp:events:worker:broadcast

Each XADD entry carries:
  event      — "vcp_registered" | "state_change" | "command_result"
  vcp_id     — VCP identifier
  worker_id  — publishing worker
  payload    — JSON-encoded event data
  ts         — ISO-8601 UTC timestamp
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis

from vcp_data_plane.config.schema import RedisConfig
from vcp_data_plane.state.machine import VcpState

logger = logging.getLogger(__name__)
# RNZ (2026-10-09): Added a dedicated logger for outgoing Redis events so every event the
# data plane publishes to the control plane is written to its own file (the "before" log).
# It is the counterpart of the control plane's "vcp_control_plane.redis_events" RECV log
# (the "after" log); the same Redis message id appears in both, so messages can be traced
# end to end.
event_log = logging.getLogger("vcp_data_plane.redis_events")

# RNZ (2026-10-09): Same line format as the control plane's redis_events.log.
_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


# RNZ (2026-10-09): Attaches a rotating file handler (10 MB x 5 files) to event_log.
# Called once from EventPublisher.start(); an empty log_file disables the file and the
# lines then only go to the console.
def _configure_event_log(log_file: str) -> None:
    if not log_file or event_log.handlers:
        return
    from logging.handlers import RotatingFileHandler
    from pathlib import Path

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    file_handler.setLevel(logging.INFO)
    event_log.addHandler(file_handler)  # still propagates to the console handler
    event_log.setLevel(logging.INFO)

# Control plane stores connection_state as Connected / Connecting / Disconnected
_CONNECTION_MAP = {
    VcpState.OFFLINE.value: "Disconnected",
    VcpState.RECOVERING.value: "Connecting",
}


class EventPublisher:
    """Publishes worker events to the control plane's event stream."""

    def __init__(
        self,
        worker_id: str,
        redis_config: RedisConfig,
        maxlen: int = 10_000,
        # RNZ (2026-10-09): New parameter - path of the outgoing-event log file
        # (WorkerConfig.redis_event_log_file). Empty string = no file.
        log_file: str = "",
    ) -> None:
        self._worker_id = worker_id
        self._cfg = redis_config
        self._stream = f"{redis_config.event_stream_prefix}:broadcast"
        self._maxlen = maxlen
        self._log_file = log_file
        self._redis: aioredis.Redis | None = None

    async def start(self) -> None:
        # RNZ (2026-10-09): Set up the outgoing-event log file before publishing starts.
        _configure_event_log(self._log_file)
        cfg = self._cfg
        self._redis = aioredis.Redis(
            host=cfg.host,
            port=cfg.port,
            db=cfg.db,
            password=cfg.password,
            ssl=cfg.ssl,
            decode_responses=True,
        )
        await self._redis.ping()
        logger.info("EventPublisher started — stream: %s", self._stream)

    async def stop(self) -> None:
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    async def publish_state_change(self, vcp_id: str, snapshot: dict[str, Any], reason: str = "") -> None:
        """Publish a VCP state snapshot in the shape the control plane persists."""
        connection = snapshot["connection"]
        connectors = snapshot["connectors"]
        primary = next(iter(connectors.values()), {})
        if connection == VcpState.OFFLINE.value:
            actual_state = VcpState.OFFLINE.value
        else:
            actual_state = primary.get("state", connection)

        await self._publish("state_change", vcp_id, {
            "actual_state": actual_state,
            "connection_state": _CONNECTION_MAP.get(connection, "Connected"),
            "connector_state": connectors,
            "reason": reason,
        })

    async def publish_vcp_registered(self, vcp_id: str, registration: dict[str, Any]) -> None:
        """Announce a VCP this worker owns so the control plane can create its DB row."""
        await self._publish("vcp_registered", vcp_id, registration)

    async def publish_command_result(self, vcp_id: str, command: Any, result: dict[str, Any]) -> None:
        await self._publish("command_result", vcp_id, {
            "command_id": command.command_id,
            "command_type": command.command_type.value,
            "issued_by": command.issued_by,
            "result": result,
        })

    async def _publish(self, event: str, vcp_id: str, payload: dict[str, Any]) -> None:
        # RNZ (2026-10-09): Reworked to log every publish attempt to event_log:
        #   SEND - XADD succeeded; includes the Redis message id and all fields
        #   FAIL - XADD raised an error (previously only logged at DEBUG, so hidden)
        #   DROP - publisher not started, event discarded (previously silent)
        # The fields dict is built first so the same content is both sent and logged.
        fields = {
            "event": event,
            "vcp_id": vcp_id,
            "worker_id": self._worker_id,
            "payload": json.dumps(payload, default=str),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if self._redis is None:
            event_log.warning(
                "DROP stream=%s (publisher not started) fields=%s",
                self._stream, json.dumps(fields, ensure_ascii=False),
            )
            return
        try:
            msg_id = await self._redis.xadd(
                self._stream,
                fields,
                maxlen=self._maxlen,
                approximate=True,
            )
        except Exception as exc:  # noqa: BLE001
            event_log.warning(
                "FAIL stream=%s error=%s fields=%s",
                self._stream, exc, json.dumps(fields, ensure_ascii=False),
            )
            return
        # RNZ (2026-10-09): msg_id matches the "id=" on the control plane's RECV line for
        # this message, so a SEND line can be matched to its RECV line.
        event_log.info(
            "SEND stream=%s id=%s fields=%s",
            self._stream, msg_id, json.dumps(fields, ensure_ascii=False),
        )
