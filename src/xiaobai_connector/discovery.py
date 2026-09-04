"""Conservative local Agent discovery.

Discovery is intentionally limited to known executable names and documented
configuration directories.  It does not inspect arbitrary processes or upload
local files.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

from .models import AgentCandidate


def _search_dirs() -> list[Path]:
    home = Path.home()
    values = [
        home / ".local" / "bin",
        home / ".cargo" / "bin",
        home / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    ]
    if platform.system() == "Windows":
        windows_values = [home / ".local" / "bin", home / "bin"]
        local_app_data = os.environ.get("LOCALAPPDATA")
        app_data = os.environ.get("APPDATA")
        if local_app_data:
            windows_values.append(Path(local_app_data) / "Programs")
        if app_data:
            windows_values.append(Path(app_data) / "npm")
        values = windows_values + values
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        if not str(value) or str(value) == ".":
            continue
        resolved = str(value.expanduser())
        if resolved not in seen:
            result.append(value.expanduser())
            seen.add(resolved)
    return result


def _which(names: tuple[str, ...]) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return str(Path(found).resolve())
    for directory in _search_dirs():
        for name in names:
            candidate = directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve())
            if platform.system() == "Windows":
                for suffix in (".exe", ".cmd", ".bat"):
                    candidate = directory / (name + suffix)
                    if candidate.is_file():
                        return str(candidate.resolve())
    return None


def _command(path: str, *args: str) -> list[str]:
    if platform.system() == "Windows" and Path(path).suffix.lower() in {".cmd", ".bat"}:
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", path, *args]
    return [path, *args]


def _version(path: str | None) -> str:
    if not path:
        return ""
    try:
        result = subprocess.run(_command(path, "--version"), capture_output=True,
                                text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    raw = (result.stdout or result.stderr or "").strip().splitlines()
    return raw[0][:160] if raw else ""


def _candidate(kind: str, name: str, names: tuple[str, ...], *,
               profile_dirs: tuple[Path, ...] = (),
               capabilities: tuple[str, ...] = ("chat", "stream")) -> AgentCandidate:
    executable = _which(names)
    configured = next((path for path in profile_dirs if path.expanduser().is_dir()), None)
    if executable:
        return AgentCandidate(
            kind=kind, display_name=name, executable=executable,
            version=_version(executable), status="online",
            detail="已找到命令行程序", local_ref=f"{kind}:default",
            selected=True, capabilities=capabilities)
    if configured:
        return AgentCandidate(
            kind=kind, display_name=name, executable=None, version="",
            status="configured", detail=f"已找到配置目录：{configured.expanduser()}",
            local_ref=f"{kind}:default", selected=False, capabilities=capabilities)
    return AgentCandidate(
        kind=kind, display_name=name, executable=None, status="not_found",
        detail="没有找到已知安装位置", local_ref=f"{kind}:default",
        selected=False, capabilities=capabilities)


def scan_agents() -> list[AgentCandidate]:
    home = Path.home()
    return [
        _candidate("codex", "Codex", ("codex",), capabilities=("chat", "stream", "cancel", "steer")),
        _candidate("claude", "Claude Code", ("claude",), capabilities=("chat", "stream", "cancel")),
        _candidate(
            "hermes", "Hermes", ("hermes", "hermes-cli"),
            profile_dirs=(home / ".hermes", home / ".xiaobai" / "hermes"),
            capabilities=("chat", "stream")),
    ]
