"""Event Subscriber — reads VCP state events from Redis Streams.

Reads from:
  vcp:events:worker:{worker_id}   — one stream per worker URL

The subscriber runs as a background asyncio task (started in app lifespan).
For each event it:
  1. Parses the event payload
  2. Creates the VCP registry row in PostgreSQL on "vcp_registered", and
     updates connection/actual state on "state_change"
  3. Broadcasts the event frame to all connected WebSocket clients via
     the events router's broadcast_event() function

Worker event message fields (all strings):
  event         — event type: "state_change" | "meter_value" | "ocpp_trace" | ...
  vcp_id        — VCP identifier
  worker_id     — publishing worker
  payload       — JSON-encoded event data
  ts            — ISO-8601 timestamp
"""
from __future__ import annotations

import asyncio
import json
import logging

import redis.asyncio as aioredis

from vcp_control_plane.config.settings import Settings

logger = logging.getLogger(__name__)
# Raw Redis traffic goes to its own logger so it can be written to a separate file
event_log = logging.getLogger("vcp_control_plane.redis_events")

# How long XREADGROUP blocks waiting for new messages (ms)
_BLOCK_MS = 2_000
# How many messages to fetch per read
_COUNT = 100
# Retry delay on Redis error
_RETRY_DELAY_S = 5.0


class EventSubscriber:
    """Async background task that tails worker event streams."""

    def __init__(self, redis_client: aioredis.Redis, settings: Settings) -> None:
        self._redis = redis_client
        self._settings = settings
        self._running = False

    def _event_streams(self) -> list[str]:
        """Derive event stream keys from configured worker URLs.

        Convention: strip the URL to the last path component or use the index.
        The data plane publishes on vcp:events:worker:{worker_id}; the worker_id
        is typically the worker's own identifier registered in the VCP instance.
        We also subscribe to a single aggregate stream if configured.
        """
        prefix = self._settings.event_stream_prefix
        # Build one stream name per worker URL (use index as fallback ID)
        streams = []
        for i, url in enumerate(self._settings.worker_urls):
            # Derive a short worker id from the URL host:port portion
            try:
                from urllib.parse import urlparse
                parsed = urlparse(url)
                worker_slug = (parsed.hostname or "worker") + (
                    f"_{parsed.port}" if parsed.port else ""
                )
            except Exception:
                worker_slug = f"worker_{i}"
            streams.append(f"{prefix}:{worker_slug}")

        # Always subscribe to a generic broadcast event stream too
        streams.append(f"{prefix}:broadcast")
        return list(dict.fromkeys(streams))  # deduplicate while preserving order

    async def _ensure_consumer_groups(self, streams: list[str]) -> None:
        group = self._settings.consumer_group
        for stream in streams:
            try:
                # id="0": a new group also reads events published before the control
                # plane started (e.g. vcp_registered from workers that booted first).
                await self._redis.xgroup_create(stream, group, id="0", mkstream=True)
                logger.info("Created consumer group '%s' on stream '%s'", group, stream)
            except aioredis.ResponseError as exc:
                if "BUSYGROUP" in str(exc):
                    pass  # Already exists — fine
                else:
                    logger.warning("xgroup_create error on %s: %s", stream, exc)

    async def run(self) -> None:
        """Main loop: read events, update DB, forward to WebSocket clients."""
        self._running = True
        streams = self._event_streams()
        group = self._settings.consumer_group
        consumer = self._settings.consumer_name

        logger.info(
            "EventSubscriber starting. Streams: %s  Group: %s  Consumer: %s",
            streams,
            group,
            consumer,
        )

        await self._ensure_consumer_groups(streams)

        # Build the stream-id mapping for XREADGROUP: {stream: ">"}
        stream_ids = {s: ">" for s in streams}

        while self._running:
            try:
                results = await self._redis.xreadgroup(
                    groupname=group,
                    consumername=consumer,
                    streams=stream_ids,
                    count=_COUNT,
                    block=_BLOCK_MS,
                )
            except asyncio.CancelledError:
                logger.info("EventSubscriber cancelled")
                return
            except Exception as exc:
                logger.error("EventSubscriber Redis error: %s — retrying in %ss", exc, _RETRY_DELAY_S)
                await asyncio.sleep(_RETRY_DELAY_S)
                continue

            if not results:
                continue

            for stream_name, messages in results:
                event_log.debug("Read %d message(s) from %s", len(messages), stream_name)
                for msg_id, fields in messages:
                    await self._handle_message(stream_name, msg_id, fields, group)

    async def _handle_message(
        self,
        stream: str,
        msg_id: str,
        fields: dict,
        group: str,
    ) -> None:
        """Process a single event message."""
        event_log.info(
            "RECV stream=%s id=%s fields=%s",
            stream,
            msg_id,
            json.dumps(fields, ensure_ascii=False, default=str),
        )
        try:
            event_type = fields.get("event", "unknown")
            vcp_id = fields.get("vcp_id", "")
            worker_id = fields.get("worker_id", "")
            ts = fields.get("ts", "")

            try:
                payload = json.loads(fields.get("payload", "{}"))
            except json.JSONDecodeError as exc:
                event_log.warning(
                    "Invalid JSON payload on %s id=%s: %s — raw=%r",
                    stream, msg_id, exc, fields.get("payload"),
                )
                payload = {}

            if not vcp_id:
                event_log.warning("Message %s on %s has no vcp_id", msg_id, stream)

            frame = {
                "event": event_type,
                "vcpId": vcp_id,
                "workerId": worker_id,
                "payload": payload,
                "ts": ts,
            }

            # Create / refresh the registry row for VCPs a worker announces
            if event_type == "vcp_registered" and vcp_id:
                await self._apply_registration(vcp_id, worker_id, payload)

            # Update DB state for state_change events
            if event_type == "state_change" and vcp_id:
                await self._apply_state_change(vcp_id, worker_id, payload)

            # Forward to WebSocket clients
            from vcp_control_plane.api.routers.events import broadcast_event
            await broadcast_event(frame)

            # Acknowledge message
            await self._redis.xack(stream, group, msg_id)
            event_log.debug("ACK stream=%s id=%s event=%s vcp=%s", stream, msg_id, event_type, vcp_id)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Error handling msg %s on %s: %s", msg_id, stream, exc, exc_info=True
            )
            # Still ack to avoid redelivery loops on persistent parse errors
            try:
                await self._redis.xack(stream, group, msg_id)
            except Exception:
                pass

    async def _apply_registration(
        self,
        vcp_id: str,
        worker_id: str,
        payload: dict,
    ) -> None:
        """Insert or refresh the VCP registry row from a worker's vcp_registered event."""
        charge_point_id = payload.get("charge_point_id")
        if not charge_point_id:
            event_log.warning("vcp_registered for %s has no charge_point_id — skipped", vcp_id)
            return
        try:
            from vcp_control_plane.db.base import get_db_session
            from vcp_control_plane.registry.store import VcpRegistryStore

            async for session in get_db_session():
                store = VcpRegistryStore(session)
                created = await store.upsert_vcp_registration(
                    vcp_id=vcp_id,
                    charge_point_id=charge_point_id,
                    worker_id=worker_id or None,
                    profile_id=payload.get("profile_id") or "",
                    profile_version=payload.get("profile_version") or "latest",
                    ocpp_version=payload.get("ocpp_version") or "1.6J",
                    firmware=payload.get("firmware"),
                    security_profile=int(payload.get("security_profile") or 0),
                )
                break
            event_log.info(
                "DB %s vcp_instance for %s: cp=%s worker=%s",
                "registered" if created else "refreshed",
                vcp_id,
                charge_point_id,
                worker_id,
            )
        except Exception as exc:
            logger.error("Failed to register VCP %s: %s", vcp_id, exc, exc_info=True)

    async def _apply_state_change(
        self,
        vcp_id: str,
        worker_id: str,
        payload: dict,
    ) -> None:
        """Persist state updates to the VCP registry."""
        try:
            from vcp_control_plane.db.base import get_db_session
            from vcp_control_plane.registry.store import VcpRegistryStore

            async for session in get_db_session():
                store = VcpRegistryStore(session)
                rows = await store.update_vcp_state(
                    vcp_id=vcp_id,
                    actual_state=payload.get("actual_state"),
                    connection_state=payload.get("connection_state"),
                    connector_state=payload.get("connector_state"),
                    worker_id=worker_id or None,
                )
                break  # single iteration — session is committed inside update_vcp_state
            if rows:
                event_log.info(
                    "DB updated vcp_instance for %s: actual=%s connection=%s worker=%s",
                    vcp_id,
                    payload.get("actual_state"),
                    payload.get("connection_state"),
                    worker_id,
                )
            else:
                event_log.warning(
                    "DB NOT updated: no vcp_instance row with vcp_id=%s (state_change dropped)",
                    vcp_id,
                )
        except Exception as exc:
            logger.error("Failed to persist state for VCP %s: %s", vcp_id, exc, exc_info=True)
