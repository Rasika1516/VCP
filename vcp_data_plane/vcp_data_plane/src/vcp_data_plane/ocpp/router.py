"""OCPP message router and dispatcher.

Responsibilities:
  - Dispatch inbound CALL messages to registered action handlers.
  - Track in-flight outbound CALL messages and correlate incoming
    CALLRESULT / CALLERROR responses via message_id.
  - Enforce one concurrent outbound CALL at a time (OCPP requirement):
    a second CALL is queued until the first is resolved.
  - Apply a configurable request timeout; resolve the pending future with
    a CALLERROR if no response arrives in time.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine

from vcp_data_plane.ocpp.common import (
    ErrorCode,
    OcppCall,
    OcppCallError,
    OcppCallResult,
    PendingRequest,
    parse_message,
)

logger = logging.getLogger(__name__)

# Handler type: receives the incoming CALL, returns the response payload dict
HandlerFn = Callable[[OcppCall], Coroutine[Any, Any, dict[str, Any]]]


class OcppRouter:
    """Routes OCPP messages for a single VCP connection.

    Typically one OcppRouter per VCP instance.
    """

    def __init__(self, vcp_id: str, request_timeout_s: float = 30.0) -> None:
        self._vcp_id = vcp_id
        self._timeout = request_timeout_s
        # action → async handler
        self._handlers: dict[str, HandlerFn] = {}
        # In-flight outbound requests keyed by message_id
        self._pending: dict[str, PendingRequest] = {}
        # Serializes outbound CALLs (one outstanding at a time per OCPP spec)
        self._send_lock = asyncio.Lock()
        # Callback invoked with the serialised OCPP message string to transmit
        self._send_fn: Callable[[str], Coroutine[Any, Any, None]] | None = None

    def set_send_fn(self, fn: Callable[[str], Coroutine[Any, Any, None]]) -> None:
        """Register the coroutine that actually writes to the WebSocket."""
        self._send_fn = fn

    def register(self, action: str, handler: HandlerFn) -> None:
        """Register a handler for an inbound CSMS→CP CALL action."""
        self._handlers[action] = handler

    async def handle_inbound(self, raw: str) -> None:
        """Process a raw message received from the CSMS."""
        try:
            msg = parse_message(raw)
        except ValueError as exc:
            logger.warning("[%s] malformed OCPP message: %s", self._vcp_id, exc)
            return

        if isinstance(msg, OcppCall):
            await self._dispatch_call(msg)
        elif isinstance(msg, OcppCallResult):
            self._resolve_pending(msg.message_id, msg)
        elif isinstance(msg, OcppCallError):
            self._resolve_pending(msg.message_id, msg)

    async def _dispatch_call(self, call: OcppCall) -> None:
        """Find the registered handler for an inbound CALL and respond."""
        handler = self._handlers.get(call.action)
        if handler is None:
            logger.warning("[%s] no handler for action: %s", self._vcp_id, call.action)
            error = OcppCallError(
                message_id=call.message_id,
                error_code=ErrorCode.NOT_IMPLEMENTED,
                error_description=f"Action {call.action!r} not supported by this VCP",
            )
            await self._transmit(error.to_json())
            return

        try:
            result_payload = await handler(call)
            result = OcppCallResult(message_id=call.message_id, payload=result_payload)
            await self._transmit(result.to_json())
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] handler error for %s: %s", self._vcp_id, call.action, exc)
            error = OcppCallError(
                message_id=call.message_id,
                error_code=ErrorCode.INTERNAL_ERROR,
                error_description=str(exc),
            )
            await self._transmit(error.to_json())

    def _resolve_pending(self, message_id: str, response: OcppCallResult | OcppCallError) -> None:
        req = self._pending.pop(message_id, None)
        if req is None:
            logger.debug("[%s] received response for unknown message_id: %s", self._vcp_id, message_id)
            return
        req.resolve(response)

    async def call(
        self,
        action: str,
        payload: dict[str, Any],
    ) -> OcppCallResult | OcppCallError:
        """Send an outbound CALL and await the CSMS response.

        The send lock ensures only one CALL is outstanding at a time.
        Raises asyncio.TimeoutError if no response arrives within timeout.
        """
        if self._send_fn is None:
            raise RuntimeError("send_fn not set on OcppRouter")

        async with self._send_lock:
            call = OcppCall.new(action=action, payload=payload)
            req = PendingRequest(call)
            self._pending[call.message_id] = req

            await self._transmit(call.to_json())

            try:
                return await asyncio.wait_for(asyncio.shield(req.future), timeout=self._timeout)
            except asyncio.TimeoutError:
                self._pending.pop(call.message_id, None)
                req.cancel("timeout")
                logger.warning(
                    "[%s] CALL %s timed out after %.1fs",
                    self._vcp_id, action, self._timeout,
                )
                return OcppCallError(
                    message_id=call.message_id,
                    error_code=ErrorCode.GENERIC_ERROR,
                    error_description=f"CALL timeout: {action}",
                )

    async def _transmit(self, raw: str) -> None:
        if self._send_fn:
            await self._send_fn(raw)

    def cancel_all_pending(self, reason: str = "connection closed") -> None:
        """Cancel all in-flight requests (called on disconnect)."""
        for req in list(self._pending.values()):
            req.cancel(reason)
        self._pending.clear()
