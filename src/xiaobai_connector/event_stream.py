"""Common event normalization for every local Agent adapter.

Adapters are allowed to emit small, implementation-specific events, but only
the safe run lifecycle and user-facing text are written to the durable spool.
Keeping this policy in one place also gives Hermes a stable message boundary
contract and prevents token-sized output from filling SQLite during a long run.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .models import Emit


MAX_OUTPUT_CHUNK = 8_000
OUTPUT_BATCH_SIZE = 512
MAX_PROGRESS_TEXT = 240
MAX_PROGRESS_FIELDS = frozenset({
    "phase", "message", "heartbeat_at", "elapsed_seconds", "step", "total",
    "warning", "notice",
})
STRING_PROGRESS_FIELDS = frozenset({
    "phase", "message", "heartbeat_at", "warning", "notice",
})
NUMBER_PROGRESS_FIELDS = frozenset({"elapsed_seconds", "step", "total"})
LIFECYCLE_EVENTS = frozenset({
    "run.accepted", "run.started", "run.awaiting_input", "run.awaiting_approval",
    "run.completed", "run.failed", "run.canceled",
})


def bounded_text(value: Any, limit: int = MAX_PROGRESS_TEXT) -> str:
    return (str(value or "").replace("\x00", " ").replace("\r", " ")
            .replace("\n", " ").strip()[:limit])


def normalize_progress(value: Any) -> dict[str, Any]:
    """Keep only small, user-facing progress fields."""
    raw = value.get("progress") if isinstance(value, dict) else None
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Any] = {}
    for key in MAX_PROGRESS_FIELDS:
        if key not in raw:
            continue
        if key in STRING_PROGRESS_FIELDS:
            text = bounded_text(raw[key])
            if text:
                result[key] = text
        elif key in NUMBER_PROGRESS_FIELDS:
            try:
                number = int(raw[key])
            except (TypeError, ValueError, OverflowError):
                continue
            if 0 <= number <= 2_147_483_647:
                result[key] = number
    if not any(result.get(key) for key in ("message", "warning", "notice")):
        result.pop("phase", None)
    return result


class ConnectorEventStream:
    """Normalize adapter events before they become durable outbox rows."""

    def __init__(self, emit: Emit, *, output_seq_start: int = 0):
        self._emit = emit
        self._output_seq = max(0, int(output_seq_start))
        self._lock = asyncio.Lock()
        self._output_buffer = ""
        self._buffer_message_start = False
        self._buffer_message_end = False

    @property
    def output_sequence(self) -> int:
        return self._output_seq

    async def _flush_output_locked(self) -> None:
        if not self._output_buffer:
            self._buffer_message_start = False
            self._buffer_message_end = False
            return
        text = self._output_buffer
        message_start = self._buffer_message_start
        message_end = self._buffer_message_end
        self._output_buffer = ""
        self._buffer_message_start = False
        self._buffer_message_end = False
        self._output_seq += 1
        payload: dict[str, Any] = {"seq": self._output_seq, "delta": text}
        if message_start:
            payload["message_start"] = True
        if message_end:
            payload["message_end"] = True
        await self._emit("run.output.delta", payload)

    async def _append_output_locked(self, text: str, *, message_start: bool,
                                    message_end: bool) -> None:
        if message_start and self._output_buffer:
            await self._flush_output_locked()
        if message_start:
            self._buffer_message_start = True
        self._output_buffer += text
        if message_end:
            self._buffer_message_end = True

        while len(self._output_buffer) >= OUTPUT_BATCH_SIZE:
            chunk = self._output_buffer[:OUTPUT_BATCH_SIZE]
            remainder = self._output_buffer[OUTPUT_BATCH_SIZE:]
            chunk_starts = self._buffer_message_start
            chunk_ends = self._buffer_message_end and not remainder
            self._output_buffer = remainder
            self._buffer_message_start = False
            if not remainder:
                self._buffer_message_end = False
            self._output_seq += 1
            payload: dict[str, Any] = {"seq": self._output_seq, "delta": chunk}
            if chunk_starts:
                payload["message_start"] = True
            if chunk_ends:
                payload["message_end"] = True
            await self._emit("run.output.delta", payload)

        if message_end:
            await self._flush_output_locked()

    async def __call__(self, event_type: str,
                       value: dict[str, Any] | None = None) -> Any:
        value = value if isinstance(value, dict) else {}
        async with self._lock:
            if event_type == "run.output.delta":
                raw_text = value.get("delta")
                if not isinstance(raw_text, str) or not raw_text.strip():
                    return None
                text = raw_text.replace("\x00", " ")[:MAX_OUTPUT_CHUNK]
                await self._append_output_locked(
                    text,
                    message_start=bool(value.get("message_start")),
                    message_end=bool(value.get("message_end")),
                )
                return None

            await self._flush_output_locked()
            if event_type == "run.progress":
                progress = normalize_progress(value)
                return await self._emit("run.progress", {"progress": progress}) if progress else None
            if event_type == "agent.message.send":
                return await self._emit(event_type, value)
            if event_type not in LIFECYCLE_EVENTS:
                return None
            if event_type in {"run.awaiting_input", "run.awaiting_approval"}:
                safe: dict[str, Any] = {}
                for key in ("request_id", "prompt", "question", "message", "title"):
                    if key in value:
                        text = bounded_text(value[key], 500)
                        if text:
                            safe[key] = text
                if event_type == "run.awaiting_approval":
                    approval = value.get("approval")
                    if isinstance(approval, dict):
                        safe_approval: dict[str, Any] = {}
                        for key in ("request_id", "title", "description"):
                            if key in approval:
                                text = bounded_text(approval[key], 500)
                                if text:
                                    safe_approval[key] = text
                        choices = approval.get("choices")
                        if isinstance(choices, list):
                            safe_approval["choices"] = [
                                bounded_text(item, 80) for item in choices[:8]
                                if bounded_text(item, 80)
                            ]
                        if safe_approval:
                            safe["approval"] = safe_approval
                return await self._emit(event_type, safe)
            safe = {}
            for key in ("code", "detail", "adapter_session_id", "adapter_turn_id"):
                if key in value:
                    text = bounded_text(value[key], 500)
                    if text:
                        safe[key] = text
            return await self._emit(event_type, safe)


__all__ = ["ConnectorEventStream", "normalize_progress"]
