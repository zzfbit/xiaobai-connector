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
from .base import Emit, LocalAgent, RunControl, RunRequest, executable_command, request_prompt


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
    path = Path(binary).expanduser()
    return bool(shutil.which(binary) or (path.is_file() and os.access(path, os.X_OK)))


class CodexAdapter:
    def __init__(self, definition: dict[str, Any] | None = None):
        self.definition = dict(definition or {})
        self.binary = resolve_codex_binary(self.definition.get("binary"))

    def discover(self) -> list[LocalAgent]:
        available = _codex_available(self.binary)
        return [LocalAgent(
            local_ref=str(self.definition.get("local_ref") or "codex:default"),
            adapter="codex",
            display_name=str(self.definition.get("display_name") or "Codex"),
            mention_handle=str(self.definition.get("mention_handle") or "codex"),
            capabilities=tuple(self.definition.get("capabilities") or
                               ("chat", "stream", "cancel", "steer")),
            status="online" if available else "offline",
            enabled=bool(self.definition.get("enabled", available)) and available,
        )]

    async def execute(self, request: RunRequest, emit: Emit,
                      control: RunControl) -> None:
        if not _codex_available(self.binary):
            raise RuntimeError("没有找到 Codex 命令，请重新扫描本机 Agent")
        session = dict(request.payload.get("codex_session") or {})
        workdir = Path(str(session.get("cwd") or self.definition.get("workdir") or Path.home()))
        workdir = workdir.expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        sandbox = str(session.get("sandbox") or self.definition.get("sandbox") or "workspace-write")
        model = str(session.get("model") or self.definition.get("model") or "")
        effort = str(session.get("reasoning_effort") or self.definition.get("reasoning_effort") or "")
        proc = await asyncio.create_subprocess_exec(
            *executable_command(self.binary, "app-server", "--stdio"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True, limit=READ_LIMIT)
        next_id = 1
        thread_id = str(session.get("thread_id") or "").strip()
        turn_id = ""
        turn_request_id: int | None = None
        sequence = 0
        started = False
        read_task: asyncio.Task[bytes] | None = None
        steer_task: asyncio.Task[Any] | None = None

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
                value = json.loads(raw)
                if value.get("id") == request_id:
                    if value.get("error"):
                        raise RuntimeError(str(value["error"])[:500])
                    return dict(value.get("result") or {})

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
                "clientInfo": {"name": "xiaobai-connector", "version": "0.1.0"},
                "capabilities": {
                    "experimentalApi": True,
                    "optOutNotificationMethods": INTERNAL_NOTIFICATION_METHODS,
                },
            }, next_id)
            await response(next_id)
            next_id += 1
            await send("initialized", {})

            thread_params: dict[str, Any] = {
                "cwd": str(workdir),
                "approvalPolicy": "never",
                "sandbox": sandbox,
                "experimentalRawEvents": False,
                "ephemeral": False,
            }
            if model:
                thread_params["model"] = model
            if thread_id:
                thread_params["threadId"] = thread_id
                await send("thread/resume", thread_params, next_id)
            else:
                await send("thread/start", thread_params, next_id)
            thread_result = await response(next_id)
            next_id += 1
            thread_id = str((thread_result.get("thread") or {}).get("id") or thread_id)
            if not thread_id:
                raise RuntimeError("Codex 没有返回 thread id")

            turn_params: dict[str, Any] = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": request_prompt(request)}],
                "clientUserMessageId": request.run_id,
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
                if read_task is None:
                    read_task = asyncio.create_task(proc.stdout.readline())
                cancel_task = asyncio.create_task(control.cancel.wait())
                done, _ = await asyncio.wait(
                    {read_task, cancel_task, steer_task},
                    return_when=asyncio.FIRST_COMPLETED)
                if cancel_task in done and control.cancel.is_set():
                    read_task.cancel()
                    await asyncio.gather(read_task, return_exceptions=True)
                    if thread_id and turn_id:
                        await send("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, next_id)
                        next_id += 1
                    await emit("run.canceled", {"code": "user_canceled", "detail": "用户取消"})
                    return
                if steer_task in done:
                    value = steer_task.result()
                    steer_task = asyncio.create_task(control.steers.get())
                    if isinstance(value, dict) and thread_id and turn_id:
                        text = str(value.get("text") or "").strip()
                        if text:
                            await send("turn/steer", {
                                "threadId": thread_id, "expectedTurnId": turn_id,
                                "input": [{"type": "text", "text": text}],
                                "clientUserMessageId": str(value.get("client_message_id") or
                                                           request.run_id + ":steer"),
                            }, next_id)
                            next_id += 1
                    if read_task not in done:
                        cancel_task.cancel()
                        await asyncio.gather(cancel_task, return_exceptions=True)
                        continue
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)
                raw = await read_task
                read_task = None
                if not raw:
                    raise RuntimeError("Codex app-server 提前退出")
                value = json.loads(raw)
                response_id = value.get("id")
                if response_id == turn_request_id:
                    if value.get("error"):
                        raise RuntimeError(str(value["error"])[:500])
                    turn = (value.get("result") or {}).get("turn") or {}
                    turn_id = str(turn.get("id") or turn_id)
                    if not started:
                        await emit("run.started", {"adapter_session_id": thread_id,
                                                    "adapter_turn_id": turn_id})
                        started = True
                    continue
                if value.get("method") == "item/tool/call":
                    await reply(value.get("id"), {
                        "contentItems": [{"type": "inputText", "text": "Connector 暂不支持此动态工具。"}],
                        "success": False,
                    })
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
                    else:
                        await emit("run.failed", {"code": "codex_failed",
                                                   "detail": str(turn.get("error") or "Codex 执行失败")[:500]})
                    return
                elif method == "error":
                    raise RuntimeError(str(params.get("message") or "Codex 临时错误")[:500])
        except json.JSONDecodeError as exc:
            raise RuntimeError("Codex app-server 返回了无效 JSON") from exc
        finally:
            await cleanup()

    async def queue_thread_message(self, *, thread_id: str, text: str,
                                   attachments: list[dict[str, Any]] | None = None,
                                   client_message_id: str) -> bool:
        """Insert a phone message into a desktop-owned Codex thread.

        The ChatGPT desktop app owns the long-lived writer for the thread.  A
        short-lived app-server bridge may therefore only call
        ``thread/queue/add``; opening another ``thread/resume`` would race the
        desktop process and make the phone message appear to disappear.
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

        proc = await asyncio.create_subprocess_exec(
            *executable_command(self.binary, "app-server", "--stdio"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True, limit=READ_LIMIT)
        request_id = 1

        async def send(method: str, params: dict[str, Any], rpc_id: int) -> None:
            if proc.stdin is None:
                raise CodexQueueError("Codex app-server 输入流不可用")
            value = {"id": rpc_id, "method": method, "params": params}
            proc.stdin.write((json.dumps(value, ensure_ascii=False) + "\n").encode())
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
                        message = str(error.get("message") or error)[:500]
                        raise CodexQueueError(message, code=error.get("code"))
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
                "threadId": str(thread_id).strip(),
                "input": [{"type": "text", "text": queued_text}],
                "clientUserMessageId": str(client_message_id or "").strip(),
            }, request_id)
            await response(request_id)
            return True
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
