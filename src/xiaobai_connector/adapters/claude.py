"""Claude Code stream-json adapter."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .base import Emit, LocalAgent, RunControl, RunRequest, direct_turn_text, executable_command, request_prompt


MCP_BRIDGE = Path(__file__).resolve().parents[1] / "message_agent_mcp.py"
CLAUDE_STDOUT_LIMIT = 64 * 1024 * 1024
CLAUDE_RECOVERY_PROMPT = (
    "[Xiaobai internal recovery — do not display]\n"
    "The Connector was restarted while the previous task was running. Continue "
    "the previous task from the existing Claude conversation, do not repeat work "
    "that is already complete, and return the final result."
)


class _MessageAgentBridge:
    """One-run loopback bridge; the child process receives only an opaque token."""

    def __init__(self, emit: Emit):
        self.emit = emit
        self.token = secrets.token_urlsafe(32)
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> dict[str, str]:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        sock = self.server.sockets[0]
        host, port = sock.getsockname()[:2]
        return {"XIAOBAI_MCP_BRIDGE": f"{host}:{port}",
                "XIAOBAI_MCP_TOKEN": self.token}

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=12)
            value = json.loads(raw)
            if not isinstance(value, dict) or not secrets.compare_digest(
                    str(value.get("token") or ""), self.token):
                result: dict[str, Any] = {"ok": False, "error": "协作工具鉴权失败"}
            else:
                response = await self.emit("agent.message.send", {
                    "target": str(value.get("target") or ""),
                    "message": str(value.get("message") or ""),
                })
                result = {"ok": True,
                          "child_run_id": str((response or {}).get("child_run_id") or "")}
        except Exception as exc:
            result = {"ok": False, "error": str(exc)[:200]}
        writer.write((json.dumps(result, ensure_ascii=False) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()


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
        policy = dict(request.payload.get("policy") or {})
        group_call = not bool(policy.get("owner_only", True))
        session_id = str(request.payload.get("claude_session_id") or "").strip()
        if not session_id:
            session_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL, "xiaobai:claude:" + request.run_id))
        recovering = bool(request.payload.get("_connector_recovery")) and bool(
            request.payload.get("claude_session_id"))
        prompt = (CLAUDE_RECOVERY_PROMPT if recovering
                  else (request_prompt(request) if group_call else direct_turn_text(request)))
        bridge: _MessageAgentBridge | None = None
        config_path: Path | None = None
        if group_call and bool(policy.get("message_agent_allowed")):
            bridge = _MessageAgentBridge(emit)
            config_path = self._mcp_config(await bridge.start())
        command = executable_command(
            self.binary, "--print", "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose")
        command.extend(["--session-id", session_id])
        if recovering:
            command.extend(["--resume", session_id])
        if config_path is not None:
            command.extend(["--mcp-config", str(config_path)])
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
                "role": "user", "content": prompt}},
                ensure_ascii=False) + "\n").encode())
            await proc.stdin.drain()
            proc.stdin.close()
            await emit("run.accepted", {"adapter_session_id": session_id})
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
            await emit("run.completed", {"adapter_session_id": session_id})
        finally:
            if config_path is not None:
                config_path.unlink(missing_ok=True)
            if bridge is not None:
                await bridge.close()
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()

    @staticmethod
    def _mcp_config(environment: dict[str, str]) -> Path:
        if getattr(sys, "frozen", False):
            command, args = sys.executable, ["--message-agent-mcp"]
        else:
            command, args = sys.executable, [str(MCP_BRIDGE)]
        return ClaudeAdapter._write_mcp_config({"mcpServers": {
            "xiaobai-message-agent": {
                "command": command,
                "args": args,
                "env": environment,
            },
        }})

    @staticmethod
    def _write_mcp_config(value: dict[str, Any]) -> Path:
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="xiaobai-claude-mcp-",
            suffix=".json", delete=False)
        try:
            json.dump(value, handle, ensure_ascii=False)
            handle.write("\n")
            return Path(handle.name)
        finally:
            handle.close()

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
