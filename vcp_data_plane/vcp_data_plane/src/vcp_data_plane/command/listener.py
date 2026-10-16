"""Redis Streams Command Listener.

Consumes WorkerCommand messages from two Redis Streams:
  1. vcp:commands:worker:{worker_id}   — per-worker commands (VCP-specific)
  2. vcp:commands:broadcast            — fleet/group broadcast commands

Architecture rationale for Redis Streams (vs Kafka):
  - At 10k+ VCPs across ~20 workers, per-worker streams give each worker its
    own consumer group; commands are delivered exactly once via XACK.
  - Commands are typically low volume (<1000/s peak); Redis Streams trivially
    handles this.  Kafka's overhead (broker cluster, topic partitions, consumer
    rebalancing) is not justified at this scale.
  - Redis also serves VCP state caching, making it a single dependency rather
    than two.
  - Pending-Entry List (PEL) and XAUTOCLAIM enable automatic re-delivery of
    commands that were consumed but not ACK'd (e.g. due to worker restart).

Stream message format — two layouts are accepted:

  Flat fields (written by vcp_control_plane bus/publisher.py):
    command_id    — UUID string
    command_type  — CommandType value
    vcp_id        — target VCP ID (empty string for broadcast/group)
    payload       — JSON-encoded dict
    issued_by     — requester identifier
    group_id      — (optional) group-scoped broadcast
    ts            — ISO-8601 UTC timestamp

  Legacy single field (written by CommandPublisher below):
    command_json  — JSON-serialised WorkerCommand

Consumer groups are created automatically on worker start if absent.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine

import redis.asyncio as aioredis

from vcp_data_plane.config.schema import CommandType, RedisConfig, WorkerCommand

logger = logging.getLogger(__name__)

# Dispatch callback: receives a WorkerCommand; returns response dict (ack'd payload)
CommandHandler = Callable[[WorkerCommand], Coroutine[Any, Any, dict[str, Any]]]


def parse_command_fields(fields: dict[str, str]) -> WorkerCommand | None:
    """Build a WorkerCommand from a stream entry's fields.

    Returns None when the entry carries no command at all.
    Raises ValueError / ValidationError when the entry is malformed.
    """
    raw_json = fields.get("command_json")
    if raw_json:
        return WorkerCommand.model_validate(json.loads(raw_json))

    if not fields.get("command_type"):
        return None

    raw_payload = fields.get("payload") or "{}"
    payload = json.loads(raw_payload)
    if not isinstance(payload, dict):
        raise ValueError(f"payload must be a JSON object, got {type(payload).__name__}")

    return WorkerCommand.model_validate({
        "command_id": fields.get("command_id", ""),
        "command_type": fields["command_type"],
        # Control plane sends "" for "not set"; the worker treats None as broadcast
        "vcp_id": fields.get("vcp_id") or None,
        "group_id": fields.get("group_id") or None,
        "payload": payload,
        "issued_by": fields.get("issued_by", ""),
        "issued_at": fields.get("ts", ""),
    })


class CommandListener:
    """Listens to Redis Streams and dispatches commands to registered handlers."""

    def __init__(self, worker_id: str, redis_config: RedisConfig) -> None:
        self._worker_id = worker_id
        self._cfg = redis_config
        self._redis: aioredis.Redis | None = None
        self._handlers: dict[CommandType, CommandHandler] = {}
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []
        self._consumer_name = f"consumer-{worker_id}"

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def register(self, command_type: CommandType, handler: CommandHandler) -> None:
        """Register an async handler for a command type."""
        self._handlers[command_type] = handler

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
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

        worker_stream = f"{cfg.command_stream_prefix}:{self._worker_id}"
        broadcast_stream = cfg.broadcast_stream
        # Consumers in one group split the messages between them, so a shared group
        # on the broadcast stream would hand each broadcast to only one worker.
        # Each worker gets its own group there so every worker sees every broadcast.
        broadcast_group = f"{cfg.consumer_group}:{self._worker_id}"

        # Create consumer groups if they don't exist
        await self._ensure_group(worker_stream, cfg.consumer_group)
        # "$": a newly created broadcast group must not replay old fleet-wide commands
        await self._ensure_group(broadcast_stream, broadcast_group, start_id="$")

        self._running = True
        # Start separate reader tasks for each stream
        self._tasks = [
            asyncio.create_task(
                self._read_loop(worker_stream, cfg.consumer_group),
                name=f"cmd-worker-{self._worker_id}",
            ),
            asyncio.create_task(
                self._read_loop(broadcast_stream, broadcast_group),
                name=f"cmd-broadcast-{self._worker_id}",
            ),
        ]
        logger.info("CommandListener started — streams: %s, %s", worker_stream, broadcast_stream)

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._redis:
            await self._redis.aclose()
        logger.info("CommandListener stopped")

    # ------------------------------------------------------------------
    # Stream read loop
    # ------------------------------------------------------------------

    async def _read_loop(self, stream: str, group: str) -> None:
        """Blocking read loop for a single Redis Stream consumer group."""
        cfg = self._cfg
        block_ms = cfg.stream_block_ms

        while self._running:
            try:
                # XREADGROUP: block until a message arrives (or timeout)
                entries = await self._redis.xreadgroup(
                    groupname=group,
                    consumername=self._consumer_name,
                    streams={stream: ">"},
                    count=10,               # Process up to 10 commands per iteration
                    block=block_ms,
                )

                if not entries:
                    continue

                for stream_key, messages in entries:
                    for msg_id, fields in messages:
                        await self._handle_entry(stream_key, group, msg_id, fields)

                # Also claim timed-out pending entries (PEL) to handle crashed workers
                await self._reclaim_pending(stream, group)

            except asyncio.CancelledError:
                break
            except aioredis.ConnectionError as exc:
                logger.error("Redis connection error in command listener: %s", exc)
                await asyncio.sleep(2)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Command listener error: %s", exc)
                await asyncio.sleep(1)

    async def _handle_entry(self, stream: str, group: str, msg_id: str, fields: dict[str, str]) -> None:
        """Parse and dispatch a single stream entry."""
        try:
            command = parse_command_fields(fields)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Malformed command in stream %s msg_id=%s fields=%s: %s",
                stream, msg_id, fields, exc,
            )
            await self._ack(stream, group, msg_id)
            return

        if command is None:
            logger.warning("Stream %s msg_id=%s has no command fields — ACK'd without action: %s",
                           stream, msg_id, fields)
            await self._ack(stream, group, msg_id)
            return

        logger.info(
            "Command received %s [%s] vcp=%s from %s",
            command.command_type.value, command.command_id, command.vcp_id or "*", command.issued_by or "?",
        )

        handler = self._handlers.get(command.command_type)
        if handler is None:
            logger.warning("No handler for command_type=%s — ACK'd without action", command.command_type)
            await self._ack(stream, group, msg_id)
            return

        try:
            result = await handler(command)
            logger.debug(
                "Command %s [%s] dispatched → %s",
                command.command_type, command.command_id, result,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Command handler error for %s [%s]: %s",
                command.command_type, command.command_id, exc,
            )
        finally:
            # Always ACK — we don't want dead commands to block the stream
            await self._ack(stream, group, msg_id)

    async def _ack(self, stream: str, group: str, msg_id: str) -> None:
        try:
            await self._redis.xack(stream, group, msg_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("XACK failed for %s: %s", msg_id, exc)

    async def _reclaim_pending(self, stream: str, group: str) -> None:
        """Auto-claim pending messages older than 30s (worker that consumed them may have died)."""
        try:
            # XAUTOCLAIM: steal messages pending > 30000ms
            claimed = await self._redis.xautoclaim(
                name=stream,
                groupname=group,
                consumername=self._consumer_name,
                min_idle_time=30_000,
                start_id="0-0",
                count=5,
            )
            messages = claimed[1] if isinstance(claimed, (list, tuple)) and len(claimed) > 1 else []
            for msg_id, fields in messages:
                logger.info("Reclaimed pending message %s from stream %s", msg_id, stream)
                await self._handle_entry(stream, group, msg_id, fields)
        except Exception:  # noqa: BLE001
            pass  # XAUTOCLAIM not available on older Redis; silently skip

    async def _ensure_group(self, stream: str, group: str, start_id: str = "0") -> None:
        """Create consumer group and stream if they don't exist."""
        try:
            await self._redis.xgroup_create(stream, group, id=start_id, mkstream=True)
            logger.info("Created consumer group '%s' on stream '%s'", group, stream)
        except aioredis.ResponseError as exc:
            if "BUSYGROUP" in str(exc):
                pass  # Group already exists — normal
            elif "ERR The XGROUP subcommand requires the key to exist" in str(exc):
                # Stream doesn't exist yet, mkstream handles it
                pass
            else:
                raise


class CommandPublisher:
    """Publishes WorkerCommands to Redis Streams (used by control plane / API)."""

    def __init__(self, redis_config: RedisConfig) -> None:
        self._cfg = redis_config
        self._redis: aioredis.Redis | None = None

    async def start(self) -> None:
        cfg = self._cfg
        self._redis = aioredis.Redis(
            host=cfg.host,
            port=cfg.port,
            db=cfg.db,
            password=cfg.password,
            ssl=cfg.ssl,
            decode_responses=True,
        )

    async def stop(self) -> None:
        if self._redis:
            await self._redis.aclose()

    async def publish(self, worker_id: str, command: WorkerCommand) -> str:
        """Publish a command to a specific worker's stream. Returns stream message ID."""
        stream = f"{self._cfg.command_stream_prefix}:{worker_id}"
        msg_id = await self._redis.xadd(
            stream,
            {"command_json": command.model_dump_json()},
            maxlen=10_000,      # Trim stream to avoid unbounded growth
            approximate=True,
        )
        logger.debug("Published command %s to %s → msg_id=%s", command.command_type, stream, msg_id)
        return msg_id

    async def broadcast(self, command: WorkerCommand) -> str:
        """Publish a command to the broadcast stream (all workers consume it)."""
        msg_id = await self._redis.xadd(
            self._cfg.broadcast_stream,
            {"command_json": command.model_dump_json()},
            maxlen=5_000,
            approximate=True,
        )
        logger.debug("Broadcast command %s → msg_id=%s", command.command_type, msg_id)
        return msg_id
