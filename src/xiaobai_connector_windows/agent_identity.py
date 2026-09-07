"""Agent identity and UI contracts shared with the Xiaobai app.

The mobile client treats ``avatar`` and ``presentation`` as part of an Agent's
public snapshot.  Keeping the defaults here prevents a standalone Connector
from silently falling back to the generic computer avatar when it discovers
an Agent without a local voice-repository pack.
"""

from __future__ import annotations

import base64
from copy import deepcopy
from pathlib import Path
from typing import Any


CODEX_AVATAR: dict[str, Any] = {
    "kind": "bundled_asset",
    "asset": "AgentCodexAvatar",
    "fallback": "💻",
    "source": "xiaobai/robots/codex/avatar.png",
}

CODEX_PRESENTATION: dict[str, Any] = {
    "schema_version": 1,
    "surface": "standard_chat_v1",
    "message_format": "markdown",
    "show_avatars": True,
    "composer": {
        "text": True,
        "voice": False,
        "attachments": True,
        "agent_picker": False,
    },
    "extensions": [
        {
            "id": "codex_sessions_v1",
            "features": [
                "desktop_thread_history",
                "new_thread",
                "model_picker",
                "reasoning_effort_picker",
            ],
        },
        {
            "id": "codex_usage_limits_v1",
            "features": [
                "account_rate_limits",
                "five_hour_window",
                "weekly_window",
            ],
        },
    ],
}

HERMES_PRESENTATION: dict[str, Any] = {
    "schema_version": 1,
    "surface": "standard_chat_v1",
    "message_format": "markdown",
    "show_avatars": True,
    "composer": {
        "text": True,
        "voice": False,
        "attachments": True,
        "agent_picker": False,
    },
    "extensions": [],
}

HERMES_AVATAR_ASSET = (
    Path(__file__).resolve().parent / "assets" / "hermes-desktop-avatar.png"
)


def clone(value: dict[str, Any]) -> dict[str, Any]:
    """Return an independent metadata object for a frozen LocalAgent."""
    return deepcopy(value)


def hermes_avatar() -> dict[str, Any]:
    """Return the portable Hermes Desktop female avatar when bundled."""
    try:
        payload = HERMES_AVATAR_ASSET.read_bytes()
    except OSError:
        payload = b""
    if payload and len(payload) <= 96 * 1024:
        return {
            "kind": "data_url",
            "data_url": "data:image/png;base64," + base64.b64encode(payload).decode("ascii"),
            "shape": "circle",
            "fallback": "🪽",
        }
    return {"kind": "emoji", "fallback": "🪽"}
