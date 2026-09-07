"""Read-only bridge to the Codex desktop session catalogue.

The Connector never writes Codex history.  It asks the local app-server for the
thread catalogue and reads the corresponding JSONL rollouts in read-only mode,
then publishes a bounded, redacted projection to the Xiaobai Gateway.  All
paths are validated below ``CODEX_HOME`` so a malformed app-server response
cannot make the Connector read an arbitrary local file.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import select
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .adapters.base import executable_command


CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
LOCAL_CATALOG_DB = CODEX_HOME / "sqlite" / "codex-dev.db"
CHATGPT_CODEX_BINARY = Path(
    "/Applications/ChatGPT.app/Contents/Resources/codex")
# A desktop-originated rollout has no Gateway run row.  Once the local rollout
# is terminal, the mobile history view should treat it as idle/ready rather
# than leave a stale "completed" activity badge visible forever.
MOBILE_TERMINAL_STATUS = frozenset({"completed", "succeeded", "success"})
RECOVERY_PROMPT_PREFIX = "[Xiaobai internal recovery — do not display]"
DESKTOP_ACTIVE_LEASE_SECONDS = 5 * 60
RUNTIME_STATUS_SCAN_BYTES = 256 * 1024
MAX_THREADS = 100
MAX_TURNS = 200
MAX_ITEMS_PER_TURN = 80


class CodexSessionError(RuntimeError):
    """A local Codex RPC or rollout-read failure."""

    def __init__(self, message: str, *, code: Any = None) -> None:
        super().__init__(message)
        self.code = code


def mobile_runtime_status(value: Any) -> str | None:
    """Normalize desktop rollout states for the phone's history UI."""
    if value is None:
        return None
    status = str(value).strip()
    if not status:
        return None
    return "ready" if status.lower() in MOBILE_TERMINAL_STATUS else status


def resolve_codex_binary(binary: str | None = None) -> str:
    explicit = str(binary or os.environ.get("XIAOBAI_CODEX_BIN") or "").strip()
    if explicit:
        return explicit
    if CHATGPT_CODEX_BINARY.is_file() and os.access(CHATGPT_CODEX_BINARY, os.X_OK):
        return str(CHATGPT_CODEX_BINARY)
    discovered = shutil.which("codex")
    if discovered:
        return discovered
    return str(Path.home() / ".local" / "bin" / "codex")


def _epoch_seconds(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        candidate = float(value)
        return candidate if math.isfinite(candidate) else 0.0
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return 0.0


def _unix(value: Any) -> int:
    return int(_epoch_seconds(value))


def _bounded(value: Any, limit: int = 16_000) -> str:
    return str(value or "").replace("\x00", " ").strip()[:limit]


class CodexSessionClient:
    def __init__(self, binary: str | None = None, *, home: Path | None = None):
        self.binary = resolve_codex_binary(binary)
        self.home = Path(home or os.environ.get("CODEX_HOME") or CODEX_HOME).expanduser()
        self.catalog_db = self.home / "sqlite" / "codex-dev.db"

    def threads(self, *, limit: int = MAX_THREADS) -> list[dict[str, Any]]:
        result = self._request("thread/list", {
            "limit": min(max(int(limit), 1), MAX_THREADS),
            "sortKey": "updated_at", "sortDirection": "desc",
        })
        items = [dict(item) for item in result.get("data") or [] if isinstance(item, dict)]
        self._merge_desktop_display_titles(items)
        self._merge_desktop_runtime_status(items)
        self._merge_codex_ownership(items)
        return [self._public_thread(item) for item in items]

    def thread(self, thread_id: str) -> dict[str, Any]:
        wanted = str(thread_id or "").strip()
        if not wanted or len(wanted) > 200:
            raise CodexSessionError("Codex 会话 ID 无效")
        thread = next((item for item in self.threads(limit=MAX_THREADS)
                       if str(item.get("id") or "") == wanted), None)
        if thread is None:
            raise CodexSessionError("找不到这条 Codex 会话")
        path = self._safe_rollout_path(thread.get("path"))
        if path is None:
            raise CodexSessionError("Codex 会话文件无效")
        try:
            turns = self._turns_from_rollout(path)
            runtime_options = self._runtime_options(path)
            runtime_status = mobile_runtime_status(
                self._runtime_status_from_rollout(path))
        except (OSError, ValueError, UnicodeError) as exc:
            raise CodexSessionError("无法读取 Codex 会话历史") from exc
        return {**thread, **runtime_options, "turns": turns,
                "runtime_status": runtime_status,
                "queued_messages": self._queued_messages(wanted)}

    def snapshot(self, *, limit: int = MAX_THREADS) -> dict[str, Any]:
        """Return a bounded catalogue and inline details for remote history."""
        threads = self.threads(limit=limit)
        public: list[dict[str, Any]] = []
        budget = 700_000
        for item in threads:
            value = self._portable_thread(item)
            path = self._safe_rollout_path(value.get("path"))
            if path is not None:
                try:
                    value["turns"] = self._turns_from_rollout(path)
                    value.update(self._runtime_options(path))
                    value["runtime_status"] = mobile_runtime_status(
                        self._runtime_status_from_rollout(path))
                    value["queued_messages"] = self._queued_messages(str(value.get("id") or ""))
                except (OSError, ValueError, UnicodeError):
                    value["turns"] = []
                    value["queued_messages"] = []
            value.pop("path", None)
            value = self._trim_thread(value, budget)
            encoded_size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
            if encoded_size > budget:
                break
            public.append(value)
            budget -= encoded_size
        return {"threads": public}

    def status(self, *, local_ref: str) -> dict[str, Any]:
        """Return a cheap local status projection without opening app-server."""
        status = "online" if self._available() else "offline"
        progress: dict[str, Any] | None = None
        root = (self.home / "sessions").resolve()
        if root.is_dir():
            for path in root.rglob("*.jsonl"):
                try:
                    value = self._runtime_status_from_rollout(path)
                except (OSError, ValueError, UnicodeError):
                    continue
                if value == "thinking":
                    status = "busy"
                    progress = {
                        "phase": "thinking", "message": "正在思考",
                        "heartbeat_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                    break
        result: dict[str, Any] = {"local_ref": local_ref, "status": status}
        if progress:
            result["progress"] = progress
        return result

    def models(self) -> list[dict[str, Any]]:
        result = self._request("model/list", {"limit": 100, "includeHidden": False})
        return [dict(item) for item in result.get("data") or [] if isinstance(item, dict)]

    def rate_limits(self) -> dict[str, Any]:
        return self._request("account/rateLimits/read", {})

    def account_name(self) -> str | None:
        """Read only the email claim from Codex's local ID token.

        This is kept for local diagnostics and is never included in a history
        or status projection sent to the Gateway.
        """
        auth_path = self.home / "auth.json"
        try:
            document = json.loads(auth_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, TypeError, ValueError):
            return None
        if not isinstance(document, dict):
            return None
        tokens = document.get("tokens")
        token = tokens.get("id_token") if isinstance(tokens, dict) else None
        if not isinstance(token, str):
            return None
        parts = token.split(".")
        if len(parts) < 2:
            return None
        try:
            encoded = parts[1]
            payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            claims = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeError, TypeError, binascii.Error):
            return None
        email = claims.get("email") if isinstance(claims, dict) else None
        return email.strip() if isinstance(email, str) and email.strip() else None

    def queue_message(self, thread_id: str, text: str, client_message_id: str,
                      *, input_items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        thread = _bounded(thread_id, 160)
        message = _bounded(text, 16_000)
        message_id = _bounded(client_message_id, 160)
        if not thread or not message or not message_id:
            raise CodexSessionError("Codex 插话参数无效")
        items = list(input_items or [{"type": "text", "text": message}])
        if not items or any(not isinstance(item, dict) for item in items):
            raise CodexSessionError("Codex 插话内容无效")
        return self._request("thread/queue/add", {
            "threadId": thread, "input": items, "clientUserMessageId": message_id,
        })

    @staticmethod
    def defaults() -> dict[str, str]:
        return {"model": "gpt-5.6-luna", "reasoningEffort": "high"}

    def projects(self) -> list[dict[str, Any]]:
        projects: dict[str, dict[str, Any]] = {}
        for thread in self.threads(limit=MAX_THREADS):
            raw = str(thread.get("cwd") or "").strip()
            if not raw:
                continue
            path = Path(raw).expanduser()
            if not path.is_absolute() or not path.is_dir():
                continue
            try:
                resolved = str(path.resolve())
            except OSError:
                continue
            project = projects.setdefault(resolved, {
                "id": resolved, "path": resolved,
                "name": path.name or resolved, "thread_count": 0,
            })
            project["thread_count"] += 1
        return sorted(projects.values(), key=lambda item: (
            -int(item["thread_count"]), str(item["name"]).lower()))

    def is_project(self, cwd: str) -> bool:
        try:
            selected = str(Path(cwd).expanduser().resolve())
        except OSError:
            return False
        return any(item["path"] == selected for item in self.projects())

    def _available(self) -> bool:
        path = Path(self.binary).expanduser()
        return bool(shutil.which(self.binary) or path.is_file())

    def _safe_rollout_path(self, raw_path: Any) -> Path | None:
        raw = str(raw_path or "").strip()
        if not raw:
            return None
        try:
            path = Path(raw).expanduser().resolve()
            root = (self.home / "sessions").resolve()
        except OSError:
            return None
        if path.suffix.lower() != ".jsonl" or root not in path.parents:
            return None
        return path if path.is_file() else None

    @staticmethod
    def _public_thread(item: dict[str, Any]) -> dict[str, Any]:
        result = dict(item)
        result["id"] = str(result.get("id") or "").strip()
        result["name"] = str(result.get("name") or result.get("title") or "").strip() or None
        result["preview"] = _bounded(result.get("preview") or result.get("first_user_message"), 2_000)
        result["createdAt"] = _unix(result.get("createdAt") or result.get("created_at"))
        result["updatedAt"] = _unix(result.get("updatedAt") or result.get("updated_at"))
        return result

    @staticmethod
    def _trim_thread(value: dict[str, Any], budget: int) -> dict[str, Any]:
        result = dict(value)
        turns = result.get("turns")
        if isinstance(turns, list):
            result["turns"] = turns[-MAX_TURNS:]
            for turn in result["turns"]:
                if not isinstance(turn, dict):
                    continue
                items = turn.get("items")
                if isinstance(items, list):
                    turn["items"] = items[-MAX_ITEMS_PER_TURN:]
                    for item in turn["items"]:
                        if isinstance(item, dict) and isinstance(item.get("text"), str):
                            item["text"] = item["text"][:4_000]
        return result

    @staticmethod
    def _portable_thread(item: dict[str, Any]) -> dict[str, Any]:
        """Keep the Connector history wire format stable across Codex builds."""
        allowed = {
            "id", "name", "preview", "createdAt", "updatedAt", "model",
            "reasoningEffort", "cwd", "path", "runtime_status", "codex_owner",
        }
        return {key: value for key, value in item.items() if key in allowed}

    def _merge_desktop_display_titles(self, items: list[dict[str, Any]]) -> None:
        ids = [str(item.get("id") or "").strip() for item in items]
        ids = [item for item in ids if item]
        if not ids or not self.catalog_db.is_file():
            return
        placeholders = ",".join("?" for _ in ids)
        try:
            database = sqlite3.connect(
                f"file:{self.catalog_db}?mode=ro", uri=True, timeout=0.2)
            try:
                rows = database.execute(
                    "SELECT thread_id, display_title FROM local_thread_catalog "
                    f"WHERE host_id=? AND thread_id IN ({placeholders})", ["local", *ids]).fetchall()
            finally:
                database.close()
        except (OSError, sqlite3.Error):
            return
        titles = {str(thread_id): str(title).strip() for thread_id, title in rows if str(title).strip()}
        for item in items:
            if str(item.get("name") or "").strip():
                continue
            title = titles.get(str(item.get("id") or "").strip())
            if title:
                item["name"] = title

    def _merge_desktop_runtime_status(self, items: list[dict[str, Any]]) -> None:
        for item in items:
            path = self._safe_rollout_path(item.get("path"))
            if path is None:
                continue
            try:
                value = self._runtime_status_from_rollout(path)
            except (OSError, ValueError, UnicodeError):
                continue
            if value:
                item["runtime_status"] = value

    def _merge_codex_ownership(self, items: list[dict[str, Any]]) -> None:
        for item in items:
            path = self._safe_rollout_path(item.get("path"))
            if path is None:
                continue
            metadata = self._rollout_metadata(path)
            originator = metadata.get("originator")
            if originator:
                item["originator"] = originator
            owner = self._codex_owner(originator)
            if owner:
                item["codex_owner"] = owner

    @staticmethod
    def _rollout_metadata(path: Path) -> dict[str, str]:
        try:
            with path.open(encoding="utf-8") as handle:
                for raw in handle:
                    if not raw.strip():
                        continue
                    record = json.loads(raw)
                    if not isinstance(record, dict) or record.get("type") != "session_meta":
                        continue
                    payload = record.get("payload")
                    if not isinstance(payload, dict):
                        return {}
                    return {key: str(payload.get(key) or "").strip()
                            for key in ("originator", "source", "thread_source")
                            if str(payload.get(key) or "").strip()}
        except (OSError, UnicodeError, ValueError):
            return {}
        return {}

    @staticmethod
    def _codex_owner(originator: str | None) -> str | None:
        value = str(originator or "").strip().lower()
        if "codex desktop" in value or value == "desktop":
            return "desktop"
        if "xiaobai-connector" in value or "connector" in value:
            return "connector"
        return None

    @staticmethod
    def _turns_from_rollout(path: Path) -> list[dict[str, Any]]:
        turns: list[dict[str, Any]] = []
        active: dict[str, Any] | None = None
        with path.open(encoding="utf-8") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                record = json.loads(raw)
                payload = record.get("payload") if isinstance(record, dict) else None
                if not isinstance(payload, dict):
                    continue
                if record.get("type") == "event_msg":
                    if payload.get("type") in {"task_started", "turn_started"}:
                        turn_id = str(payload.get("turn_id") or "").strip()
                        if turn_id:
                            active = {"id": turn_id, "startedAt": _unix(record.get("timestamp")), "items": []}
                            turns.append(active)
                    continue
                if record.get("type") != "response_item" or active is None:
                    continue
                role = str(payload.get("role") or "")
                content = payload.get("content")
                if role not in {"user", "assistant"} or not isinstance(content, list):
                    continue
                text = "\n".join(str(item.get("text") or "") for item in content
                             if isinstance(item, dict) and item.get("type") in {"input_text", "output_text"}).strip()
                if not text:
                    continue
                if role == "user":
                    parts = [str(item.get("text") or "").strip() for item in content
                             if isinstance(item, dict) and item.get("type") == "input_text"]
                    parts = [part for part in parts if part and not part.startswith((
                        "<recommended_plugins>", "# AGENTS.md instructions for ",
                        "<environment_context>", "<app-context>", RECOVERY_PROMPT_PREFIX,
                    ))]
                    text = "\n".join(parts).strip()
                    if not text:
                        continue
                    item = {"id": str(payload.get("id") or f"{active['id']}-{len(active['items'])}"),
                            "type": "userMessage", "content": [{"type": "text", "text": text}]}
                else:
                    item = {"id": str(payload.get("id") or f"{active['id']}-{len(active['items'])}"),
                            "type": "agentMessage", "text": text}
                timestamp = _epoch_seconds(record.get("timestamp"))
                if timestamp > 0:
                    item["timestamp"] = timestamp
                active["items"].append(item)
        return [turn for turn in turns[-MAX_TURNS:] if turn["items"]]

    @staticmethod
    def _runtime_status_from_rollout(
            path: Path, *, now: float | None = None,
            active_lease_seconds: int = DESKTOP_ACTIVE_LEASE_SECONDS) -> str | None:
        try:
            file_mtime = path.stat().st_mtime
            size = path.stat().st_size
            scan_start = max(0, size - RUNTIME_STATUS_SCAN_BYTES)
            with path.open("rb") as handle:
                handle.seek(scan_start)
                if scan_start:
                    handle.readline()
                tail = handle.read()
        except OSError:
            return None

        def lifecycle(record: dict[str, Any]) -> str | None:
            if record.get("type") != "event_msg":
                return None
            payload = record.get("payload")
            if not isinstance(payload, dict):
                return None
            kind = str(payload.get("type") or "")
            if kind in {"task_started", "turn_started"}:
                return "thinking"
            if kind in {"task_complete", "turn_completed"}:
                return "completed" if str(payload.get("status") or "completed") == "completed" and not payload.get("error") else "failed"
            if kind in {"turn_aborted", "turn_interrupted", "task_interrupted", "task_cancelled", "turn_cancelled"}:
                return "stopped"
            return None

        for raw in reversed(tail.splitlines()):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
                value = lifecycle(record)
            except (ValueError, UnicodeError):
                continue
            if value:
                if value != "thinking":
                    return value
                freshest = max(file_mtime, _epoch_seconds(record.get("timestamp")))
                reference = time.time() if now is None else float(now)
                return ("thinking" if freshest
                        and reference - freshest <= max(int(active_lease_seconds), 1)
                        else "stopped")

        # A very active rollout can append more than the bounded tail after
        # its start record. Confirm the start in the head and use file mtime
        # as the liveness signal instead of incorrectly reporting no status.
        try:
            with path.open("rb") as handle:
                head = handle.read(RUNTIME_STATUS_SCAN_BYTES)
        except OSError:
            return None
        for raw in head.splitlines():
            if not raw.strip():
                continue
            try:
                if lifecycle(json.loads(raw)) == "thinking":
                    reference = time.time() if now is None else float(now)
                    return ("thinking" if file_mtime
                            and reference - file_mtime <= max(int(active_lease_seconds), 1)
                            else "stopped")
            except (ValueError, UnicodeError):
                continue
        return None

    @staticmethod
    def _runtime_options(path: Path) -> dict[str, str]:
        model, effort = "", ""
        with path.open(encoding="utf-8") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                record = json.loads(raw)
                if record.get("type") != "turn_context":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                candidate_model = str(payload.get("model") or "").strip()
                candidate_effort = str(payload.get("effort") or "").strip()
                model = candidate_model or model
                effort = candidate_effort or effort
                collaboration = payload.get("collaboration_mode")
                settings = (collaboration.get("settings") or {}
                            if isinstance(collaboration, dict) else {})
                if not effort and isinstance(settings, dict):
                    effort = str(settings.get("reasoning_effort") or "").strip()
        result: dict[str, str] = {}
        if model:
            result["model"] = model
        if effort:
            result["reasoningEffort"] = effort
        return result

    def _queued_messages(self, thread_id: str) -> list[dict[str, Any]]:
        thread = str(thread_id or "").strip()
        if not thread or not self.home.is_dir():
            return []
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for database_path in sorted(self.home.glob("queue_*.sqlite")):
            try:
                database = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=0.2)
                database.row_factory = sqlite3.Row
                try:
                    rows = database.execute(
                        "SELECT id,payload_json,queue_order,created_at_ms FROM queued_items "
                        "WHERE thread_id=? ORDER BY queue_order,created_at_ms,id", (thread,)).fetchall()
                finally:
                    database.close()
            except (OSError, sqlite3.Error):
                continue
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict):
                    continue
                user_input = payload.get("UserInput")
                if isinstance(user_input, dict):
                    inputs = user_input.get("content")
                    embedded_client_id = user_input.get("client_id")
                else:
                    inputs = payload.get("input")
                    embedded_client_id = None
                if isinstance(inputs, dict):
                    inputs = [inputs]
                if not isinstance(inputs, list):
                    continue
                text = "\n".join(str(item.get("text") or "").strip() for item in inputs
                                 if isinstance(item, dict) and item.get("type") in {"text", "input_text", "output_text"}).strip()
                if not text:
                    continue
                item_id = str(row["id"] or "").strip()
                client_id = str(payload.get("clientUserMessageId")
                                or payload.get("client_message_id")
                                or embedded_client_id or item_id).strip()
                if not item_id:
                    item_id = "queue:" + (client_id or text[:40])
                key = (item_id, client_id)
                if key in seen:
                    continue
                seen.add(key)
                projected: dict[str, Any] = {"id": item_id,
                    "client_message_id": client_id,
                    "text": text[:16_000]}
                try:
                    created_ms = int(row["created_at_ms"] or 0)
                except (TypeError, ValueError):
                    created_ms = 0
                if created_ms > 0:
                    projected["timestamp"] = created_ms / 1000.0
                result.append(projected)
        return result

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            proc = subprocess.Popen(
                executable_command(self.binary, "app-server", "--stdio"),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError as exc:
            raise CodexSessionError("无法启动本机 Codex") from exc
        try:
            self._write(proc, 1, "initialize", {
                "clientInfo": {"name": "xiaobai-connector", "version": "0.3.2"},
                "capabilities": {"experimentalApi": True},
            })
            self._response(proc, 1)
            assert proc.stdin is not None
            proc.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
            proc.stdin.flush()
            self._write(proc, 2, method, params)
            return self._response(proc, 2)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

    @staticmethod
    def _write(proc: subprocess.Popen[str], request_id: int, method: str,
               params: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}) + "\n")
        proc.stdin.flush()

    @staticmethod
    def _response(proc: subprocess.Popen[str], request_id: int) -> dict[str, Any]:
        assert proc.stdout is not None
        deadline = time.monotonic() + 20
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexSessionError("本机 Codex 响应超时")
            if os.name != "nt":
                readable, _, _ = select.select([proc.stdout], [], [], remaining)
                if not readable:
                    raise CodexSessionError("本机 Codex 响应超时")
            raw = proc.stdout.readline()
            if not raw:
                raise CodexSessionError("本机 Codex 提前退出")
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if value.get("id") != request_id:
                continue
            if value.get("error"):
                error = value["error"]
                if isinstance(error, dict):
                    raise CodexSessionError(str(error.get("message") or error)[:500], code=error.get("code"))
                raise CodexSessionError(str(error)[:500])
            result = value.get("result")
            return dict(result) if isinstance(result, dict) else {}


__all__ = ["CodexSessionClient", "CodexSessionError", "resolve_codex_binary"]
