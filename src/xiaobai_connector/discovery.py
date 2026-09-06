"""Conservative local Agent discovery.

Discovery is intentionally limited to known executable names and documented
configuration directories.  A user can also provide an explicit executable
path; it is checked locally and never uploaded by this module.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

from .models import AgentCandidate


AGENT_DISPLAY_NAMES = {
    "codex": "Codex",
    "claude": "Claude Code",
    "hermes": "Hermes",
}
WINDOWS_EXECUTABLE_SUFFIXES = {".exe", ".cmd", ".bat", ".com"}


CHATGPT_CODEX_BINARY = Path(
    "/Applications/ChatGPT.app/Contents/Resources/codex")


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
        windows_values = [
            home / ".local" / "bin",
            home / ".cargo" / "bin",
            home / "bin",
        ]
        local_app_data = os.environ.get("LOCALAPPDATA")
        app_data = os.environ.get("APPDATA")
        program_files = os.environ.get("ProgramFiles")
        program_files_x86 = os.environ.get("ProgramFiles(x86)")
        program_w6432 = os.environ.get("ProgramW6432")
        if local_app_data:
            windows_values.extend([
                Path(local_app_data) / "Programs",
                Path(local_app_data) / "Programs" / "Claude",
                Path(local_app_data) / "Programs" / "Codex",
                Path(local_app_data) / "npm",
            ])
        if app_data:
            windows_values.extend([
                Path(app_data) / "npm",
                Path(app_data) / "Programs",
            ])
        for value in (program_w6432, program_files, program_files_x86):
            if value:
                windows_values.extend([
                    Path(value) / "nodejs",
                    Path(value) / "Claude",
                    Path(value) / "Codex",
                ])
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
            found_path = Path(found).resolve()
            if _is_usable_executable(found_path):
                return str(found_path)
    for directory in _search_dirs():
        for name in names:
            candidate = directory / name
            if candidate.is_file() and _is_usable_executable(candidate):
                return str(candidate.resolve())
            if platform.system() == "Windows":
                for suffix in WINDOWS_EXECUTABLE_SUFFIXES:
                    candidate = directory / (name + suffix)
                    if candidate.is_file():
                        return str(candidate.resolve())
    return None


def _is_usable_executable(path: Path) -> bool:
    """Return whether *path* can be used as a local Agent command."""
    if not path.is_file():
        return False
    if platform.system() == "Windows":
        return path.suffix.lower() in WINDOWS_EXECUTABLE_SUFFIXES
    return os.access(path, os.X_OK)


def resolve_executable(value: str | Path | None) -> str | None:
    """Resolve a configured command or explicit executable path.

    This is deliberately limited to a file, a PATH command, or the Windows
    command-script formats that need to be launched through ``cmd.exe``.
    """
    raw = str(value or "").strip().strip('"')
    if not raw:
        return None
    path = Path(raw).expanduser()
    if _is_usable_executable(path):
        return str(path.resolve())
    found = shutil.which(raw)
    if found:
        found_path = Path(found).resolve()
        if _is_usable_executable(found_path):
            return str(found_path)
    return None


def _codex_binary() -> str | None:
    """Prefer the app-server shipped with ChatGPT on macOS."""
    if CHATGPT_CODEX_BINARY.is_file() and os.access(CHATGPT_CODEX_BINARY, os.X_OK):
        return str(CHATGPT_CODEX_BINARY)
    return _which(("codex",))


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
               configured_binary: str | None = None,
               capabilities: tuple[str, ...] = ("chat", "stream")) -> AgentCandidate:
    if configured_binary:
        executable = resolve_executable(configured_binary)
        if executable:
            return AgentCandidate(
                kind=kind, display_name=name, executable=executable,
                version=_version(executable), status="online",
                detail="已使用手动指定路径", local_ref=f"{kind}:default",
                selected=True, capabilities=capabilities)
        return AgentCandidate(
            kind=kind, display_name=name, executable=None, version="",
            status="not_found",
            detail=f"手动指定路径不可用：{configured_binary}",
            local_ref=f"{kind}:default", selected=False,
            capabilities=capabilities)
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


def _configured_binary(configured_agents: list[dict[str, object]], kind: str) -> str | None:
    for definition in configured_agents:
        if str(definition.get("adapter") or "").strip().lower() != kind:
            continue
        binary = str(definition.get("binary") or "").strip()
        if binary:
            return binary
    return None


def scan_agents(configured_agents: list[dict[str, object]] | None = None) -> list[AgentCandidate]:
    configured_agents = list(configured_agents or [])
    home = Path.home()
    codex_binary = _configured_binary(configured_agents, "codex")
    codex = codex_binary or _codex_binary()
    codex_candidate = _candidate(
        "codex", "Codex", ("codex",), configured_binary=codex_binary,
        capabilities=("chat", "stream", "cancel", "steer"))
    if codex and not codex_binary:
        # Keep the setup wizard's persisted binary aligned with the runtime
        # resolver; otherwise an old PATH CLI would be displayed and saved even
        # though desktop queue operations use ChatGPT's bundled Codex.
        codex_candidate.executable = codex
        codex_candidate.version = _version(codex)
        codex_candidate.status = "online"
        codex_candidate.detail = "已找到 Codex 桌面 app-server"
        codex_candidate.selected = True
    return [
        codex_candidate,
        _candidate(
            "claude", "Claude Code", ("claude",),
            configured_binary=_configured_binary(configured_agents, "claude"),
            capabilities=("chat", "stream", "cancel")),
        _candidate(
            "hermes", "Hermes", ("hermes", "hermes-cli"),
            profile_dirs=(home / ".hermes", home / ".xiaobai" / "hermes"),
            configured_binary=_configured_binary(configured_agents, "hermes"),
            capabilities=("chat", "stream")),
    ]
