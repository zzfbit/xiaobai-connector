"""Send follow-ups through the existing ChatGPT Desktop writer.

ChatGPT Desktop owns the long-lived app-server writer for desktop-created
threads. Opening another app-server connection from the Connector therefore
cannot reliably steer that turn. On macOS, ChatGPT Desktop exposes a local
peer-authenticated app-tools pipe; this module uses that existing writer and
falls back to the durable ``thread/queue/add`` protocol when the pipe is not
available. The bridge is deliberately disabled for non-ChatGPT binaries and
on Windows.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable

from .codex_sessions import CHATGPT_CODEX_BINARY


CHATGPT_CUA_NODE = Path(
    "/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node")
PIPE_ENV_NAME = "CODEX_APP_TOOLS_PIPE_PATH"
BRIDGE_DISABLE_ENV = "XIAOBAI_CODEX_DESKTOP_BRIDGE"
PIPE_RESPONSE_LIMIT = 64 * 1024 * 1024
NODE_TIMEOUT_SECONDS = 18.0


class CodexDesktopBridgeUnavailable(RuntimeError):
    """The desktop app-tools bridge is not available before sending."""


class CodexDesktopBridgeError(RuntimeError):
    """The desktop app-tools bridge was reached but rejected a call."""


# The app-tools transport is a four-byte little-endian length prefix followed
# by one UTF-8 JSON object. Keep the helper self-contained so Connector does
# not need to import private modules from ChatGPT Desktop.
_NODE_CLIENT = r'''
const net = require("net");

let chunks = [];
let finishedInput = false;
process.stdin.on("data", (chunk) => chunks.push(Buffer.from(chunk)));
process.stdin.on("end", () => {
  if (finishedInput) return;
  finishedInput = true;
  let input;
  try {
    input = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch (error) {
    process.stdout.write(JSON.stringify({ok: false, sent: false,
      error: "invalid bridge input"}));
    return;
  }

  const pipe = String(input.pipe || "");
  const request = input.request;
  if (!pipe || !request || typeof request !== "object") {
    process.stdout.write(JSON.stringify({ok: false, sent: false,
      error: "invalid bridge request"}));
    return;
  }

  let done = false;
  let sent = false;
  let received = Buffer.alloc(0);
  let socket;
  const timer = setTimeout(() => finish({ok: false, sent,
    error: "app-tools pipe response timeout"}), 15000);

  function finish(value) {
    if (done) return;
    done = true;
    clearTimeout(timer);
    process.stdout.write(JSON.stringify(value));
    if (socket && !socket.destroyed) socket.destroy();
  }

  try {
    socket = net.createConnection({path: pipe});
    socket.on("connect", () => {
      let body;
      try {
        body = Buffer.from(JSON.stringify(request), "utf8");
      } catch (error) {
        finish({ok: false, sent: false, error: "cannot encode bridge request"});
        return;
      }
      if (body.length > 0xffffffff) {
        finish({ok: false, sent: false, error: "bridge request too large"});
        return;
      }
      const frame = Buffer.allocUnsafe(4 + body.length);
      frame.writeUInt32LE(body.length, 0);
      body.copy(frame, 4);
      sent = true;
      socket.write(frame, (error) => {
        if (error) finish({ok: false, sent: true, error: String(error)});
      });
    });
    socket.on("data", (chunk) => {
      received = Buffer.concat([received, Buffer.from(chunk)]);
      if (received.length < 4) return;
      const size = received.readUInt32LE(0);
      if (size > %d) {
        finish({ok: false, sent, error: "app-tools response too large"});
        return;
      }
      if (received.length < 4 + size) return;
      try {
        const response = JSON.parse(received.subarray(4, 4 + size).toString("utf8"));
        finish({ok: true, sent, response});
      } catch (error) {
        finish({ok: false, sent, error: "invalid app-tools response"});
      }
    });
    socket.on("error", (error) => finish({ok: false, sent,
      error: String(error && error.message || error)}));
    socket.on("close", () => {
      if (!done) finish({ok: false, sent, error: "app-tools pipe closed"});
    });
  } catch (error) {
    finish({ok: false, sent, error: String(error && error.message || error)});
  }
});
'''.replace("%d", str(PIPE_RESPONSE_LIMIT))


NodeRunner = Callable[[Path, dict[str, Any]], Awaitable[dict[str, Any]]]


def _disabled() -> bool:
    value = os.environ.get(BRIDGE_DISABLE_ENV, "1").strip().lower()
    return value in {"0", "false", "no", "off", "disabled"}


class CodexDesktopBridge:
    """Connector-side client for ChatGPT Desktop's app-tools pipe."""

    def __init__(self, *, binary: str | None = None,
                 node_path: Path | None = None,
                 node_runner: NodeRunner | None = None) -> None:
        self.binary = str(binary or "").strip()
        self.node_path = Path(node_path or CHATGPT_CUA_NODE)
        self._node_runner = node_runner

    def enabled(self) -> bool:
        """Only the installed ChatGPT Desktop binary may use this bridge."""
        if _disabled() or not self.binary:
            return False
        try:
            return (Path(self.binary).expanduser().resolve()
                    == CHATGPT_CODEX_BINARY.resolve())
        except OSError:
            return False

    async def send_message(self, *, thread_id: str, text: str,
                           client_message_id: str) -> dict[str, Any]:
        """Ask Desktop to deliver a visible follow-up through its own writer."""
        thread = str(thread_id or "").strip()
        message = str(text or "").strip()
        message_id = str(client_message_id or "").strip()
        if (not thread or not message or not message_id
                or len(thread) > 160 or len(message) > 16_000
                or len(message_id) > 160):
            raise CodexDesktopBridgeError("Codex 插话参数无效")
        if not self.enabled():
            raise CodexDesktopBridgeUnavailable("ChatGPT Desktop bridge 未启用")
        if not self.node_path.is_file() or not os.access(self.node_path, os.X_OK):
            raise CodexDesktopBridgeUnavailable("ChatGPT Desktop bridge runtime 不可用")
        pipe = self._find_pipe()
        if pipe is None:
            raise CodexDesktopBridgeUnavailable("找不到 ChatGPT Desktop app-tools pipe")

        request = self._request(thread, message, message_id)
        if self._node_runner is not None:
            response = await self._node_runner(pipe, request)
        else:
            response = await self._run_node(pipe, request)
        if not isinstance(response, dict):
            raise CodexDesktopBridgeError("ChatGPT Desktop bridge 返回格式无效")
        if not bool(response.get("ok")):
            detail = str(response.get("error") or "ChatGPT Desktop bridge 调用失败")[:500]
            if (not bool(response.get("sent"))
                    or self._is_unavailable_transport(detail)):
                raise CodexDesktopBridgeUnavailable(detail)
            raise CodexDesktopBridgeError(detail)

        wire = response.get("response")
        if not isinstance(wire, dict):
            raise CodexDesktopBridgeError("ChatGPT Desktop bridge 响应格式无效")
        error = wire.get("error")
        if error:
            raise CodexDesktopBridgeError(str(error)[:500])
        result = wire.get("result")
        if isinstance(result, dict) and result.get("isError"):
            raise CodexDesktopBridgeError(self._tool_error(result))
        return dict(result) if isinstance(result, dict) else {}

    @staticmethod
    def _is_unavailable_transport(detail: str) -> bool:
        value = str(detail or "").lower()
        return any(marker in value for marker in (
            "app-tools pipe closed", "codex app tools pipe closed",
            "connection reset", "connection refused", "broken pipe", "enoent",
        ))

    @staticmethod
    def _request(thread_id: str, text: str, message_id: str) -> dict[str, Any]:
        return {
            "id": 1,
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "arguments": {"threadId": thread_id, "prompt": text},
                "callId": message_id,
                "namespace": "codex_app",
                "threadId": thread_id,
                "tool": "send_message_to_thread",
                "turnId": "xiaobai-connector:" + message_id,
            },
        }

    @staticmethod
    def _tool_error(result: dict[str, Any]) -> str:
        content = result.get("content")
        if isinstance(content, list):
            texts = [str(item.get("text") or "").strip()
                     for item in content if isinstance(item, dict)]
            detail = " ".join(item for item in texts if item)
            if detail:
                return detail[:500]
        return "ChatGPT Desktop app tool 拒绝了插话"

    def _find_pipe(self) -> Path | None:
        if not self.enabled():
            return None
        try:
            codex_binary = str(Path(self.binary).expanduser().resolve())
        except OSError:
            return None
        rows = self._process_rows()
        candidates: list[tuple[int, Path]] = []
        for pid, ppid, command in rows:
            if codex_binary not in command or "app-server" not in command:
                continue
            parent = self._process_command(ppid)
            if not self._is_chatgpt_parent(parent):
                continue
            full_command = self._process_environment_command(pid)
            match = re.search(
                rf"(?:^| ){re.escape(PIPE_ENV_NAME)}=([^ ]+)", full_command)
            if not match:
                continue
            path = Path(match.group(1)).expanduser()
            try:
                if not stat.S_ISSOCK(path.stat().st_mode):
                    continue
            except OSError:
                continue
            candidates.append((pid, path))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    @staticmethod
    def _is_chatgpt_parent(command: str) -> bool:
        return "/ChatGPT.app/Contents/MacOS/ChatGPT" in str(command or "")

    @staticmethod
    def _process_rows() -> list[tuple[int, int, str]]:
        try:
            completed = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,command="],
                check=False, capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.SubprocessError):
            return []
        rows: list[tuple[int, int, str]] = []
        for line in completed.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) != 3:
                continue
            try:
                rows.append((int(parts[0]), int(parts[1]), parts[2]))
            except ValueError:
                continue
        return rows

    @staticmethod
    def _process_command(pid: int) -> str:
        return CodexDesktopBridge._ps_command(["ps", "-p", str(pid), "-o", "command="])

    @staticmethod
    def _process_environment_command(pid: int) -> str:
        return CodexDesktopBridge._ps_command(["ps", "eww", "-p", str(pid), "-o", "command="])

    @staticmethod
    def _ps_command(args: list[str]) -> str:
        try:
            completed = subprocess.run(
                args, check=False, capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.SubprocessError):
            return ""
        return completed.stdout.strip()

    async def _run_node(self, pipe: Path, request: dict[str, Any]) -> dict[str, Any]:
        from .adapters.base import subprocess_options

        payload = json.dumps({"pipe": str(pipe), "request": request},
                             ensure_ascii=False).encode("utf-8")
        try:
            process = await asyncio.create_subprocess_exec(
                str(self.node_path), "-e", _NODE_CLIENT,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                **subprocess_options())
        except (OSError, asyncio.SubprocessError) as exc:
            raise CodexDesktopBridgeUnavailable(
                "无法启动 ChatGPT Desktop bridge runtime") from exc
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(payload), timeout=NODE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise CodexDesktopBridgeError("ChatGPT Desktop bridge 响应超时") from exc
        try:
            value = json.loads(stdout.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise CodexDesktopBridgeError("ChatGPT Desktop bridge 输出无效") from exc
        return dict(value) if isinstance(value, dict) else {}


__all__ = [
    "CodexDesktopBridge",
    "CodexDesktopBridgeError",
    "CodexDesktopBridgeUnavailable",
]
