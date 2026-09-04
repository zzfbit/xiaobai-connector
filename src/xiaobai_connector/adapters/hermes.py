"""Hermes adapter with a deliberately explicit CLI contract.

Hermes installations vary.  The Connector can discover a ``hermes`` or
``hermes-cli`` executable and run it in print mode.  If a particular build
uses another command, the generated config accepts a ``command`` array; the
array may contain the literal ``{prompt}`` placeholder.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

from .base import Emit, LocalAgent, RunControl, RunRequest, request_prompt


class HermesAdapter:
    def __init__(self, definition: dict[str, Any] | None = None):
        self.definition = dict(definition or {})
        self.binary = (str(self.definition.get("binary") or "").strip()
                       or os.environ.get("XIAOBAI_HERMES_BIN")
                       or shutil.which("hermes") or shutil.which("hermes-cli") or "hermes")

    def discover(self) -> list[LocalAgent]:
        executable = bool(shutil.which(self.binary) or Path(self.binary).is_file())
        configured = any(Path(value).expanduser().is_dir() for value in (
            "~/.hermes", "~/.xiaobai/hermes"))
        status = "online" if executable else "configured" if configured else "offline"
        detail = "已找到 Hermes 命令" if executable else "已找到 Hermes 配置目录，但未找到 CLI" if configured else "没有找到 Hermes"
        return [LocalAgent(
            local_ref=str(self.definition.get("local_ref") or "hermes:default"),
            adapter="hermes", display_name=str(self.definition.get("display_name") or "Hermes"),
            mention_handle=str(self.definition.get("mention_handle") or "hermes"),
            capabilities=("chat", "stream"), status=status,
            enabled=bool(self.definition.get("enabled", executable)) and executable,
            presentation={"detail": detail},
        )]

    async def execute(self, request: RunRequest, emit: Emit,
                      control: RunControl) -> None:
        if not (shutil.which(self.binary) or Path(self.binary).is_file()):
            raise RuntimeError("Hermes 没有可运行的 CLI；请安装 Hermes CLI 或配置 command")
        workdir = Path(str(self.definition.get("workdir") or Path.home())).expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        configured = self.definition.get("command")
        if isinstance(configured, list) and configured:
            command = [str(item).replace("{prompt}", request_prompt(request)) for item in configured]
            stdin_prompt = None
        else:
            # Hermes' supported automation mode prints only the final answer
            # and is safe to invoke without a terminal.  The output is still
            # streamed line-by-line to the phone as it becomes available.
            command = [self.binary, "-z", request_prompt(request)]
            stdin_prompt = None
        proc = await asyncio.create_subprocess_exec(
            *command, cwd=str(workdir), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True, limit=16 * 1024 * 1024)
        try:
            if stdin_prompt is not None and proc.stdin is not None:
                proc.stdin.write((stdin_prompt + "\n").encode())
                await proc.stdin.drain()
                proc.stdin.close()
            await emit("run.accepted", {})
            await emit("run.started", {"adapter_session_id": request.run_id})
            sequence = 0
            while True:
                if proc.stdout is None:
                    break
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
                text = raw.decode(errors="replace")
                if text:
                    sequence += 1
                    await emit("run.output.delta", {"seq": sequence, "delta": text})
            if await proc.wait() != 0:
                await emit("run.failed", {"code": "hermes_failed", "detail": "Hermes 执行失败"})
            else:
                await emit("run.completed", {"adapter_session_id": request.run_id})
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
