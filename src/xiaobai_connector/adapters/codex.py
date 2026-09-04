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

from .base import Emit, LocalAgent, RunControl, RunRequest, request_prompt


READ_LIMIT = 64 * 1024 * 1024
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


class CodexAdapter:
    def __init__(self, definition: dict[str, Any] | None = None):
        self.definition = dict(definition or {})
        self.binary = (str(self.definition.get("binary") or "").strip()
                       or os.environ.get("XIAOBAI_CODEX_BIN")
                       or shutil.which("codex") or "codex")

    def discover(self) -> list[LocalAgent]:
        available = bool(shutil.which(self.binary) or Path(self.binary).is_file())
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
        if not (shutil.which(self.binary) or Path(self.binary).is_file()):
            raise RuntimeError("没有找到 Codex 命令，请重新扫描本机 Agent")
        session = dict(request.payload.get("codex_session") or {})
        workdir = Path(str(session.get("cwd") or self.definition.get("workdir") or Path.home()))
        workdir = workdir.expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        sandbox = str(session.get("sandbox") or self.definition.get("sandbox") or "workspace-write")
        model = str(session.get("model") or self.definition.get("model") or "")
        effort = str(session.get("reasoning_effort") or self.definition.get("reasoning_effort") or "")
        proc = await asyncio.create_subprocess_exec(
            self.binary, "app-server", "--stdio", stdin=asyncio.subprocess.PIPE,
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
