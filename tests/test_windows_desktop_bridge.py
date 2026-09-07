import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from xiaobai_connector_windows.adapters.codex import CodexAdapter
from xiaobai_connector_windows.codex_desktop_bridge import (
    CodexDesktopBridge,
    CodexDesktopBridgeError,
    CodexDesktopBridgeUnavailable,
    _command_value,
    _normalize_pipe_path,
)


class WindowsCodexDesktopBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_send_message_call_for_windows_desktop_writer(self):
        with tempfile.TemporaryDirectory() as root:
            node = Path(root) / "node.exe"
            node.write_text("placeholder", encoding="utf-8")
            captured = {}

            async def runner(pipe, request):
                captured["pipe"] = pipe
                captured["request"] = request
                return {"ok": True, "sent": True,
                        "response": {"jsonrpc": "2.0", "id": 1,
                                     "result": {"contentItems": [], "success": True}}}

            bridge = CodexDesktopBridge(
                binary=r"C:\Users\test\AppData\Local\OpenAI\Codex\bin\codex.CMD",
                node_path=node, node_runner=runner)
            with patch.dict(os.environ, {"XIAOBAI_CODEX_DESKTOP_BRIDGE": "1"}), \
                 patch.object(bridge, "_find_pipe",
                              return_value=Path(r"\\.\pipe\codex-app-tools-test")):
                result = await bridge.send_message(
                    thread_id="desktop-thread", text="停止，不用上传了",
                    client_message_id="a" * 32)

        self.assertEqual(result, {"contentItems": [], "success": True})
        self.assertEqual(str(captured["pipe"]), r"\\.\pipe\codex-app-tools-test")
        request = captured["request"]
        self.assertEqual(request["method"], "tools/call")
        self.assertEqual(request["params"]["tool"], "send_message_to_thread")
        self.assertEqual(request["params"]["threadId"], "desktop-thread")
        self.assertEqual(request["params"]["arguments"], {
            "threadId": "desktop-thread", "prompt": "停止，不用上传了",
        })
        self.assertEqual(request["params"]["callId"], "a" * 32)

    async def test_reached_bridge_error_does_not_look_like_unavailable(self):
        with tempfile.TemporaryDirectory() as root:
            node = Path(root) / "node.exe"
            node.write_text("placeholder", encoding="utf-8")

            async def runner(pipe, request):
                return {"ok": False, "sent": True, "error": "desktop rejected"}

            bridge = CodexDesktopBridge(
                binary=r"C:\Tools\codex.exe", node_path=node,
                node_runner=runner)
            with patch.dict(os.environ, {"XIAOBAI_CODEX_DESKTOP_BRIDGE": "1"}), \
                 patch.object(bridge, "_find_pipe",
                              return_value=Path(r"\\.\pipe\codex-app-tools-test")):
                with self.assertRaises(CodexDesktopBridgeError):
                    await bridge.send_message(
                        thread_id="desktop-thread", text="插话",
                        client_message_id="b" * 32)

    async def test_adapter_uses_windows_bridge_before_legacy_queue(self):
        bridge = AsyncMock()
        bridge.send_message.return_value = {"contentItems": [], "success": True}
        with patch("xiaobai_connector_windows.adapters.codex.CodexDesktopBridge",
                   return_value=bridge):
            adapter = CodexAdapter({"binary": sys.executable})
            self.assertTrue(await adapter.queue_thread_message(
                thread_id="desktop-thread", text="当前任务先停止上传",
                attachments=[], client_message_id="c" * 32))

        bridge.send_message.assert_awaited_once_with(
            thread_id="desktop-thread", text="当前任务先停止上传",
            client_message_id="c" * 32)

    async def test_adapter_falls_back_only_when_windows_bridge_is_unavailable(self):
        bridge = AsyncMock()
        bridge.send_message.side_effect = CodexDesktopBridgeUnavailable("pipe gone")
        with patch("xiaobai_connector_windows.adapters.codex.CodexDesktopBridge",
                   return_value=bridge):
            adapter = CodexAdapter({"binary": sys.executable})
            fallback = AsyncMock()
            with patch.object(adapter, "_queue_once", fallback):
                self.assertTrue(await adapter.queue_thread_message(
                    thread_id="desktop-thread", text="回退到持久队列",
                    attachments=[], client_message_id="d" * 32))

        fallback.assert_awaited_once_with(
            thread_id="desktop-thread", text="回退到持久队列",
            client_message_id="d" * 32)

    def test_decodes_windows_app_server_environment_from_command_line(self):
        command = (
            r'codex.exe app-server -c "env={\"CODEX_APP_TOOLS_PIPE_PATH\"='
            r'"\\\\.\\pipe\\codex-browser-use-test\",'
            r'\"CODEX_MCP_NODE_PATH\"=\"C:\\Users\\test\\node.exe\"}"'
        )
        self.assertEqual(
            _command_value(command, "CODEX_APP_TOOLS_PIPE_PATH"),
            r"\\.\pipe\codex-browser-use-test",
        )
        self.assertEqual(
            _normalize_pipe_path(
                _command_value(command, "CODEX_APP_TOOLS_PIPE_PATH")),
            r"\\.\pipe\codex-browser-use-test",
        )
        self.assertEqual(
            _command_value(command, "CODEX_MCP_NODE_PATH"),
            r"C:\Users\test\node.exe",
        )

    def test_rejects_non_codex_binary_from_desktop_bridge(self):
        bridge = CodexDesktopBridge(binary=r"C:\Tools\python.exe")
        self.assertFalse(bridge.enabled())
        self.assertIsNone(bridge._find_pipe())


if __name__ == "__main__":
    unittest.main()
