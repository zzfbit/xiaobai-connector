"""Reconnect-capable Connector WebSocket runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .adapters.codex import CodexQueueError
from .adapters.router import AdapterRouter
from .config import ConnectorConfig
from .event_stream import ConnectorEventStream, normalize_progress
from .models import RunControl, RunRequest
from .protocol import ack, decode, make
from .spool import RECOVERABLE_TERMINAL_CODES, Spool


class GatewayClient:
    """One desktop device connection to the Xiaobai Agent Gateway."""

    def __init__(self, config: ConnectorConfig, token: str, router: AdapterRouter,
                 *, spool_path: Path, on_status: Callable[[str, str], None] | None = None):
        self.config = config
        self._token = token
        self.router = router
        self.spool = Spool(spool_path)
        self.connection_id: str | None = None
        self.registered_agents: list[dict[str, Any]] = []
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._controls: dict[str, RunControl] = {}
        self._agent_locks: dict[str, asyncio.Lock] = {}
        self._ack_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._history_fingerprints: dict[str, str] = {}
        self._status_fingerprints: dict[str, str] = {}
        self._history_projection_lock = asyncio.Lock()
        self._status_projection_lock = asyncio.Lock()
        self._on_status = on_status

    def _status(self, state: str, detail: str = "") -> None:
        if self._on_status:
            try:
                self._on_status(state, detail)
            except Exception:
                pass

    @staticmethod
    def _queue_error(exc: BaseException | None = None) -> dict[str, Any]:
        """Convert a local desktop queue failure into a retryable ACK."""
        raw = str(exc or "").strip()
        rpc_code = str(getattr(exc, "code", "") or "").strip()
        lowered = raw.lower()
        if (rpc_code in {"-32600", "invalid_request"}
                or "unknown variant `thread/queue/add`" in lowered
                or "unknown method" in lowered):
            return {
                "code": "queue_protocol_unsupported",
                "message": "本机 Codex 不支持桌面消息队列，请更新 Connector",
                "retryable": True,
            }
        if "no rollout found" in lowered or "找不到这条 codex 会话" in lowered:
            return {
                "code": "thread_not_found",
                "message": "桌面 Codex 会话已经不存在，请重新选择会话",
                "retryable": False,
            }
        return {
            "code": "queue_failed",
            "message": raw[:500] if raw else "桌面 Codex 队列暂时未接收消息",
            "retryable": True,
        }

    def connection_url(self) -> str:
        split = urllib.parse.urlsplit(self.config.server_url)
        query = urllib.parse.parse_qs(split.query)
        query["device_id"] = [self.config.device_id]
        return urllib.parse.urlunsplit((split.scheme, split.netloc,
                                        split.path or "/agent/connect",
                                        urllib.parse.urlencode(query, doseq=True),
                                        split.fragment))

    async def run_forever(self, stop: asyncio.Event) -> None:
        delay = 1.0
        while not stop.is_set():
            try:
                await self.connect_and_register(stop=stop)
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._status("offline", str(exc)[:300])
            if stop.is_set():
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2.0, 30.0) + random.random() * 0.25
        self._status("stopped", "连接已停止")

    async def connect_and_register(self, *, stop: asyncio.Event) -> None:
        headers = {"Authorization": "Bearer " + self._token}
        self._status("connecting", "正在连接服务器")
        async with connect(
            self.connection_url(), additional_headers=headers, compression=None,
            ping_interval=20, ping_timeout=30, proxy=None, max_size=1_048_576,
        ) as websocket:
            lock = asyncio.Lock()
            hello = make("hello", self.config.device_id, None, {
                "protocol_versions": [1],
                "connector_version": self.config.connector_version,
                "platform": self.config.platform,
                "event_types": [
                    "agents.snapshot", "agent.status", "agent.history.snapshot", "pong", "ack",
                    "run.accepted", "run.started", "run.output.delta", "run.progress",
                    "run.awaiting_input", "run.awaiting_approval", "run.completed",
                    "run.failed", "run.canceled", "run.steer", "codex.thread.queue",
                    "codex.sequence.create", "codex.sequence.item", "codex.sequence.start",
                    "codex.sequence.cancel",
                    "agent.message.send",
                ],
            })
            await websocket.send(hello.dumps())
            hello_ack = decode(await websocket.recv())
            if hello_ack.type != "hello.ack" or hello_ack.device_id != self.config.device_id:
                raise RuntimeError("服务器没有返回有效 hello.ack")
            self.connection_id = str(hello_ack.payload.get("connection_id") or "")
            if not self.connection_id or hello_ack.connection_id != self.connection_id:
                raise RuntimeError("hello.ack connection_id 不一致")
            snapshot = make("agents.snapshot", self.config.device_id, self.connection_id,
                            {"agents": [item.snapshot() for item in self.router.discover()]})
            await websocket.send(snapshot.dumps())
            snapshot_ack = decode(await websocket.recv())
            if (snapshot_ack.type != "ack" or snapshot_ack.event_id != snapshot.event_id
                    or not snapshot_ack.payload.get("accepted")):
                raise RuntimeError("服务器拒绝了 Agent 列表")
            self.registered_agents = list(snapshot_ack.payload.get("agents") or [])
            self.spool.requeue_failed_completions()
            self._recover_unfinished()
            self._status("online", f"已连接，{len(self.registered_agents)} 个 Agent")
            await self._hold(websocket, lock, stop)

    async def _hold(self, websocket: Any, lock: asyncio.Lock, stop: asyncio.Event) -> None:
        sender = asyncio.create_task(self._event_sender(websocket, lock))
        receiver = asyncio.create_task(self._event_receiver(websocket, lock))
        stop_task = asyncio.create_task(stop.wait())
        history = asyncio.create_task(self._history_projector())
        status = asyncio.create_task(self._status_projector())
        sequence_runner = asyncio.create_task(self._task_sequence_loop())
        waiters = {sender, receiver, history, status, sequence_runner, stop_task}
        done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done and stop.is_set():
            await websocket.close(1000, "connector stopping")
        for task in pending:
            task.cancel()
        results = await asyncio.gather(*waiters, return_exceptions=True)
        for result in results:
            if isinstance(result, ConnectionClosed):
                raise result
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise result

    async def _history_projector(self) -> None:
        while True:
            try:
                await self._queue_history_snapshots()
            except Exception:
                # History is advisory. A locked or partially written local
                # Agent database must never tear down the command lane.
                pass
            await asyncio.sleep(2)

    async def _status_projector(self) -> None:
        while True:
            try:
                await self._queue_agent_status()
            except Exception:
                pass
            await asyncio.sleep(2)

    async def _queue_agent_status(self, *, force: bool = False) -> None:
        reader = getattr(self.router, "status_snapshots", None)
        if not callable(reader) or self._status_projection_lock.locked():
            return
        async with self._status_projection_lock:
            try:
                snapshots = await asyncio.wait_for(
                    asyncio.to_thread(reader), timeout=5)
            except Exception:
                return
        for snapshot in snapshots or []:
            if not isinstance(snapshot, dict):
                continue
            local_ref = str(snapshot.get("local_ref") or "").strip()
            status = str(snapshot.get("status") or "").strip().lower()
            if not local_ref or status not in {"online", "busy", "offline"}:
                continue
            payload: dict[str, Any] = {"local_ref": local_ref, "status": status}
            progress = normalize_progress({"progress": snapshot.get("progress")})
            if progress:
                payload["progress"] = progress
            fingerprint_value = dict(payload)
            fingerprint_progress = fingerprint_value.get("progress")
            if isinstance(fingerprint_progress, dict):
                fingerprint_progress = dict(fingerprint_progress)
                fingerprint_progress.pop("heartbeat_at", None)
                fingerprint_value["progress"] = fingerprint_progress
            fingerprint = hashlib.sha256(json.dumps(
                fingerprint_value, ensure_ascii=False, separators=(",", ":"),
                sort_keys=True).encode("utf-8")).hexdigest()
            if not force and self._status_fingerprints.get(local_ref) == fingerprint:
                continue
            self.spool.enqueue_event("status:" + local_ref, "agent.status", payload)
            self._status_fingerprints[local_ref] = fingerprint

    async def _queue_history_snapshots(self) -> None:
        reader = getattr(self.router, "history_snapshots", None)
        if not callable(reader) or self._history_projection_lock.locked():
            return
        async with self._history_projection_lock:
            try:
                snapshots = await asyncio.wait_for(
                    asyncio.to_thread(reader), timeout=5)
            except Exception:
                return
        for snapshot in snapshots or []:
            if not isinstance(snapshot, dict):
                continue
            local_ref = str(snapshot.get("local_ref") or "").strip()
            source = str(snapshot.get("source") or "").strip()
            if not local_ref or source not in {"hermes_bot_chat_v1", "codex_threads_v1"}:
                continue
            payload: dict[str, Any] = {
                "local_ref": local_ref,
                "source": source,
            }
            if source == "hermes_bot_chat_v1":
                messages = snapshot.get("messages")
                if not isinstance(messages, list):
                    continue
                payload["messages"] = messages
            else:
                threads = snapshot.get("threads")
                if not isinstance(threads, list):
                    continue
                payload["threads"] = threads
                capabilities = snapshot.get("capabilities")
                if isinstance(capabilities, dict):
                    payload["capabilities"] = capabilities
            fingerprint = hashlib.sha256(json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"),
                sort_keys=True).encode("utf-8")).hexdigest()
            if self._history_fingerprints.get(local_ref) == fingerprint:
                continue
            self.spool.enqueue_event("history:" + local_ref,
                                     "agent.history.snapshot", payload)
            self._history_fingerprints[local_ref] = fingerprint

    def _recover_unfinished(self) -> None:
        """Resume interrupted local runs from the durable event spool."""
        for row in self.spool.unfinished_starts():
            run_id = str(row["run_id"] or "")
            active = self._tasks.get(run_id)
            if active is not None and not active.done():
                continue
            payload = self.spool.recovery_payload(run_id) or dict(row["payload"])
            if not self._owns_local_payload(payload):
                continue
            terminal = self.spool.terminal_event(run_id)
            terminal_code = str((terminal or {}).get("payload", {}).get("code") or "")
            if terminal is not None and terminal_code not in RECOVERABLE_TERMINAL_CODES:
                self.spool.set_command_state(str(row["event_id"]), "finished")
                continue
            if terminal is not None:
                self.spool.suppress_recoverable_terminals(run_id)
            payload["_connector_recovery"] = True
            payload["_output_seq_start"] = self.spool.output_sequence(run_id)
            control = self._controls.setdefault(run_id, RunControl())
            resumed = SimpleNamespace(
                event_id=str(row["event_id"]), event_type=str(row["event_type"]),
                payload=payload)
            self._tasks[run_id] = asyncio.create_task(
                self._execute(resumed, control))

    async def _event_sender(self, websocket: Any, lock: asyncio.Lock) -> None:
        while True:
            rows = self.spool.pending_events()
            for row in rows:
                envelope = make(row["event_type"], self.config.device_id,
                                self.connection_id, row["payload"], event_id=row["event_id"])
                history_waiter: asyncio.Future[dict[str, Any]] | None = None
                if row["event_type"] == "agent.history.snapshot":
                    history_waiter = asyncio.get_running_loop().create_future()
                    self._ack_waiters[row["event_id"]] = history_waiter
                try:
                    async with lock:
                        await websocket.send(envelope.dumps())
                except ConnectionClosed:
                    if history_waiter is not None:
                        self._ack_waiters.pop(row["event_id"], None)
                    return
                self.spool.mark_sent(row["event_id"])
                if history_waiter is not None:
                    try:
                        await asyncio.wait_for(asyncio.shield(history_waiter), timeout=30)
                    except asyncio.TimeoutError:
                        self._ack_waiters.pop(row["event_id"], None)
                        raise RuntimeError("Agent 历史同步 ACK 超时") from None
            await asyncio.sleep(0.1)

    async def _event_receiver(self, websocket: Any, lock: asyncio.Lock) -> None:
        async for raw in websocket:
            envelope = decode(raw)
            if envelope.device_id != self.config.device_id:
                await websocket.close(4400, "device mismatch")
                return
            if envelope.type == "ping":
                async with lock:
                    await websocket.send(make("pong", self.config.device_id,
                                               self.connection_id,
                                               {"ping_event_id": envelope.event_id}).dumps())
                continue
            if envelope.type == "ack":
                error = envelope.payload.get("error")
                error = error if isinstance(error, dict) else {}
                error_code = str(error.get("code") or "")
                retryable = bool(error.get("retryable")) or error_code in {
                    "stale_connection", "rate_limited",
                }
                self.spool.acknowledge(
                    envelope.event_id, bool(envelope.payload.get("accepted")),
                    retryable=retryable, error_code=error_code)
                waiter = self._ack_waiters.pop(envelope.event_id, None)
                if waiter is not None and not waiter.done():
                    waiter.set_result(dict(envelope.payload))
                continue
            if envelope.type == "device.revoke":
                async with lock:
                    await websocket.send(ack(envelope, accepted=True).dumps())
                await websocket.close(4003, "device revoked")
                self._status("revoked", "设备已被撤销，请重新配对")
                return
            if envelope.type not in {
                "run.start", "agent.notification", "run.cancel", "run.input",
                "run.steer", "run.approve", "run.reject", "codex.thread.queue",
                "codex.sequence.create", "codex.sequence.item",
                "codex.sequence.start", "codex.sequence.cancel",
            }:
                async with lock:
                    await websocket.send(ack(envelope, accepted=False, error={
                        "code": "unknown_type", "message": "Connector 不接受该事件类型",
                        "retryable": False}).dumps())
                continue
            try:
                # The initial execution command and thread-queue command carry
                # the immutable Agent binding.  Later controls intentionally
                # contain only run_id plus their small control payload.
                if envelope.type in {
                    "run.start", "agent.notification", "codex.thread.queue",
                    "codex.sequence.create", "codex.sequence.item",
                    "codex.sequence.start", "codex.sequence.cancel",
                }:
                    self._validate_command(
                        envelope.payload,
                        require_enabled=envelope.type != "codex.sequence.cancel")
                if envelope.type in {
                    "codex.sequence.create", "codex.sequence.item",
                    "codex.sequence.start", "codex.sequence.cancel",
                }:
                    self._validate_task_sequence_command(envelope.payload, envelope.type)
                fresh = self.spool.persist_command(envelope.event_id, envelope.type,
                                                    envelope.payload)
                if envelope.type in {"run.steer", "codex.thread.queue"}:
                    command_state = self.spool.command_state(envelope.event_id)
                    if fresh or command_state == "persisted":
                        delivered = await self._dispatch(envelope)
                        if delivered:
                            self.spool.set_command_state(envelope.event_id, "finished")
                            response = ack(envelope, accepted=True,
                                           extra={"duplicate": not fresh})
                        else:
                            self.spool.set_command_state(envelope.event_id, "failed")
                            error = (self._queue_error()
                                     if envelope.type == "codex.thread.queue" else {
                                         "code": "run_not_active",
                                         "message": "本机任务已经结束",
                                         "retryable": True})
                            response = ack(envelope, accepted=False, error=error)
                    elif command_state == "failed":
                        error = (self._queue_error()
                                 if envelope.type == "codex.thread.queue" else {
                                     "code": "run_not_active",
                                     "message": "本机任务已经结束",
                                     "retryable": True})
                        response = ack(envelope, accepted=False, error=error)
                    else:
                        response = ack(envelope, accepted=True,
                                       extra={"duplicate": True})
                    async with lock:
                        await websocket.send(response.dumps())
                elif envelope.type in {
                    "codex.sequence.create", "codex.sequence.item",
                    "codex.sequence.start", "codex.sequence.cancel",
                }:
                    command_state = self.spool.command_state(envelope.event_id)
                    if fresh or command_state == "persisted":
                        delivered = await self._dispatch(envelope)
                        if delivered:
                            self.spool.set_command_state(envelope.event_id, "finished")
                            response = ack(envelope, accepted=True,
                                           extra={"duplicate": not fresh})
                        else:
                            self.spool.set_command_state(envelope.event_id, "failed")
                            response = ack(envelope, accepted=False, error={
                                "code": "sequence_rejected",
                                "message": "Connector 未能保存顺序任务",
                                "retryable": True})
                    elif command_state == "failed":
                        response = ack(envelope, accepted=False, error={
                            "code": "sequence_rejected",
                            "message": "Connector 未能保存顺序任务",
                            "retryable": True})
                    else:
                        response = ack(envelope, accepted=True,
                                       extra={"duplicate": True})
                    async with lock:
                        await websocket.send(response.dumps())
                else:
                    async with lock:
                        await websocket.send(ack(envelope, accepted=True,
                                                 extra={"duplicate": not fresh}).dumps())
                    if fresh:
                        await self._dispatch(envelope)
            except CodexQueueError as exc:
                self.spool.set_command_state(envelope.event_id, "failed")
                async with lock:
                    await websocket.send(ack(
                        envelope, accepted=False, error=self._queue_error(exc)).dumps())
            except (ValueError, KeyError, RuntimeError) as exc:
                if envelope.type in {"run.steer", "codex.thread.queue"}:
                    # _dispatch may fail after persist_command() but before
                    # the type-specific ACK branch can mark the row. Keep the
                    # durable projection truthful instead of leaving a
                    # rejected command stuck in ``persisted``.
                    self.spool.set_command_state(envelope.event_id, "failed")
                async with lock:
                    await websocket.send(ack(envelope, accepted=False, error={
                        "code": "invalid_command", "message": str(exc)[:500],
                        "retryable": False}).dumps())

    def _validate_command(self, payload: dict[str, Any], *,
                          require_enabled: bool = True) -> None:
        if not isinstance(payload, dict):
            raise ValueError("执行命令必须是对象")
        agent_id = str(payload.get("agent_id") or "")
        local_ref = str(payload.get("local_ref") or "")
        if not agent_id or not local_ref:
            raise ValueError("执行命令缺少 agent_id/local_ref")
        match = next((item for item in self.registered_agents
                      if str(item.get("local_ref") or "") == local_ref), None)
        if match is None or str(match.get("agent_id") or "") != agent_id:
            raise ValueError("agent_id 与 local_ref 不匹配")
        if require_enabled and (not bool(match.get("enabled"))
                                or not self.router.contains(local_ref)):
            raise ValueError("Agent 未启用")

    def _owned_agent_keys(self) -> set[tuple[str, str]]:
        """Return the local Agent identities this Connector currently exposes."""
        return {
            (str(item.get("local_ref") or "").strip(),
             str(item.get("adapter") or "").strip().lower())
            for item in self.registered_agents
            if str(item.get("local_ref") or "").strip()
        }

    def _owns_local_payload(self, payload: dict[str, Any]) -> bool:
        local_ref = str(payload.get("local_ref") or "").strip()
        adapter = str(payload.get("adapter") or "").strip().lower()
        agent_id = str(payload.get("agent_id") or "").strip()
        if not local_ref or not self.router.contains(local_ref):
            return False
        matches = [item for item in self.registered_agents
                   if str(item.get("local_ref") or "").strip() == local_ref]
        return any(
            (not adapter or str(item.get("adapter") or "").strip().lower() == adapter)
            and (not agent_id or str(item.get("agent_id") or "").strip() == agent_id)
            and bool(item.get("enabled"))
            for item in matches
        )

    def _validate_task_sequence_command(self, payload: dict[str, Any],
                                        event_type: str) -> None:
        """Validate ordered-task fields before touching the local SQLite spool."""
        if str(payload.get("adapter") or "").lower() != "codex":
            raise ValueError("顺序任务只能交给 Codex")
        sequence_id = str(payload.get("sequence_id") or "").strip()
        if not sequence_id:
            raise ValueError("顺序任务缺少 sequence_id")
        if event_type == "codex.sequence.cancel":
            return
        if event_type in {"codex.sequence.create", "codex.sequence.start"}:
            try:
                item_count = int(payload.get("item_count"))
            except (TypeError, ValueError):
                raise ValueError("顺序任务 item_count 无效") from None
            if item_count < 1:
                raise ValueError("顺序任务至少包含一项")
        if event_type == "codex.sequence.item":
            item_id = str(payload.get("sequence_item_id") or "").strip()
            run_id = str(payload.get("run_id") or "").strip()
            try:
                position = int(payload.get("position"))
            except (TypeError, ValueError):
                raise ValueError("顺序任务 position 无效") from None
            if not item_id or not run_id or position < 0:
                raise ValueError("顺序任务 item 无效")
            input_value = payload.get("input")
            if not isinstance(input_value, dict):
                raise ValueError("顺序任务 input 无效")
            text = str(input_value.get("text") or "").strip()
            if not text or len(text) > 16_000:
                raise ValueError("顺序任务消息无效")
            session = payload.get("codex_session")
            cwd = (str((session or {}).get("cwd") or "").strip()
                   if isinstance(session, dict) else "")
            if (not self._is_absolute_path(cwd) or len(cwd) > 1_024
                    or "\x00" in cwd):
                raise ValueError("顺序任务项目路径无效")

    @staticmethod
    def _is_absolute_path(value: str) -> bool:
        """Accept POSIX, drive-letter, and UNC paths from Windows clients."""
        return value.startswith("/") or bool(re.match(r"^[A-Za-z]:[\\/]", value)) \
            or value.startswith("\\\\")

    async def _task_sequence_loop(self) -> None:
        """Run saved ordered tasks locally, including after reconnects."""
        while True:
            due = await asyncio.to_thread(
                self.spool.claim_due_task_sequence_items,
                owned_agent_keys=self._owned_agent_keys())
            for row in due:
                run_id = str(row["run_id"])
                active = self._tasks.get(run_id)
                if active is not None and not active.done():
                    continue
                payload = dict(row["payload"])
                if not self._owns_local_payload(payload):
                    continue
                envelope = SimpleNamespace(
                    event_id=str(row["event_id"]),
                    event_type="run.start",
                    payload=payload,
                )
                control = self._controls.setdefault(run_id, RunControl())
                self._tasks[run_id] = asyncio.create_task(
                    self._execute(envelope, control))
            await asyncio.sleep(0.5)

    async def _dispatch(self, envelope: Any) -> bool:
        payload = envelope.payload
        run_id = str(payload.get("run_id") or "")
        if envelope.type in {"run.start", "agent.notification"}:
            control = self._controls.setdefault(run_id, RunControl())
            task = self._tasks.get(run_id)
            if task is None or task.done():
                self._tasks[run_id] = asyncio.create_task(self._execute(envelope, control))
            return True
        if envelope.type == "run.cancel":
            self._controls.setdefault(run_id, RunControl()).cancel.set()
            return True
        if envelope.type == "run.input":
            await self._controls.setdefault(run_id, RunControl()).inputs.put(payload)
            return True
        if envelope.type == "run.steer":
            task = self._tasks.get(run_id)
            if task is None or task.done():
                return False
            await self._controls.setdefault(run_id, RunControl()).steers.put(payload)
            return True
        if envelope.type in {"run.approve", "run.reject"}:
            await self._controls.setdefault(run_id, RunControl()).approvals.put({
                **payload, "approved": envelope.type == "run.approve"})
            return True
        if envelope.type == "codex.thread.queue":
            return bool(await self.router.queue_thread_message(
                local_ref=str(payload.get("local_ref") or ""),
                thread_id=str(payload.get("thread_id") or ""),
                text=str(payload.get("text") or ""),
                attachments=list(payload.get("attachments") or []),
                client_message_id=str(payload.get("client_message_id") or ""),
            ))
        if envelope.type == "codex.sequence.create":
            return self.spool.persist_task_sequence(
                str(payload.get("sequence_id") or ""), payload)
        if envelope.type == "codex.sequence.item":
            return self.spool.persist_task_sequence_item(
                str(payload.get("sequence_id") or ""), payload)
        if envelope.type == "codex.sequence.start":
            return self.spool.start_task_sequence(str(payload.get("sequence_id") or ""))
        if envelope.type == "codex.sequence.cancel":
            self.spool.cancel_task_sequence(str(payload.get("sequence_id") or ""))
            active_run_id = str(payload.get("active_run_id") or "").strip()
            task = self._tasks.get(active_run_id)
            if task is not None and not task.done():
                self._controls.setdefault(active_run_id, RunControl()).cancel.set()
            return True
        return False

    async def _execute(self, envelope: Any, control: RunControl) -> None:
        payload = envelope.payload
        run_id = str(payload.get("run_id") or "")
        local_ref = str(payload.get("local_ref") or "")
        adapter_name = str(payload.get("adapter") or "").strip().lower()
        sequence_id = str(payload.get("sequence_id") or "").strip()
        sequence_item_id = str(payload.get("sequence_item_id") or "").strip()
        request = RunRequest(
            run_id=run_id, agent_id=str(payload.get("agent_id") or ""),
            local_ref=local_ref,
            text=str((payload.get("input") or {}).get("text") or ""),
            deadline_at=str(payload.get("deadline_at") or ""), payload=payload)
        adapter = self.router.adapter_for(local_ref)
        async def raw_emit(event_type: str, value: dict[str, Any] | None = None) -> Any:
            value = dict(value) if isinstance(value, dict) else {}
            if event_type == "agent.message.send":
                policy = dict(request.payload.get("policy") or {})
                if not bool(policy.get("message_agent_allowed")):
                    raise RuntimeError("message_agent_not_allowed")
                if set(value) != {"target", "message"}:
                    raise RuntimeError("message_agent 参数无效")
                target = str(value.get("target") or "").strip()
                message = str(value.get("message") or "").strip()
                if not target or not message or len(target) > 160 or len(message) > 16_000:
                    raise RuntimeError("message_agent 参数无效")
                event_id = self.spool.enqueue_event(run_id, event_type, {
                    "delivery_id": "delivery_" + uuid.uuid4().hex,
                    "source_run_id": run_id, "source_agent_id": request.agent_id,
                    "target": target, "message": message,
                })
                waiter = asyncio.get_running_loop().create_future()
                self._ack_waiters[event_id] = waiter
                try:
                    result = await asyncio.wait_for(asyncio.shield(waiter), timeout=8)
                except asyncio.TimeoutError as exc:
                    raise RuntimeError("message_agent ACK 超时") from exc
                finally:
                    self._ack_waiters.pop(event_id, None)
                if not result.get("accepted"):
                    error = result.get("error") if isinstance(result.get("error"), dict) else {}
                    raise RuntimeError(
                        str(error.get("code") or "message_agent 被拒绝") + ": "
                        + str(error.get("message") or ""))
                return result

            terminal = {"run.completed", "run.failed", "run.canceled"}
            if event_type in terminal:
                previous = self.spool.terminal_event(run_id)
                previous_code = str(
                    (previous or {}).get("payload", {}).get("code") or "")
                if previous is not None and not (
                        bool(payload.get("_connector_recovery"))
                        and previous_code in RECOVERABLE_TERMINAL_CODES):
                    return None
            if sequence_id:
                value = {**value, "sequence_id": sequence_id,
                         "sequence_item_id": sequence_item_id}
            event_id = self.spool.enqueue_event(run_id, event_type, value)
            if sequence_id and sequence_item_id and event_type in terminal:
                state = {
                    "run.completed": "completed",
                    "run.failed": "failed",
                    "run.canceled": "canceled",
                }[event_type]
                self.spool.finish_task_sequence_item(
                    sequence_id, sequence_item_id, run_id, state,
                    str(value.get("detail") or ""))
            return event_id

        session = dict(payload.get("codex_session") or {})
        thread_id = str(session.get("thread_id") or "").strip()
        if adapter_name == "codex":
            execution_key = "codex-thread:" + thread_id if thread_id else "codex-new:" + run_id
        else:
            execution_key = local_ref
        lock = self._agent_locks.setdefault(execution_key, asyncio.Lock())
        output_seq_start = self.spool.output_sequence(run_id)
        try:
            output_seq_start = max(output_seq_start, int(payload.get("_output_seq_start") or 0))
        except (TypeError, ValueError):
            pass
        stream = ConnectorEventStream(raw_emit, output_seq_start=output_seq_start)
        async with lock:
            self.spool.set_command_state(envelope.event_id, "running")
            started_at = datetime.now(timezone.utc)

            async def heartbeat() -> None:
                while True:
                    elapsed = int((datetime.now(timezone.utc) - started_at).total_seconds())
                    await stream("run.progress", {"progress": {
                        "phase": "thinking", "message": "正在思考",
                        "elapsed_seconds": elapsed,
                        "heartbeat_at": datetime.now(timezone.utc).isoformat(
                            timespec="seconds").replace("+00:00", "Z"),
                    }})
                    await asyncio.sleep(10)

            heartbeat_task = asyncio.create_task(heartbeat())
            preserve_for_recovery = False
            try:
                if control.cancel.is_set():
                    await stream("run.canceled", {
                        "code": "user_canceled", "detail": "用户取消"})
                elif adapter_name in {"codex", "claude", "hermes"}:
                    # These are real local coding agents and may legitimately
                    # run for hours; the Gateway's deadline only governs queue
                    # delivery, never an accepted local execution.
                    await adapter.execute(request, stream, control)
                else:
                    try:
                        deadline = datetime.fromisoformat(
                            request.deadline_at.replace("Z", "+00:00"))
                        remaining = max(0.01, (deadline - datetime.now(timezone.utc)).total_seconds())
                    except ValueError:
                        remaining = 1200
                    await asyncio.wait_for(
                        adapter.execute(request, stream, control), timeout=remaining)
            except asyncio.TimeoutError:
                await stream("run.failed", {"code": "timeout", "detail": "任务超过截止时间"})
            except asyncio.CancelledError:
                # Replacing/restarting Connector is an execution pause, not a
                # user cancellation. Leave the command in ``running`` so the
                # next process can recover the same Codex/Claude/Hermes run.
                preserve_for_recovery = True
                raise
            except Exception as exc:
                if adapter_name == "codex":
                    await stream("run.failed", {
                        "code": "unexpected_stop", "detail": "Codex 任务中途意外停止"})
                else:
                    await stream("run.failed", {
                        "code": "adapter_error", "detail": str(exc)[:500]})
            finally:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
                if not preserve_for_recovery:
                    self.spool.set_command_state(envelope.event_id, "finished")
