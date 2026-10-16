"""Command Publisher — writes WorkerCommand messages to Redis Streams.

Stream key conventions (must match vcp_data_plane exactly):

  Per-worker:   vcp:commands:worker:{worker_id}
  Broadcast:    vcp:commands:broadcast

Each XADD entry carries the following fields (all string-encoded JSON where
needed):

  command_id    — UUID string
  command_type  — e.g. "run_scenario", "reset", "change_state"
  vcp_id        — target VCP ID (empty string for broadcast/group)
  payload       — JSON-encoded dict
  issued_by     — requester identifier
  group_id      — (optional) for group-scoped broadcasts
  ts            — ISO-8601 UTC timestamp
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis

from vcp_control_plane.config.settings import Settings

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CommandPublisher:
    def __init__(self, redis_client: aioredis.Redis, settings: Settings) -> None:
        self._redis = redis_client
        self._settings = settings

    def _worker_stream(self, worker_id: str) -> str:
        return f"{self._settings.command_stream_prefix}:{worker_id}"

    @property
    def _broadcast_stream(self) -> str:
        return self._settings.broadcast_stream

    async def send_to_vcp(
        self,
        vcp_id: str,
        worker_id: str | None,
        command_type: str,
        payload: dict,
        command_id: str,
        issued_by: str | None = None,
        group_id: str | None = None,
    ) -> str:
        """Publish a command targeting a specific VCP.

        If worker_id is known, the command goes to the worker's dedicated stream.
        If not, it falls through to the broadcast stream (workers filter by vcp_id).
        """
        fields = {
            "command_id": command_id,
            "command_type": command_type,
            "vcp_id": vcp_id,
            "payload": json.dumps(payload),
            "issued_by": issued_by or "",
            "group_id": group_id or "",
            "ts": _now_iso(),
        }

        if worker_id:
            stream = self._worker_stream(worker_id)
        else:
            stream = self._broadcast_stream

        msg_id = await self._redis.xadd(stream, fields, maxlen=10_000, approximate=True)
        logger.debug(
            "Published command %s (%s) → %s [msg=%s]",
            command_id,
            command_type,
            stream,
            msg_id,
        )
        return msg_id

    async def broadcast(
        self,
        command_type: str,
        payload: dict,
        command_id: str,
        issued_by: str | None = None,
        group_id: str | None = None,
    ) -> str:
        """Publish a fleet-wide or group-scoped broadcast command."""
        fields = {
            "command_id": command_id,
            "command_type": command_type,
            "vcp_id": "",
            "payload": json.dumps(payload),
            "issued_by": issued_by or "",
            "group_id": group_id or "",
            "ts": _now_iso(),
        }
        msg_id = await self._redis.xadd(
            self._broadcast_stream, fields, maxlen=10_000, approximate=True
        )
        logger.debug(
            "Broadcast command %s (%s) → %s [msg=%s]",
            command_id,
            command_type,
            self._broadcast_stream,
            msg_id,
        )
        return msg_id
