"""Read-only Codex usage-limit projection."""

from __future__ import annotations

from typing import Any

from ..codex_sessions import CodexSessionClient


class CodexUsageExtensionAdapter:
    extension_id = "codex_usage_limits_v1"

    def __init__(self, client: CodexSessionClient | None = None,
                 *, binary: str | None = None) -> None:
        self.client = client or CodexSessionClient(binary=binary)

    def read(self) -> dict[str, Any]:
        wire = self.client.rate_limits()
        snapshot = self._snapshot(wire)
        limits = []
        for scope in ("primary", "secondary"):
            value = self._window(snapshot.get(scope), scope=scope)
            if value is not None:
                limits.append(value)
        return {"limits": limits, "plan_type": self._optional_string(snapshot.get("planType"))}

    @staticmethod
    def _snapshot(wire: Any) -> dict[str, Any]:
        if not isinstance(wire, dict):
            return {}
        buckets = wire.get("rateLimitsByLimitId")
        if isinstance(buckets, dict) and isinstance(buckets.get("codex"), dict):
            return dict(buckets["codex"])
        snapshot = wire.get("rateLimits")
        return dict(snapshot) if isinstance(snapshot, dict) else {}

    @classmethod
    def _window(cls, value: Any, *, scope: str) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        try:
            used = min(max(int(value["usedPercent"]), 0), 100)
        except (KeyError, TypeError, ValueError):
            return None
        return {
            "scope": scope,
            "window_duration_minutes": cls._optional_int(value.get("windowDurationMins")),
            "used_percent": used,
            "remaining_percent": 100 - used,
            "resets_at": cls._optional_int(value.get("resetsAt")),
        }

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            value = int(value)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value or None


__all__ = ["CodexUsageExtensionAdapter"]
