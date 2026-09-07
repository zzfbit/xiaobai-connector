"""Codex app-server v2 adapter.

The Connector talks to Codex's local ``app-server --stdio`` process instead of
driving a terminal window.  Only natural-language message deltas cross the
Connector boundary; tool output and internal JSON-RPC notifications are
consumed locally.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .. import __version__
from ..agent_identity import CODEX_AVATAR, CODEX_PRESENTATION, clone
from ..codex_sessions import CodexSessionClient, CodexSessionError
from ..codex_desktop_bridge import CodexDesktopBridge, CodexDesktopBridgeUnavailable
from .base import (Emit, LocalAgent, RunControl, RunRequest, direct_turn_text,
                   executable_available, executable_command, request_prompt,
                   subprocess_options)
from .codex_usage import CodexUsageExtensionAdapter


READ_LIMIT = 64 * 1024 * 1024
CHATGPT_CODEX_BINARY = Path(
    "/Applications/ChatGPT.app/Contents/Resources/codex")
INTERNAL_NOTIFICATION_METHODS = [
    "thread/status/changed", "thread/tokenUsage/updated", "turn/diff/updated",
    "turn/plan/updated", "hook/started", "hook/completed", "item/started",
    "item/completed", "item/plan/delta", "command/exec/outputDelta",
    "process/outputDelta", "process/exited", "item/commandExecution/outputDelta",
    "item/commandExecution/terminalInteraction", "item/fileChange/outputDelta",
    "item/fileChange/patchUpdated", "item/mcpToolCall/progress",
    "item/reasoning/summaryTextDelta", "item/reasoning/summaryPartAdded",
    "item/reasoning/textDelta",
]
RECOVERY_PROMPT = (
    "[Xiaobai internal recovery — do not display]\n"
    "The Connector was restarted while the previous Codex task was running. "
    "Continue from the work already completed, do not repeat completed work, "
    "and return the final result."
)


def _is_quota_error(value: Any = None) -> bool:
    raw = str(value or "").lower()
    return any(marker in raw for marker in (
        "quota", "rate limit", "rate_limit", "usage limit", "usage_limit",
        "limit_reached", "too many requests", "token limit", "token_limit",
        "token exhausted", "tokens exhausted", "5-hour limit", "5 hour limit",
        "five-hour limit", "five hour limit", "limit has been reached",
        "hit your limit", "额度", "限额", "用量",
    ))


def _unexpected_stop_detail(value: Any = None) -> str:
    return ("Codex 使用额度已耗尽，任务中途意外停止"
            if _is_quota_error(value) else "Codex 任务中途意外停止")


class CodexQueueError(RuntimeError):
    """A desktop-thread queue bridge failure with a retryable RPC hint."""

    def __init__(self, message: str, *, code: Any = None) -> None:
        super().__init__(message)
        self.code = code


def resolve_codex_binary(configured: str | None = None) -> str:
    """Use the Codex runtime shipped with the desktop app when available.

    The standalone ``codex`` executable and ChatGPT's bundled app-server can
    be different versions.  Desktop follow-ups use ``thread/queue/add``, which
    older standalone binaries do not understand.  An explicit environment
    override and a user-selected file path remain available for custom
    installations and tests.
    """
    explicit = str(os.environ.get("XIAOBAI_CODEX_BIN") or "").strip()
    if explicit:
        return explicit
    configured_value = str(configured or "").strip()
    configured_path = Path(configured_value).expanduser()
    if configured_value and configured_path.is_file():
        return configured_value
    if CHATGPT_CODEX_BINARY.is_file() and os.access(CHATGPT_CODEX_BINARY, os.X_OK):
        return str(CHATGPT_CODEX_BINARY)
    if configured_value and (
            Path(configured_value).expanduser().is_file()
            or shutil.which(configured_value)):
        return configured_value
    return shutil.which("codex") or configured_value or "codex"


def _codex_available(binary: str) -> bool:
    return executable_available(binary)


class CodexAdapter:
    def __init__(self, definition: dict[str, Any] | None = None):
        self.definition = dict(definition or {})
        self.binary = resolve_codex_binary(self.definition.get("binary"))
        self._last_history_snapshot: dict[str, Any] | None = None
        self._last_history_snapshot_at = 0.0
        self._last_capabilities: dict[str, Any] | None = None
        self._last_capabilities_at = 0.0

    def discover(self) -> list[LocalAgent]:
        available = _codex_available(self.binary)
        return [LocalAgent(
            local_ref=str(self.definition.get("local_ref") or "codex:default"),
            adapter="codex",
            display_name=str(self.definition.get("display_name") or "Codex"),
            mention_handle=str(self.definition.get("mention_handle") or "codex"),
            capabilities=tuple(self.definition.get("capabilities") or
                               ("chat", "stream", "cancel", "steer")),
            avatar=clone(self.definition.get("avatar")
                         if isinstance(self.definition.get("avatar"), dict)
                         else CODEX_AVATAR),
            presentation=clone(self.definition.get("presentation")
                               if isinstance(self.definition.get("presentation"), dict)
                               else CODEX_PRESENTATION),
            status="online" if available else "offline",
            enabled=bool(self.definition.get("enabled", available)) and available,
        )]

    def history_snapshots(self) -> list[dict[str, Any]]:
        """Project local Codex threads so the remote history page is portable."""
        import time
        now = time.monotonic()
        if self._last_history_snapshot is not None and now - self._last_history_snapshot_at < 1.5:
            snapshot = clone(self._last_history_snapshot)
        else:
            try:
                snapshot = CodexSessionClient(binary=self.binary).snapshot(limit=100)
            except (CodexSessionError, OSError, ValueError):
                return []
            self._last_history_snapshot = clone(snapshot)
            self._last_history_snapshot_at = now
        local_ref = str(self.definition.get("local_ref") or "codex:default")
        return [{"local_ref": local_ref, "source": "codex_threads_v1",
                 "threads": list(snapshot.get("threads") or []),
                 "capabilities": self._capability_snapshot()}]

    def _capability_snapshot(self) -> dict[str, Any]:
        import time

        now = time.monotonic()
        if self._last_capabilities is not None and now - self._last_capabilities_at < 30:
            return clone(self._last_capabilities)
        client = CodexSessionClient(binary=self.binary)
        try:
            models = self._public_models(client.models())
        except (CodexSessionError, OSError, ValueError):
            models = []
        try:
            usage = CodexUsageExtensionAdapter(client=client).read()
        except (CodexSessionError, OSError, ValueError):
            usage = {"limits": [], "plan_type": None}
        capabilities = {
            "models": models,
            "defaults": {
                "model": str(self.definition.get("model") or "gpt-5.6-luna"),
                "reasoningEffort": str(
                    self.definition.get("reasoning_effort") or "high"),
            },
            "usage": usage,
        }
        self._last_capabilities = clone(capabilities)
        self._last_capabilities_at = now
        return capabilities

    @staticmethod
    def _public_models(value: Any) -> list[dict[str, Any]]:
        """Keep only fields decoded by the mobile Codex model picker."""
        if not isinstance(value, list):
            return []
        result: list[dict[str, Any]] = []
        for raw in value[:100]:
            if not isinstance(raw, dict):
                continue
            model_id = str(raw.get("id") or raw.get("model") or "").strip()
            model_name = str(raw.get("model") or model_id).strip()
            if not model_id or not model_name:
                continue
            default_effort = str(raw.get("defaultReasoningEffort") or "high").strip()
            efforts: list[dict[str, str]] = []
            raw_efforts = raw.get("supportedReasoningEfforts") or []
            if isinstance(raw_efforts, list):
                for item in raw_efforts[:12]:
                    if isinstance(item, str):
                        effort = item.strip()
                        description = ""
                    elif isinstance(item, dict):
                        effort = str(item.get("reasoningEffort") or item.get("effort") or "").strip()
                        description = str(item.get("description") or "").strip()
                    else:
                        continue
                    if effort and len(effort) <= 80:
                        efforts.append({"reasoningEffort": effort[:80],
                                        "description": description[:240]})
            if not efforts and default_effort:
                efforts.append({"reasoningEffort": default_effort[:80], "description": ""})
            result.append({
                "id": model_id[:160], "model": model_name[:160],
                "displayName": str(raw.get("displayName") or raw.get("name") or model_name)[:200],
                "description": str(raw.get("description") or "")[:500],
                "defaultReasoningEffort": default_effort[:80],
                "supportedReasoningEfforts": efforts,
            })
        return result

    def status_snapshots(self) -> list[dict[str, Any]]:
        local_ref = str(self.definition.get("local_ref") or "codex:default")
        # The Connector's WebSocket connection is the Agent's liveness signal.
        # A local app-server/session scan can fail transiently (and can be
        # unavailable while Codex is starting), but that must not turn an
        # otherwise discovered Codex executable into an offline mobile Agent.
        discovered = self.discover()[0]
        if discovered.status != "online":
            return [{"local_ref": local_ref, "status": "offline"}]
        try:
            snapshot = CodexSessionClient(binary=self.binary).status(local_ref=local_ref)
        except (CodexSessionError, OSError, ValueError):
            return [{"local_ref": local_ref, "status": "online"}]
        if str(snapshot.get("status") or "").lower() == "busy":
            return [{"local_ref": local_ref, "status": "busy",
                     "progress": snapshot.get("progress") or {}}]
        return [{"local_ref": local_ref, "status": "online"}]

    async def execute(self, request: RunRequest, emit: Emit,
                      control: RunControl) -> None:
        if not _codex_available(self.binary):
            raise RuntimeError("没有找到 Codex 命令，请重新扫描本机 Agent")
        session = dict(request.payload.get("codex_session") or {})
        workdir = Path(str(session.get("cwd") or self.definition.get("workdir") or Path.home()))
        workdir = workdir.expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        policy = dict(request.payload.get("policy") or {})
        group_call = not bool(policy.get("owner_only", True))
        requested_thread_id = str(session.get("thread_id") or "").strip()
        recovering = bool(request.payload.get("_connector_recovery")) and bool(
            requested_thread_id)
        sandbox = str(session.get("sandbox") or self.definition.get("sandbox") or "workspace-write")
        model = str(session.get("model") or self.definition.get("model") or "gpt-5.6-luna")
        effort = str(session.get("reasoning_effort") or self.definition.get("reasoning_effort") or "high")
        proc = await asyncio.create_subprocess_exec(
            *executable_command(self.binary, "app-server", "--stdio"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True, limit=READ_LIMIT, **subprocess_options())
        next_id = 1
        thread_id = requested_thread_id
        turn_id = ""
        turn_request_id: int | None = None
        sequence = max(0, int(request.payload.get("_output_seq_start") or 0))
        started = False
        recovery_attempted = False
        pending_steer_ids: set[int] = set()
        read_task: asyncio.Task[bytes] | None = None
        steer_task: asyncio.Task[Any] | None = None
        deferred_steers: list[dict[str, Any]] = []

        async def send(method: str, params: dict[str, Any], request_id: int | None = None) -> None:
            if proc.stdin is None:
                raise RuntimeError("Codex app-server 输入流不可用")
            payload: dict[str, Any] = {"method": method, "params": params}
            if request_id is not None:
                payload["id"] = request_id
            proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
            await proc.stdin.drain()

        async def reply(request_id: Any, payload: dict[str, Any]) -> None:
            if proc.stdin is None:
                return
            proc.stdin.write((json.dumps({"id": request_id, "result": payload},
                                         ensure_ascii=False) + "\n").encode())
            await proc.stdin.drain()

        async def response(request_id: int) -> dict[str, Any]:
            if proc.stdout is None:
                raise RuntimeError("Codex app-server 输出流不可用")
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    raise RuntimeError("Codex app-server 提前退出")
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise RuntimeError("Codex app-server 返回了无效 JSON") from exc
                if not isinstance(value, dict):
                    raise RuntimeError("Codex app-server 返回格式无效")
                if value.get("id") == request_id:
                    if value.get("error"):
                        raise RuntimeError(str(value["error"])[:500])
                    return dict(value.get("result") or {})

        def recovery_prompt() -> str:
            """Carry the last user request into a replacement Codex thread."""
            if not thread_id:
                return RECOVERY_PROMPT
            try:
                history = CodexSessionClient(binary=self.binary).thread(thread_id)
            except (CodexSessionError, OSError, ValueError):
                return RECOVERY_PROMPT
            user_messages: list[str] = []
            for turn in history.get("turns") or []:
                if not isinstance(turn, dict):
                    continue
                for item in turn.get("items") or []:
                    if not isinstance(item, dict) or item.get("type") != "userMessage":
                        continue
                    parts = item.get("content") or []
                    text = "\n".join(str(part.get("text") or "") for part in parts
                                     if isinstance(part, dict)).strip()
                    if (text and text not in user_messages and text != "继续"
                            and not text.startswith(RECOVERY_PROMPT.split("\n", 1)[0])):
                        user_messages.append(text)
            if not user_messages:
                return RECOVERY_PROMPT
            return (RECOVERY_PROMPT + "\n\nOriginal user request from the interrupted "
                    "thread:\n" + user_messages[-1])

        thread_params: dict[str, Any] = {
            "cwd": str(workdir),
            "approvalPolicy": "never",
            "sandbox": sandbox,
            "experimentalRawEvents": False,
            "ephemeral": False,
        }
        if model:
            thread_params["model"] = model
        if bool(policy.get("message_agent_allowed")):
            thread_params["dynamicTools"] = [self.message_agent_tool_spec()]

        async def reconnect_and_continue(*, force_fresh_thread: bool = False) -> None:
            """Restart one lost app-server and continue the same durable task."""
            nonlocal proc, next_id, thread_id, turn_id, turn_request_id
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            proc = await asyncio.create_subprocess_exec(
                *executable_command(self.binary, "app-server", "--stdio"),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True, limit=READ_LIMIT, **subprocess_options())
            await send("initialize", {
                "clientInfo": {"name": "xiaobai-connector", "version": __version__},
                "capabilities": {
                    "experimentalApi": True,
                    "optOutNotificationMethods": INTERNAL_NOTIFICATION_METHODS,
                },
            }, next_id)
            await response(next_id)
            next_id += 1
            await send("initialized", {})
            resumed: dict[str, Any] | None = None
            if thread_id and not force_fresh_thread and not group_call:
                resume_params = dict(thread_params)
                resume_params["threadId"] = thread_id
                resume_params.pop("ephemeral", None)
                resume_params.pop("experimentalRawEvents", None)
                await send("thread/resume", resume_params, next_id)
                try:
                    resumed = await response(next_id)
                except RuntimeError:
                    resumed = None
                next_id += 1
            if resumed is None:
                fresh_params = dict(thread_params)
                fresh_params["ephemeral"] = bool(group_call)
                fresh_params.pop("threadId", None)
                await send("thread/start", fresh_params, next_id)
                resumed = await response(next_id)
                next_id += 1
            thread_id = str((resumed.get("thread") or {}).get("id") or thread_id)
            if not thread_id:
                raise RuntimeError("Codex 续跑时没有返回 thread id")
            turn_id = ""
            await emit("run.progress", {"progress": {
                "notice": "Codex 工具调用中断，正在自动继续。"}})
            turn_request_id = next_id
            await send("turn/start", {
                "threadId": thread_id,
                "input": [{"type": "text", "text": recovery_prompt()}],
                "model": model,
                "effort": effort,
                "summary": "auto",
                "clientUserMessageId": f"{request.run_id}:recovery",
            }, turn_request_id)
            next_id += 1

        async def cleanup() -> None:
            nonlocal read_task, steer_task
            for task in (read_task, steer_task):
                if task is not None and not task.done():
                    task.cancel()
            tasks = [task for task in (read_task, steer_task) if task is not None]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()

        try:
            await send("initialize", {
                "clientInfo": {"name": "xiaobai-connector", "version": __version__},
                "capabilities": {
                    "experimentalApi": True,
                    "optOutNotificationMethods": INTERNAL_NOTIFICATION_METHODS,
                },
            }, next_id)
            await response(next_id)
            next_id += 1
            await send("initialized", {})

            if requested_thread_id and not group_call:
                thread_params.pop("ephemeral", None)
                thread_params.pop("experimentalRawEvents", None)
                thread_params["threadId"] = thread_id
                await send("thread/resume", thread_params, next_id)
            else:
                # Group calls are context-only and must not become part of the
                # owner's persistent Codex desktop history.
                if group_call:
                    thread_params["ephemeral"] = True
                await send("thread/start", thread_params, next_id)
            thread_result = await response(next_id)
            next_id += 1
            thread_id = str((thread_result.get("thread") or {}).get("id") or thread_id)
            if not thread_id:
                raise RuntimeError("Codex 没有返回 thread id")

            turn_params: dict[str, Any] = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": recovery_prompt() if recovering
                           else request_prompt(request)}],
                "clientUserMessageId": (f"{request.run_id}:recovery"
                                         if recovering else request.run_id),
                "summary": "auto",
            }
            if model:
                turn_params["model"] = model
            if effort:
                turn_params["effort"] = effort
            turn_request_id = next_id
            await send("turn/start", turn_params, turn_request_id)
            next_id += 1
            await emit("run.accepted", {"adapter_session_id": thread_id})
            if proc.stdout is None:
                raise RuntimeError("Codex app-server 输出流不可用")
            steer_task = asyncio.create_task(control.steers.get())

            while True:
                if thread_id and turn_id and deferred_steers:
                    pending = deferred_steers.pop(0)
                    steer_request = RunRequest(
                        run_id=f"{request.run_id}-steer-deferred",
                        agent_id=request.agent_id, local_ref=request.local_ref,
                        text=str(pending.get("text") or "").strip(),
                        deadline_at=request.deadline_at,
                        payload={"input": {"attachments": list(
                            pending.get("attachments") or [])}},
                    )
                    text = direct_turn_text(steer_request)
                    if text:
                        await send("turn/steer", {
                            "threadId": thread_id, "expectedTurnId": turn_id,
                            "input": [{"type": "text", "text": text}],
                            "clientUserMessageId": str(
                                pending.get("client_message_id") or request.run_id + ":steer"),
                        }, next_id)
                        pending_steer_ids.add(next_id)
                        next_id += 1
                    continue
                if read_task is None:
                    read_task = asyncio.create_task(proc.stdout.readline())
                cancel_task = asyncio.create_task(control.cancel.wait())
                done, _ = await asyncio.wait(
                    {read_task, cancel_task, steer_task},
                    return_when=asyncio.FIRST_COMPLETED)
                if cancel_task in done and control.cancel.is_set():
                    for task in (read_task, steer_task):
                        if task is not None:
                            task.cancel()
                    await asyncio.gather(read_task, steer_task, return_exceptions=True)
                    if thread_id and turn_id:
                        await send("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, next_id)
                        next_id += 1
                    await emit("run.canceled", {"code": "user_canceled", "detail": "用户取消"})
                    return
                if steer_task in done:
                    value = steer_task.result()
                    steer_task = asyncio.create_task(control.steers.get())
                    if isinstance(value, dict) and thread_id and turn_id:
                        steer_request = RunRequest(
                            run_id=f"{request.run_id}-steer",
                            agent_id=request.agent_id, local_ref=request.local_ref,
                            text=str(value.get("text") or "").strip(),
                            deadline_at=request.deadline_at,
                            payload={"input": {"attachments": list(
                                value.get("attachments") or [])}},
                        )
                        text = direct_turn_text(steer_request)
                        if text:
                            await send("turn/steer", {
                                "threadId": thread_id, "expectedTurnId": turn_id,
                                "input": [{"type": "text", "text": text}],
                                "clientUserMessageId": str(value.get("client_message_id") or
                                                           request.run_id + ":steer"),
                            }, next_id)
                            pending_steer_ids.add(next_id)
                            next_id += 1
                    elif isinstance(value, dict):
                        # A steer can arrive between thread/start and the
                        # first turn/started notification. Keep it until the
                        # expected turn is known instead of silently dropping
                        # the user's message.
                        deferred_steers.append(value)
                    if read_task not in done:
                        cancel_task.cancel()
                        await asyncio.gather(cancel_task, return_exceptions=True)
                        continue
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)
                try:
                    raw = await read_task
                    read_task = None
                except (asyncio.LimitOverrunError, ValueError) as exc:
                    read_task = None
                    if not recovery_attempted:
                        recovery_attempted = True
                        await reconnect_and_continue(force_fresh_thread=True)
                        continue
                    raise RuntimeError("Codex app-server 内部输出超过适配器读取上限") from exc
                if not raw:
                    if not recovery_attempted:
                        recovery_attempted = True
                        await reconnect_and_continue()
                        continue
                    raise RuntimeError("Codex app-server 提前退出")
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    if not recovery_attempted:
                        recovery_attempted = True
                        await reconnect_and_continue(force_fresh_thread=True)
                        continue
                    raise RuntimeError("Codex app-server 返回了无效 JSON") from exc
                if not isinstance(value, dict):
                    raise RuntimeError("Codex app-server 返回格式无效")
                response_id = value.get("id")
                if value.get("method") == "item/tool/call":
                    await self._handle_dynamic_tool_call(
                        value, request=request, emit=emit, thread_id=thread_id,
                        turn_id=turn_id, reply=reply)
                    continue
                if isinstance(response_id, int) and response_id in pending_steer_ids:
                    pending_steer_ids.remove(response_id)
                    if value.get("error"):
                        await emit("run.progress", {"progress": {
                            "warning": "调整方向未生效：" + str(value["error"])[:240]}})
                    else:
                        await emit("run.progress", {"progress": {
                            "notice": "已将新消息插入当前 Codex 任务。"}})
                    continue
                if response_id == turn_request_id:
                    turn_request_id = None
                    if value.get("error"):
                        detail = str(value["error"])[:500]
                        if _is_quota_error(detail):
                            await emit("run.failed", {
                                "code": "unexpected_stop",
                                "detail": _unexpected_stop_detail(detail),
                            })
                            return
                        if not recovery_attempted:
                            recovery_attempted = True
                            await reconnect_and_continue(
                                force_fresh_thread="Separator" in detail)
                            continue
                        raise RuntimeError(detail)
                    turn = (value.get("result") or {}).get("turn") or {}
                    turn_id = str(turn.get("id") or turn_id)
                    if not started:
                        await emit("run.started", {"adapter_session_id": thread_id,
                                                    "adapter_turn_id": turn_id})
                        started = True
                    continue
                method = str(value.get("method") or "")
                params = value.get("params") or {}
                if method == "turn/started":
                    turn_id = str((params.get("turn") or {}).get("id") or turn_id)
                    if not started:
                        await emit("run.started", {"adapter_session_id": thread_id,
                                                    "adapter_turn_id": turn_id})
                        started = True
                elif method == "item/agentMessage/delta":
                    delta = str(params.get("delta") or "")
                    if delta:
                        if not started:
                            await emit("run.started", {"adapter_session_id": thread_id,
                                                        "adapter_turn_id": turn_id})
                            started = True
                        sequence += 1
                        await emit("run.output.delta", {"seq": sequence, "delta": delta})
                elif method == "turn/completed":
                    turn = params.get("turn") or {}
                    status = str(turn.get("status") or "failed")
                    if status == "completed":
                        await emit("run.completed", {"adapter_session_id": thread_id,
                                                      "adapter_turn_id": turn_id})
                    elif status == "interrupted" and control.cancel.is_set():
                        await emit("run.canceled", {"code": "user_canceled", "detail": "用户取消"})
                    elif _is_quota_error(turn.get("error")):
                        await emit("run.failed", {
                            "code": "unexpected_stop",
                            "detail": _unexpected_stop_detail(turn.get("error")),
                            })
                    elif not recovery_attempted:
                        recovery_attempted = True
                        await reconnect_and_continue(
                            force_fresh_thread="Separator" in str(turn.get("error") or ""))
                        continue
                    else:
                        await emit("run.failed", {
                            "code": "unexpected_stop",
                            "detail": _unexpected_stop_detail(turn.get("error")),
                        })
                    return
                elif method == "error":
                    detail = str(params.get("message") or "Codex 临时错误")[:500]
                    if _is_quota_error(detail):
                        await emit("run.failed", {
                            "code": "unexpected_stop",
                                "detail": _unexpected_stop_detail(detail),
                            })
                        return
                    await emit("run.progress", {"progress": {"warning": detail}})
                    if not recovery_attempted:
                        recovery_attempted = True
                        await reconnect_and_continue(
                            force_fresh_thread="Separator" in detail)
                        continue
                    raise RuntimeError(_unexpected_stop_detail(detail))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await emit("run.failed", {
                "code": "unexpected_stop", "detail": _unexpected_stop_detail(exc)})
        finally:
            await cleanup()

    async def queue_thread_message(self, *, thread_id: str, text: str,
                                   attachments: list[dict[str, Any]] | None = None,
                                   client_message_id: str) -> bool:
        """Deliver a phone message into a desktop-owned Codex thread.

        The ChatGPT desktop app owns the long-lived writer for the thread.  A
        Desktop-created thread should use its existing writer first. A
        short-lived ``thread/queue/add`` app-server bridge remains the durable
        restart-safe fallback.
        """
        if not _codex_available(self.binary):
            raise CodexQueueError("没有找到 Codex 命令，请重新扫描本机 Agent")
        request = RunRequest(
            run_id="queue-" + str(client_message_id or ""),
            agent_id=str(self.definition.get("agent_id") or "codex"),
            local_ref=str(self.definition.get("local_ref") or "codex:default"),
            text=str(text or "").strip(), deadline_at="",
            payload={"input": {"attachments": list(attachments or [])}},
        )
        queued_text = request_prompt(request)
        if not queued_text.strip():
            raise CodexQueueError("Codex 插话内容为空")
        if not str(thread_id or "").strip():
            raise CodexQueueError("Codex 会话 ID 为空")
        message_id = str(client_message_id or "").strip()

        # Desktop-created threads are owned by ChatGPT Desktop's long-lived
        # app-server writer. Use the Desktop app-tools bridge first so a phone
        # message reaches that writer while the turn is still active. Only a
        # genuinely unavailable bridge falls back to the durable queue; this
        # avoids duplicating a message after the Desktop host accepted it.
        try:
            await CodexDesktopBridge(binary=self.binary).send_message(
                thread_id=str(thread_id).strip(), text=queued_text,
                client_message_id=message_id)
            return True
        except CodexDesktopBridgeUnavailable:
            pass

        for attempt in range(3):
            try:
                await self._queue_once(
                    thread_id=str(thread_id).strip(), text=queued_text,
                    client_message_id=message_id)
                return True
            except OSError as exc:
                # Process creation and pipe writes can fail transiently while
                # Windows is restarting the local Codex desktop process. Keep
                # this on the queue error path so the Gateway returns a proper
                # retryable ACK instead of losing the WebSocket receiver task.
                queue_error = CodexQueueError(str(exc)[:500])
                if attempt == 2:
                    raise queue_error from exc
                await asyncio.sleep(0.4 * (attempt + 1))
            except CodexQueueError as exc:
                raw = str(exc).lower()
                code = str(getattr(exc, "code", "") or "")
                permanent = (
                    code in {"-32600", "invalid_request"}
                    or "no rollout found" in raw
                    or "invalid thread" in raw
                    or "找不到这条 codex 会话" in raw
                )
                if permanent or attempt == 2:
                    raise
                await asyncio.sleep(0.4 * (attempt + 1))
        return False

    async def _queue_once(self, *, thread_id: str, text: str,
                          client_message_id: str) -> None:
        """Send one queue/add request through a short-lived app-server bridge."""
        proc = await asyncio.create_subprocess_exec(
            *executable_command(self.binary, "app-server", "--stdio"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=READ_LIMIT,
            **subprocess_options(),
        )
        request_id = 1

        async def send(method: str, params: dict[str, Any], rpc_id: int) -> None:
            if proc.stdin is None:
                raise CodexQueueError("Codex app-server 输入流不可用")
            proc.stdin.write((json.dumps({
                "id": rpc_id, "method": method, "params": params,
            }, ensure_ascii=False) + "\n").encode())
            await proc.stdin.drain()

        async def response(rpc_id: int) -> dict[str, Any]:
            if proc.stdout is None:
                raise CodexQueueError("Codex app-server 输出流不可用")
            deadline = asyncio.get_running_loop().time() + 20
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise CodexQueueError("本机 Codex 队列响应超时")
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), remaining)
                except asyncio.TimeoutError as exc:
                    raise CodexQueueError("本机 Codex 队列响应超时") from exc
                if not raw:
                    raise CodexQueueError("Codex app-server 提前退出")
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(value, dict) or value.get("id") != rpc_id:
                    continue
                error = value.get("error")
                if error:
                    if isinstance(error, dict):
                        raise CodexQueueError(
                            str(error.get("message") or error)[:500],
                            code=error.get("code"))
                    raise CodexQueueError(str(error)[:500])
                result = value.get("result")
                return dict(result) if isinstance(result, dict) else {}
        try:
            await send("initialize", {
                "clientInfo": {"name": "xiaobai-connector", "version": __version__},
                "capabilities": {
                    "experimentalApi": True,
                    "optOutNotificationMethods": INTERNAL_NOTIFICATION_METHODS,
                },
            }, request_id)
            await response(request_id)
            request_id += 1
            if proc.stdin is None:
                raise CodexQueueError("Codex app-server 输入流不可用")
            proc.stdin.write(b'{"method":"initialized","params":{}}\n')
            await proc.stdin.drain()
            await send("thread/queue/add", {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
                "clientUserMessageId": client_message_id,
            }, request_id)
            await response(request_id)
        except CodexQueueError:
            raise
        except (OSError, asyncio.CancelledError):
            raise
        except Exception as exc:
            raise CodexQueueError(str(exc)[:500]) from exc
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()

    @staticmethod
    def message_agent_tool_spec() -> dict[str, Any]:
        return {
            "type": "function", "name": "message_agent",
            "description": "向当前家庭群中一个已加入的协作 Agent 投递问题。仅投递，不等待回复。",
            "inputSchema": {"type": "object", "additionalProperties": False,
                "properties": {"target": {"type": "string", "minLength": 1, "maxLength": 100},
                               "message": {"type": "string", "minLength": 1, "maxLength": 16000}},
                "required": ["target", "message"]},
        }

    @staticmethod
    async def _handle_dynamic_tool_call(value: dict[str, Any], *, request: RunRequest,
                                        emit: Emit, thread_id: str, turn_id: str,
                                        reply) -> None:
        request_id = value.get("id")
        params = dict(value.get("params") or {})
        failure = lambda message: {
            "contentItems": [{"type": "inputText", "text": message}],
            "success": False,
        }
        if (request_id is None or params.get("tool") != "message_agent"
                or params.get("threadId") != thread_id
                or (turn_id and params.get("turnId") != turn_id)):
            await reply(request_id, failure("message_agent 调用与当前 Codex turn 不匹配。"))
            return
        arguments = params.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = None
        if not isinstance(arguments, dict) or set(arguments) != {"target", "message"}:
            await reply(request_id, failure("message_agent 只接受 target 和 message。"))
            return
        try:
            result = await emit("agent.message.send", {
                "target": str(arguments["target"]), "message": str(arguments["message"]),
            })
            child = str((result or {}).get("child_run_id") or "")
            message = "已投递给群内协作 Agent；回复会异步回到本群。"
            if child:
                message += "（任务已受理）"
            await reply(request_id, {"contentItems": [{"type": "inputText", "text": message}],
                                     "success": True})
        except Exception as exc:
            await reply(request_id, failure("投递未完成：" + str(exc)[:160]))
