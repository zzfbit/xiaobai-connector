"""Cross-platform Hermes adapter with portable identity and history projection.

Hermes installations do not all expose the same executable or storage layout,
so this adapter keeps the execution contract deliberately small: a configured
print command produces user-visible text, while optional JSON/JSONL/SQLite
history is read locally and sent as a bounded, redacted projection.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..agent_identity import HERMES_PRESENTATION, clone, hermes_avatar
from .base import Emit, LocalAgent, RunControl, RunRequest, executable_command, request_prompt


VALID_MODES = frozenset({"session", "bot"})
MAX_HISTORY_BYTES = 16 * 1024 * 1024
MAX_HISTORY_MESSAGES = 4_000


def _epoch_seconds(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        result = float(value)
        if result > 100_000_000_000:
            result /= 1000.0
        return result if result > 0 else 0.0
    if isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            return result if result > 0 else 0.0
        except ValueError:
            try:
                return _epoch_seconds(float(value))
            except ValueError:
                return 0.0
    return 0.0


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith(("{", "[")):
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            if parsed is not None:
                return _content_text(parsed)
        return raw
    if isinstance(value, dict):
        for key in ("text", "content", "message", "output", "response"):
            if key in value:
                result = _content_text(value[key])
                if result:
                    return result
        return ""
    if isinstance(value, list):
        return "".join(_content_text(item) for item in value).strip()
    return ""


def _is_internal(text: str) -> bool:
    value = str(text or "").lstrip()
    return value.startswith((
        "[Xiaobai internal", "<recommended_plugins>", "<environment_context>",
        "<app-context>", "# AGENTS.md instructions for ", "[系统指令]",
    ))


class HermesAdapter:
    def __init__(self, definition: dict[str, Any] | None = None):
        self.definition = dict(definition or {})
        self.binary = (str(self.definition.get("binary") or "").strip()
                       or os.environ.get("XIAOBAI_HERMES_BIN")
                       or shutil.which("hermes") or shutil.which("hermes-cli")
                       or "hermes")
        self._active: dict[str, float] = {}
        self._active_lock = threading.Lock()

    def _mode(self) -> str:
        value = str(self.definition.get("mode") or "").strip().lower()
        if not value:
            local_ref = str(self.definition.get("local_ref") or "")
            value = "bot" if local_ref.startswith("hermes:bot:") else "session"
        if value not in VALID_MODES:
            raise ValueError("Hermes mode 必须是 session 或 bot")
        return value

    def _local_ref(self) -> str:
        mode = self._mode()
        default = "hermes:bot:default" if mode == "bot" else "hermes:default"
        return str(self.definition.get("local_ref") or default).strip()

    def _configured(self) -> bool:
        if isinstance(self.definition.get("command"), list) and self.definition["command"]:
            return True
        return any(path.expanduser().is_dir() for path in self._profile_dirs())

    def _available(self) -> bool:
        path = Path(self.binary).expanduser()
        return bool(shutil.which(self.binary) or path.is_file())

    def _profile_dirs(self) -> list[Path]:
        values = [Path("~/.hermes"), Path("~/.xiaobai/hermes")]
        for variable in ("APPDATA", "LOCALAPPDATA"):
            raw = os.environ.get(variable)
            if raw:
                values.append(Path(raw) / "Hermes")
        return values

    def discover(self) -> list[LocalAgent]:
        available = self._available() or isinstance(self.definition.get("command"), list)
        configured = self._configured()
        status = "online" if available else "configured" if configured else "offline"
        detail = ("已找到 Hermes 命令" if self._available() else
                  "已配置 Hermes 命令" if available else
                  "已找到 Hermes 配置目录，但未找到 CLI" if configured else
                  "没有找到 Hermes")
        return [LocalAgent(
            local_ref=self._local_ref(), adapter="hermes",
            display_name=str(self.definition.get("display_name") or "Hermes"),
            mention_handle=str(self.definition.get("mention_handle") or "hermes"),
            capabilities=tuple(self.definition.get("capabilities") or ("chat", "stream")),
            status=status,
            enabled=bool(self.definition.get("enabled", available)) and available,
            avatar=clone(self.definition.get("avatar")
                         if isinstance(self.definition.get("avatar"), dict)
                         else hermes_avatar()),
            presentation=clone(self.definition.get("presentation")
                               if isinstance(self.definition.get("presentation"), dict)
                               else HERMES_PRESENTATION),
        )]

    def history_snapshots(self) -> list[dict[str, Any]]:
        """Expose Bot Mode's local transcript as the canonical Hermes source."""
        if self._mode() != "bot":
            return []
        profile = str(self.definition.get("profile_id") or "default")
        title = str(self.definition.get("session_title") or "Bot Chat")
        return [{
            "local_ref": self._local_ref(),
            "source": "hermes_bot_chat_v1",
            "session_title": title,
            "messages": self._read_history(profile=profile, title=title),
        }]

    def status_snapshots(self) -> list[dict[str, Any]]:
        available = self._available() or isinstance(self.definition.get("command"), list)
        with self._active_lock:
            started = min(self._active.values()) if self._active else None
        status: dict[str, Any] = {
            "local_ref": self._local_ref(),
            "status": "busy" if started is not None else "online" if available else "offline",
        }
        if started is not None:
            status["progress"] = {
                "phase": "thinking", "message": "Hermes 正在处理",
                "elapsed_seconds": max(0, int(time.time() - started)),
                "heartbeat_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        return [status]

    async def execute(self, request: RunRequest, emit: Emit,
                      control: RunControl) -> None:
        if not self._available() and not isinstance(self.definition.get("command"), list):
            raise RuntimeError("Hermes 没有可运行的 CLI；请安装 Hermes CLI 或配置 command")
        workdir = Path(str(self.definition.get("workdir") or Path.home())).expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        prompt = request_prompt(request)
        configured = self.definition.get("command")
        if isinstance(configured, list) and configured:
            command = executable_command(*[
                str(item).replace("{prompt}", prompt) for item in configured])
        else:
            command = executable_command(self.binary, "-z", prompt)
        proc = await asyncio.create_subprocess_exec(
            *command, cwd=str(workdir), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True, limit=16 * 1024 * 1024)
        started_at = time.time()
        with self._active_lock:
            self._active[request.run_id] = started_at
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            await emit("run.accepted", {})
            await emit("run.started", {"adapter_session_id": self._local_ref()})
            while True:
                if proc.stdout is None:
                    break
                read_task = asyncio.create_task(proc.stdout.readline())
                cancel_task = asyncio.create_task(control.cancel.wait())
                done, _ = await asyncio.wait(
                    {read_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
                if cancel_task in done and control.cancel.is_set():
                    read_task.cancel()
                    await asyncio.gather(read_task, return_exceptions=True)
                    await emit("run.canceled", {
                        "code": "user_canceled", "detail": "用户取消"})
                    return
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)
                raw = await read_task
                if not raw:
                    break
                text = raw.decode(errors="replace")
                if text.strip():
                    # Hermes may emit several independent response stages. A
                    # line boundary is the only portable signal across its CLI
                    # variants, so expose it as a stable message boundary.
                    await emit("run.output.delta", {
                        "delta": text, "message_start": True, "message_end": True,
                    })
            if await proc.wait() != 0:
                await emit("run.failed", {
                    "code": "hermes_failed", "detail": "Hermes 执行失败"})
            else:
                await emit("run.completed", {
                    "adapter_session_id": self._local_ref()})
        finally:
            with self._active_lock:
                self._active.pop(request.run_id, None)
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()

    def _history_paths(self, profile: str) -> list[Path]:
        configured = self.definition.get("history_path") or self.definition.get("history_file")
        if configured:
            return [Path(str(configured)).expanduser()]
        paths: list[Path] = []
        for directory in self._profile_dirs():
            directory = directory.expanduser()
            for name in (
                    "history.jsonl", "history.json", "chat.jsonl", "chat.json",
                    "messages.jsonl", "messages.json", "state.db"):
                paths.append(directory / name)
            profile_dir = directory / profile
            for name in ("history.jsonl", "history.json", "messages.jsonl", "messages.json", "state.db"):
                paths.append(profile_dir / name)
        return paths

    def _read_history(self, *, profile: str, title: str) -> list[dict[str, Any]]:
        del title  # Generic file formats do not guarantee a session title.
        messages: list[dict[str, Any]] = []
        for path in self._history_paths(profile):
            if not path.is_file():
                continue
            try:
                if path.stat().st_size > MAX_HISTORY_BYTES:
                    continue
            except OSError:
                continue
            if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                values = self._read_sqlite(path)
            else:
                values = self._read_json(path)
            for value in values:
                normalized = self._normalize_message(value, len(messages))
                if normalized:
                    messages.append(normalized)
                if len(messages) >= MAX_HISTORY_MESSAGES:
                    break
            if len(messages) >= MAX_HISTORY_MESSAGES:
                break
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in messages:
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            unique.append(item)
        return unique[:MAX_HISTORY_MESSAGES]

    @staticmethod
    def _read_json(path: Path) -> list[Any]:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        if path.suffix.lower() == ".jsonl":
            values: list[Any] = []
            for line in raw.splitlines():
                if not line.strip():
                    continue
                try:
                    values.append(json.loads(line))
                except ValueError:
                    continue
            return HermesAdapter._flatten_records(values)
        try:
            return HermesAdapter._flatten_records([json.loads(raw)])
        except ValueError:
            return []

    @staticmethod
    def _flatten_records(values: list[Any]) -> list[Any]:
        result: list[Any] = []
        for value in values:
            if isinstance(value, dict) and isinstance(value.get("messages"), list):
                result.extend(HermesAdapter._flatten_records(value["messages"]))
            elif isinstance(value, dict) and isinstance(value.get("history"), list):
                result.extend(HermesAdapter._flatten_records(value["history"]))
            else:
                result.append(value)
        return result

    @staticmethod
    def _read_sqlite(path: Path) -> list[dict[str, Any]]:
        try:
            database = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
            database.row_factory = sqlite3.Row
            try:
                tables = {str(row[0]) for row in database.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                table = next((name for name in (
                    "messages", "chat_messages", "history_messages") if name in tables), None)
                if not table:
                    return []
                columns = {str(row[1]) for row in database.execute(
                    f'PRAGMA table_info("{table}")').fetchall()}
                role = next((name for name in ("role", "author_role", "speaker") if name in columns), None)
                content = next((name for name in ("content", "text", "message") if name in columns), None)
                if not role or not content:
                    return []
                identifier = next((name for name in ("id", "message_id") if name in columns), None)
                timestamp = next((name for name in ("timestamp", "created_at", "created_at_ms") if name in columns), None)
                fields = [role, content] + ([identifier] if identifier else []) + ([timestamp] if timestamp else [])
                quoted = ",".join('"' + field.replace('"', '""') + '"' for field in fields)
                rows = database.execute(
                    f'SELECT {quoted} FROM "{table}" ORDER BY rowid LIMIT {MAX_HISTORY_MESSAGES}').fetchall()
                result: list[dict[str, Any]] = []
                for row in rows:
                    value: dict[str, Any] = {"role": row[role], "content": row[content]}
                    if identifier:
                        value["id"] = row[identifier]
                    if timestamp:
                        value["timestamp"] = row[timestamp]
                    result.append(value)
                return result
            finally:
                database.close()
        except (OSError, sqlite3.Error):
            return []

    def _normalize_message(self, value: Any, index: int) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        role = str(value.get("role") or value.get("author_role") or value.get("speaker") or "").lower()
        if role in {"human", "user", "person"}:
            public_role = "human"
        elif role in {"assistant", "robot", "bot", "hermes", "model"}:
            public_role = "robot"
        else:
            return None
        text = _content_text(value.get("text", value.get("content", value.get("message"))))
        if not text or _is_internal(text):
            return None
        message_id = str(value.get("id") or value.get("message_id") or
                         f"hermes:{self._local_ref()}:{index}").strip()
        if not message_id or len(message_id) > 200:
            return None
        item: dict[str, Any] = {"id": message_id, "role": public_role,
                                "text": text[:16_000]}
        timestamp = _epoch_seconds(value.get("timestamp", value.get(
            "created_at", value.get("createdAt"))))
        item["timestamp"] = timestamp or time.time()
        return item


__all__ = ["HermesAdapter"]
