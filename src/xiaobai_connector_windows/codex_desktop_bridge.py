"""Send follow-ups through the existing Windows ChatGPT/Codex writer.

The Windows Codex desktop app owns the long-lived app-server writer for
desktop-created threads. Its ``codex-app-tools`` MCP child receives the
``CODEX_APP_TOOLS_PIPE_PATH`` and ``CODEX_MCP_NODE_PATH`` values in its
command line. This module discovers that child, reuses the Windows named pipe,
and sends the same framed JSON-RPC ``tools/call`` used by the macOS bridge.

The public Connector API stays identical to the macOS implementation. The
adapter still falls back to the durable ``thread/queue/add`` protocol when the
desktop app is not running, is updating, or does not expose its pipe.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable

PIPE_ENV_NAME = "CODEX_APP_TOOLS_PIPE_PATH"
EXPLICIT_PIPE_ENV = "XIAOBAI_CODEX_APP_TOOLS_PIPE_PATH"
EXPLICIT_NODE_ENV = "XIAOBAI_CODEX_CUA_NODE"
NODE_ENV_NAMES = (
    "CODEX_MCP_NODE_PATH",
    "CODEX_BROWSER_USE_NODE_PATH",
)
BRIDGE_DISABLE_ENV = "XIAOBAI_CODEX_DESKTOP_BRIDGE"
PIPE_RESPONSE_LIMIT = 64 * 1024 * 1024
NODE_TIMEOUT_SECONDS = 18.0
WINDOWS_PIPE_PREFIX = "\\\\.\\pipe\\"
WINDOWS_PIPE_ALT_PREFIX = "\\\\?\\pipe\\"


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


def _decode_process_value(value: str) -> str:
    """Decode a JSON/TOML-escaped value embedded in a Win32 command line."""
    raw = str(value or "").strip()
    # The Codex command line escapes the closing quote as ``\\\"``. That
    # backslash belongs to the command-line quoting, not to the path.
    if raw.endswith("\\"):
        raw = raw[:-1]
    try:
        decoded = json.loads('"' + raw + '"')
    except (TypeError, ValueError, UnicodeError):
        # Be tolerant of a future launcher that writes ordinary Windows paths
        # (for example ``C:\\Users\\...`` with only one level of escaping).
        decoded = re.sub(
            r"\\{2,}",
            lambda match: "\\" * ((len(match.group(0)) + 1) // 2),
            raw,
        )
    return str(decoded if isinstance(decoded, str) else raw).strip()


def _normalize_pipe_path(value: str | None) -> str | None:
    """Accept only local Windows named-pipe paths."""
    decoded = _decode_process_value(str(value or "")).strip().strip('"')
    if not decoded or "\x00" in decoded or len(decoded) > 512:
        return None
    single_prefixes = ("\\.\\pipe\\", "\\?\\pipe\\")
    lowered = decoded.lower()
    if lowered.startswith(tuple(prefix.lower() for prefix in single_prefixes)):
        decoded = "\\" + decoded
        lowered = decoded.lower()
    valid_prefixes = (WINDOWS_PIPE_PREFIX, WINDOWS_PIPE_ALT_PREFIX)
    if not lowered.startswith(tuple(prefix.lower() for prefix in valid_prefixes)):
        return None
    if len(decoded) <= len(WINDOWS_PIPE_PREFIX):
        return None
    return decoded


def _command_value(command: str, name: str) -> str | None:
    """Extract a quoted ``name=value`` entry from Codex's app-server args.

    Windows ``Win32_Process.CommandLine`` contains the serialized ``-c``
    configuration. The JSON-like object is itself escaped for the command
    line, so the key appears once in ``env_vars`` and once in ``env``. Only
    the latter occurrence has an equals sign; the small scanner below avoids
    accidentally consuming the first occurrence.
    """
    source = str(command or "")
    needle = str(name or "")
    if not source or not needle:
        return None
    cursor = 0
    while True:
        index = source.lower().find(needle.lower(), cursor)
        if index < 0:
            return None
        after_name = index + len(needle)
        equals = source.find("=", after_name, after_name + 16)
        if equals < 0:
            cursor = after_name
            continue
        quote = source.find('"', equals + 1, equals + 8)
        if quote < 0:
            cursor = after_name
            continue
        closing = source.find('"', quote + 1)
        if closing < 0:
            return None
        return _decode_process_value(source[quote + 1:closing])


def _binary_name(value: str) -> str:
    return re.split(r"[\\/]", str(value or '').strip().strip('"'))[-1].lower()


class CodexDesktopBridge:
    """Connector-side client for ChatGPT Desktop's app-tools pipe."""

    def __init__(self, *, binary: str | None = None,
                 node_path: Path | None = None,
                 node_runner: NodeRunner | None = None) -> None:
        self.binary = str(binary or "").strip()
        self.node_path = (Path(node_path).expanduser()
                          if node_path is not None else None)
        self._discovered_node_path: Path | None = None
        self._node_runner = node_runner

    def enabled(self) -> bool:
        """Only the Windows Codex executable may use this bridge."""
        if _disabled() or not self.binary:
            return False
        return _binary_name(self.binary) in {
            "codex", "codex.exe", "codex.cmd", "codex.bat",
        }

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
        pipe = self._find_pipe()
        if pipe is None:
            raise CodexDesktopBridgeUnavailable("找不到 ChatGPT Desktop app-tools pipe")
        node_path = self._resolve_node_path()
        if node_path is None:
            raise CodexDesktopBridgeUnavailable("ChatGPT Desktop bridge runtime 不可用")
        self.node_path = node_path

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
        if (isinstance(result, dict)
                and (result.get("isError") or result.get("success") is False)):
            raise CodexDesktopBridgeError(self._tool_error(result))
        return dict(result) if isinstance(result, dict) else {}

    @staticmethod
    def _is_unavailable_transport(detail: str) -> bool:
        value = str(detail or "").lower()
        return any(marker in value for marker in (
            "app-tools pipe closed", "codex app tools pipe closed",
            "connection reset", "connection refused", "econnrefused",
            "broken pipe", "enoent", "named pipe",
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
        content = result.get("content") or result.get("contentItems")
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

        # This override is useful for diagnostics and for installations that
        # run the Connector under a different Windows account. The normal
        # path below discovers the value from the desktop app-server child.
        explicit_pipe = _normalize_pipe_path(
            os.environ.get(EXPLICIT_PIPE_ENV) or os.environ.get(PIPE_ENV_NAME))
        if explicit_pipe:
            explicit_node = self._configured_node_path()
            if explicit_node is not None:
                self._discovered_node_path = explicit_node
            return Path(explicit_pipe)

        rows = self._process_rows()
        candidates: list[tuple[int, Path, Path | None]] = []
        for pid, ppid, command in rows:
            if "app-server" not in str(command or "").lower():
                continue
            if not self._matches_codex_binary(command):
                continue
            parent = self._process_command(ppid)
            if not self._is_chatgpt_parent(parent):
                continue
            pipe_value = _command_value(command, PIPE_ENV_NAME)
            pipe_name = _normalize_pipe_path(pipe_value)
            if not pipe_name:
                continue
            node_value = None
            for node_env_name in NODE_ENV_NAMES:
                node_value = _command_value(command, node_env_name)
                if node_value:
                    break
            node_name = Path(str(node_value)).expanduser() if node_value else None
            candidates.append((pid, Path(pipe_name), node_name))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        _, pipe, node = candidates[0]
        if node is not None:
            self._discovered_node_path = node
        return pipe

    def _matches_codex_binary(self, command: str) -> bool:
        source = str(command or "").lower().replace("/", "\\")
        configured = str(self.binary or "").strip().strip('"').lower().replace("/", "\\")
        if configured and configured in source:
            return True
        name = _binary_name(self.binary)
        if name in {"codex", "codex.exe", "codex.cmd", "codex.bat"}:
            # The PATH entry on Windows is often ``codex.CMD`` while the
            # desktop app launches the versioned ``codex.exe`` directly.
            names = ("codex", "codex.exe", "codex.cmd", "codex.bat")
        else:
            return False
        return any(re.search(
            rf"(?:^|[\\/\s\"]){re.escape(item)}(?=$|[\\/\s\"])",
            source,
        ) for item in names)

    @staticmethod
    def _is_chatgpt_parent(command: str) -> bool:
        return bool(re.search(
            r"(?:^|[\\/\s\"])(?:chatgpt|codex)\.exe(?=$|[\\/\s\"])",
            str(command or ""),
            re.IGNORECASE,
        ))

    @staticmethod
    def _process_rows() -> list[tuple[int, int, str]]:
        # Win32_Process exposes the exact app-server command line, including
        # the serialized ``env`` map. PowerShell is present on supported
        # Windows installations and avoids adding a Python process-inspection
        # dependency to the packaged Connector.
        script = (
            "$ErrorActionPreference='SilentlyContinue'; "
            "Get-CimInstance Win32_Process | ForEach-Object { "
            "$command = ($_.CommandLine -replace '[\\r\\n]', ' '); "
            '"{0}`t{1}`t{2}" -f $_.ProcessId, $_.ParentProcessId, $command }'
        )
        output = CodexDesktopBridge._powershell(script)
        rows: list[tuple[int, int, str]] = []
        for line in output.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            try:
                rows.append((int(parts[0].strip()), int(parts[1].strip()), parts[2]))
            except ValueError:
                continue
        return rows

    @staticmethod
    def _powershell(script: str) -> str:
        executable = next(
            (shutil.which(name) for name in (
                "powershell.exe", "pwsh.exe", "powershell", "pwsh")
             if shutil.which(name)),
            None,
        )
        if not executable:
            return ""
        options: dict[str, Any] = {}
        creation_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if creation_flag:
            options["creationflags"] = creation_flag
        try:
            completed = subprocess.run(
                [executable, "-NoLogo", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-Command", script],
                check=False, capture_output=True, text=True, timeout=3,
                encoding="utf-8", errors="replace", **options)
        except (OSError, subprocess.SubprocessError):
            return ""
        return str(completed.stdout or "").strip()

    @staticmethod
    def _process_command(pid: int) -> str:
        try:
            process_id = int(pid)
        except (TypeError, ValueError):
            return ""
        script = (
            "$p=Get-CimInstance Win32_Process -Filter 'ProcessId = "
            + str(process_id)
            + "'; if($p){$p.CommandLine}"
        )
        return CodexDesktopBridge._powershell(script)

    @staticmethod
    def _process_environment_command(pid: int) -> str:
        # Windows does not expose a ``ps eww`` equivalent. The app-server
        # command line already contains the serialized environment map.
        return CodexDesktopBridge._process_command(pid)

    def _configured_node_path(self) -> Path | None:
        for name in (EXPLICIT_NODE_ENV, *NODE_ENV_NAMES):
            value = str(os.environ.get(name) or "").strip().strip('"')
            if value:
                return Path(value).expanduser()
        return None

    @staticmethod
    def _usable_node(path: Path | None) -> bool:
        if path is None:
            return False
        try:
            return path.is_file() and (
                path.suffix.lower() == ".exe" or os.access(path, os.X_OK))
        except OSError:
            return False

    def _node_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        seen: set[str] = set()

        def add(value: str | Path | None) -> None:
            if value is None or not str(value).strip():
                return
            path = Path(value).expanduser()
            key = str(path).lower()
            if key not in seen:
                candidates.append(path)
                seen.add(key)

        add(self.node_path)
        add(self._discovered_node_path)
        add(self._configured_node_path())

        resources = os.environ.get("CODEX_ELECTRON_RESOURCES_PATH")
        if resources:
            add(Path(resources) / "cua_node" / "bin" / "node.exe")
        cli_path = os.environ.get("CODEX_CLI_PATH")
        if cli_path:
            add(Path(cli_path).expanduser().parent / "cua_node" / "bin" / "node.exe")

        user_profile = os.environ.get("USERPROFILE")
        if user_profile:
            add(Path(user_profile) / ".cache" / "codex-runtimes"
                / "codex-primary-runtime" / "dependencies" / "node" / "bin"
                / "node.exe")
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            runtimes = Path(local_app_data) / "OpenAI" / "Codex" / "runtimes" / "cua_node"
            try:
                for version in sorted(runtimes.glob("*"), reverse=True):
                    add(version / "bin" / "node.exe")
            except OSError:
                pass
        add(shutil.which("node.exe") or shutil.which("node"))
        return candidates

    def _resolve_node_path(self) -> Path | None:
        for candidate in self._node_candidates():
            if self._usable_node(candidate):
                return candidate
        return None

    async def _run_node(self, pipe: Path, request: dict[str, Any]) -> dict[str, Any]:
        from .adapters.base import subprocess_options

        if self.node_path is None or not self._usable_node(self.node_path):
            raise CodexDesktopBridgeUnavailable(
                "ChatGPT Desktop bridge runtime 不可用")
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
