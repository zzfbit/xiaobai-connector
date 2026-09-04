"""Reconnect-capable Connector WebSocket runtime."""

from __future__ import annotations

import asyncio
import random
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .adapters.codex import CodexQueueError
from .adapters.router import AdapterRouter
from .config import ConnectorConfig
from .models import RunControl, RunRequest
from .protocol import ack, decode, make
from .spool import Spool


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
                    "agents.snapshot", "agent.status", "pong", "ack",
                    "run.accepted", "run.started", "run.output.delta", "run.progress",
                    "run.awaiting_input", "run.awaiting_approval", "run.completed",
                    "run.failed", "run.canceled", "run.steer", "codex.thread.queue",
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
            self._status("online", f"已连接，{len(self.registered_agents)} 个 Agent")
            await self._hold(websocket, lock, stop)

    async def _hold(self, websocket: Any, lock: asyncio.Lock, stop: asyncio.Event) -> None:
        sender = asyncio.create_task(self._event_sender(websocket, lock))
        receiver = asyncio.create_task(self._event_receiver(websocket, lock))
        stop_task = asyncio.create_task(stop.wait())
        waiters = {sender, receiver, stop_task}
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

    async def _event_sender(self, websocket: Any, lock: asyncio.Lock) -> None:
        while True:
            rows = self.spool.pending_events()
            for row in rows:
                envelope = make(row["event_type"], self.config.device_id,
                                self.connection_id, row["payload"], event_id=row["event_id"])
                async with lock:
                    await websocket.send(envelope.dumps())
                self.spool.mark_sent(row["event_id"])
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
                self.spool.acknowledge(envelope.event_id,
                                       bool(envelope.payload.get("accepted")))
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
                if envelope.type in {"run.start", "agent.notification", "codex.thread.queue"}:
                    self._validate_command(envelope.payload)
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
                async with lock:
                    await websocket.send(ack(envelope, accepted=False, error={
                        "code": "invalid_command", "message": str(exc)[:500],
                        "retryable": False}).dumps())

    def _validate_command(self, payload: dict[str, Any]) -> None:
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
        if not bool(match.get("enabled")) or not self.router.contains(local_ref):
            raise ValueError("Agent 未启用")

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
        return False

    async def _execute(self, envelope: Any, control: RunControl) -> None:
        payload = envelope.payload
        run_id = str(payload.get("run_id") or "")
        local_ref = str(payload.get("local_ref") or "")
        request = RunRequest(
            run_id=run_id, agent_id=str(payload.get("agent_id") or ""),
            local_ref=local_ref,
            text=str((payload.get("input") or {}).get("text") or ""),
            deadline_at=str(payload.get("deadline_at") or ""), payload=payload)
        adapter = self.router.adapter_for(local_ref)
        lock = self._agent_locks.setdefault(local_ref, asyncio.Lock())
        async with lock:
            self.spool.set_command_state(envelope.event_id, "running")
            async def emit(event_type: str, value: dict[str, Any]) -> Any:
                terminal = {"run.completed", "run.failed", "run.canceled"}
                if event_type in terminal and any(
                        self.spool.event_count(run_id, kind) for kind in terminal):
                    return None
                return self.spool.enqueue_event(run_id, event_type, value)

            heartbeat = asyncio.create_task(self._heartbeat(run_id, emit))
            try:
                if control.cancel.is_set():
                    await emit("run.canceled", {"code": "user_canceled", "detail": "用户取消"})
                else:
                    await adapter.execute(request, emit, control)
            except asyncio.CancelledError:
                await emit("run.failed", {"code": "connector_stopped",
                                           "detail": "Connector 停止，任务已终止"})
                raise
            except Exception as exc:
                await emit("run.failed", {"code": "adapter_error", "detail": str(exc)[:500]})
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                self.spool.set_command_state(envelope.event_id, "finished")

    async def _heartbeat(self, run_id: str, emit: Callable[..., Any]) -> None:
        started_at = datetime.now(timezone.utc)
        while True:
            elapsed = int((datetime.now(timezone.utc) - started_at).total_seconds())
            await emit("run.progress", {"progress": {
                "phase": "thinking", "message": "正在思考",
                "elapsed_seconds": elapsed,
                "heartbeat_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds").replace("+00:00", "Z"),
            }})
            await asyncio.sleep(10)
