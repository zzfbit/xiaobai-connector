"""Claude Code stream-json adapter."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .base import Emit, LocalAgent, RunControl, RunRequest, request_prompt


class ClaudeAdapter:
    def __init__(self, definition: dict[str, Any] | None = None):
        self.definition = dict(definition or {})
        self.binary = (str(self.definition.get("binary") or "").strip()
                       or os.environ.get("XIAOBAI_CLAUDE_BIN")
                       or shutil.which("claude") or "claude")

    def discover(self) -> list[LocalAgent]:
        available = bool(shutil.which(self.binary) or Path(self.binary).is_file())
        return [LocalAgent(
            local_ref=str(self.definition.get("local_ref") or "claude:default"),
            adapter="claude", display_name=str(self.definition.get("display_name") or "Claude Code"),
            mention_handle=str(self.definition.get("mention_handle") or "claude"),
            capabilities=("chat", "stream", "cancel"), status="online" if available else "offline",
            enabled=bool(self.definition.get("enabled", available)) and available,
        )]

    async def execute(self, request: RunRequest, emit: Emit,
                      control: RunControl) -> None:
        if not (shutil.which(self.binary) or Path(self.binary).is_file()):
            raise RuntimeError("没有找到 Claude Code 命令，请重新扫描本机 Agent")
        workdir = Path(str(self.definition.get("workdir") or Path.home())).expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        command = [self.binary, "--print", "--input-format", "stream-json",
                   "--output-format", "stream-json", "--verbose"]
        model = str(self.definition.get("model") or "").strip()
        if model:
            command.extend(["--model", model])
        proc = await asyncio.create_subprocess_exec(
            *command, cwd=str(workdir), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True, limit=64 * 1024 * 1024)
        started = False
        sequence = 0
        final_text = ""
        try:
            if proc.stdin is None or proc.stdout is None:
                raise RuntimeError("Claude Code I/O 不可用")
            proc.stdin.write((json.dumps({"type": "user", "message": {
                "role": "user", "content": request_prompt(request)}},
                ensure_ascii=False) + "\n").encode())
            await proc.stdin.drain()
            proc.stdin.close()
            await emit("run.accepted", {})
            while True:
                read_task = asyncio.create_task(proc.stdout.readline())
                cancel_task = asyncio.create_task(control.cancel.wait())
                done, _ = await asyncio.wait({read_task, cancel_task},
                                             return_when=asyncio.FIRST_COMPLETED)
                if cancel_task in done and control.cancel.is_set():
                    read_task.cancel()
                    await asyncio.gather(read_task, return_exceptions=True)
                    await emit("run.canceled", {"code": "user_canceled", "detail": "用户取消"})
                    return
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)
                raw = await read_task
                if not raw:
                    break
                try:
                    event = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                delta, final = self._visible_text(event)
                if delta:
                    if not started:
                        await emit("run.started", {"adapter_session_id": request.run_id})
                        started = True
                    sequence += 1
                    await emit("run.output.delta", {"seq": sequence, "delta": delta})
                if final:
                    final_text = final
            return_code = await proc.wait()
            if return_code != 0:
                await emit("run.failed", {"code": "claude_failed", "detail": "Claude Code 执行失败"})
                return
            if not started:
                await emit("run.started", {"adapter_session_id": request.run_id})
            if final_text and sequence == 0:
                await emit("run.output.delta", {"seq": 1, "delta": final_text})
            await emit("run.completed", {"adapter_session_id": request.run_id})
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()

    @staticmethod
    def _visible_text(event: Any) -> tuple[str, str]:
        if not isinstance(event, dict):
            return "", ""
        if event.get("type") == "stream_event":
            inner = event.get("event") or {}
            delta = inner.get("delta") if isinstance(inner, dict) else None
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                return str(delta.get("text") or ""), ""
        if event.get("type") == "assistant":
            content = ((event.get("message") or {}).get("content") or [])
            if isinstance(content, list):
                return "".join(str(item.get("text") or "") for item in content
                               if isinstance(item, dict) and item.get("type") == "text"), ""
        if event.get("type") == "result":
            return "", str(event.get("result") or "")
        return "", ""
