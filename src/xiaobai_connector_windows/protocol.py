"""Strict JSON envelope implementation for Agent Gateway protocol v1."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


VERSION = 1
MAX_MESSAGE_BYTES = 1_048_576
FIELDS = {"v", "type", "event_id", "connection_id", "device_id", "sent_at", "payload"}
TYPE_RE = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9_]*)*$")
EVENT_RE = re.compile(r"^evt_[A-Za-z0-9._:-]{8,128}$")
DEVICE_RE = re.compile(r"^dev_[A-Za-z0-9._:-]{8,128}$")
CONNECTION_RE = re.compile(r"^conn_[A-Za-z0-9._:-]{8,128}$")


class ProtocolError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Envelope:
    type: str
    event_id: str
    connection_id: str | None
    device_id: str
    sent_at: str
    payload: dict[str, Any]
    v: int = VERSION

    def as_dict(self) -> dict[str, Any]:
        return {"v": self.v, "type": self.type, "event_id": self.event_id,
                "connection_id": self.connection_id, "device_id": self.device_id,
                "sent_at": self.sent_at, "payload": self.payload}

    def dumps(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def decode(raw: str | bytes) -> Envelope:
    if isinstance(raw, bytes):
        raise ProtocolError("只接受 WebSocket 文本帧")
    if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ProtocolError("消息超过大小上限")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ProtocolError("消息不是有效 JSON") from exc
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ProtocolError("envelope 字段不完整或含未知字段")
    if type(value["v"]) is not int or value["v"] != VERSION:
        raise ProtocolError("只支持协议 v1")
    event_type = value["type"]
    event_id = value["event_id"]
    device_id = value["device_id"]
    connection_id = value["connection_id"]
    if not isinstance(event_type, str) or not TYPE_RE.fullmatch(event_type):
        raise ProtocolError("type 无效")
    if not isinstance(event_id, str) or not EVENT_RE.fullmatch(event_id):
        raise ProtocolError("event_id 无效")
    if not isinstance(device_id, str) or not DEVICE_RE.fullmatch(device_id):
        raise ProtocolError("device_id 无效")
    if connection_id is not None and (not isinstance(connection_id, str)
                                      or not CONNECTION_RE.fullmatch(connection_id)):
        raise ProtocolError("connection_id 无效")
    sent_at = value["sent_at"]
    if not isinstance(sent_at, str):
        raise ProtocolError("sent_at 无效")
    try:
        parsed = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolError("sent_at 无效") from exc
    if parsed.tzinfo is None or not isinstance(value["payload"], dict):
        raise ProtocolError("sent_at 必须带 UTC 时区，payload 必须是对象")
    return Envelope(event_type, event_id, connection_id, device_id, sent_at, value["payload"])


def make(event_type: str, device_id: str, connection_id: str | None,
         payload: dict[str, Any], *, event_id: str | None = None) -> Envelope:
    return Envelope(event_type, event_id or "evt_" + uuid.uuid4().hex,
                    connection_id, device_id, _utc_now(), payload)


def ack(source: Envelope, *, accepted: bool, error: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None) -> Envelope:
    payload: dict[str, Any] = {"accepted": accepted, "error": error}
    if extra:
        payload.update(extra)
    return make("ack", source.device_id, source.connection_id, payload,
                event_id=source.event_id)
