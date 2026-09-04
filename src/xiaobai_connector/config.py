"""Non-secret persistent Connector configuration."""

from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .paths import config_path, ensure_data_dir


DEFAULT_SERVER_URL = "wss://api.xiaobaizzf.com/agent/connect"
VALID_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")


def current_platform() -> str:
    value = platform.system().lower()
    return {"darwin": "macos", "windows": "windows"}.get(value, "linux")


@dataclass
class ConnectorConfig:
    server_url: str = field(default_factory=lambda: os.environ.get(
        "XIAOBAI_CONNECTOR_SERVER_URL", DEFAULT_SERVER_URL))
    device_id: str = ""
    device_name: str = field(default_factory=platform.node)
    platform: str = field(default_factory=current_platform)
    connector_version: str = __version__
    workdir: str = field(default_factory=lambda: str(Path.home()))
    sandbox: str = "workspace-write"
    agents: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> "ConnectorConfig":
        target = path or config_path()
        if not target.is_file():
            return cls()
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"无法读取 Connector 配置：{target}") from exc
        if not isinstance(value, dict):
            raise ValueError("Connector 配置必须是 JSON 对象")
        if any(key in value for key in ("token", "connector_token", "proof_secret")):
            raise ValueError("长期 token 和配对 proof 不能写入 config.json")
        sandbox = str(value.get("sandbox") or "workspace-write")
        if sandbox not in VALID_SANDBOXES:
            raise ValueError("sandbox 配置无效")
        agents = value.get("agents") or []
        if not isinstance(agents, list) or any(not isinstance(item, dict) for item in agents):
            raise ValueError("agents 必须是对象数组")
        return cls(
            server_url=str(value.get("server_url") or DEFAULT_SERVER_URL),
            device_id=str(value.get("device_id") or ""),
            device_name=str(value.get("device_name") or platform.node()),
            platform=str(value.get("platform") or current_platform()),
            connector_version=str(value.get("connector_version") or __version__),
            workdir=str(value.get("workdir") or Path.home()),
            sandbox=sandbox,
            agents=[dict(item) for item in agents],
        )

    def validate(self) -> None:
        if not self.server_url.startswith(("ws://", "wss://")):
            raise ValueError("server_url 必须是 ws:// 或 wss://")
        if self.device_id and not self.device_id.startswith("dev_"):
            raise ValueError("device_id 无效")
        if not self.device_name.strip():
            raise ValueError("设备名称不能为空")
        if self.sandbox not in VALID_SANDBOXES:
            raise ValueError("sandbox 配置无效")

    def save(self, path: Path | None = None) -> Path:
        self.validate()
        target = path or config_path()
        if path is None:
            ensure_data_dir()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "server_url": self.server_url,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "platform": self.platform,
            "connector_version": self.connector_version,
            "workdir": self.workdir,
            "sandbox": self.sandbox,
            "agents": self.agents,
        }
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
        try:
            target.chmod(0o600)
        except OSError:
            pass
        return target

    @property
    def selected_agents(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.agents if bool(item.get("enabled"))]
