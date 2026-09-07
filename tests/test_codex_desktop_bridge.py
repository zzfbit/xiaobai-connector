import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from xiaobai_connector.adapters.codex import CodexAdapter
from xiaobai_connector.codex_desktop_bridge import (
    CodexDesktopBridge,
    CodexDesktopBridgeError,
    CodexDesktopBridgeUnavailable,
)
from xiaobai_connector.codex_sessions import CHATGPT_CODEX_BINARY


class CodexDesktopBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_send_message_call_for_desktop_writer(self):
        with tempfile.TemporaryDirectory() as root:
            node = Path(root) / "node"
            node.write_text("placeholder", encoding="utf-8")
            node.chmod(0o755)
            captured = {}

            async def runner(pipe, request):
                captured["pipe"] = pipe
                captured["request"] = request
                return {"ok": True, "sent": True,
                        "response": {"jsonrpc": "2.0", "id": 1,
                                     "result": {"content": []}}}

            bridge = CodexDesktopBridge(
                binary=str(CHATGPT_CODEX_BINARY), node_path=node,
                node_runner=runner)
            with patch.dict(os.environ, {"XIAOBAI_CODEX_DESKTOP_BRIDGE": "1"}), \
                 patch.object(bridge, "_find_pipe", return_value=Path(root) / "app.sock"):
                result = await bridge.send_message(
                    thread_id="desktop-thread", text="停止，不用上传了",
                    client_message_id="a" * 32)

        self.assertEqual(result, {"content": []})
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
            node = Path(root) / "node"
            node.write_text("placeholder", encoding="utf-8")
            node.chmod(0o755)

            async def runner(pipe, request):
                return {"ok": False, "sent": True, "error": "desktop rejected"}

            bridge = CodexDesktopBridge(
                binary=str(CHATGPT_CODEX_BINARY), node_path=node,
                node_runner=runner)
            with patch.dict(os.environ, {"XIAOBAI_CODEX_DESKTOP_BRIDGE": "1"}), \
                 patch.object(bridge, "_find_pipe", return_value=Path(root) / "app.sock"):
                with self.assertRaises(CodexDesktopBridgeError):
                    await bridge.send_message(
                        thread_id="desktop-thread", text="插话",
                        client_message_id="b" * 32)

    async def test_closed_desktop_pipe_is_recoverable(self):
        with tempfile.TemporaryDirectory() as root:
            node = Path(root) / "node"
            node.write_text("placeholder", encoding="utf-8")
            node.chmod(0o755)

            async def runner(pipe, request):
                return {"ok": False, "sent": True,
                        "error": "app-tools pipe closed"}

            bridge = CodexDesktopBridge(
                binary=str(CHATGPT_CODEX_BINARY), node_path=node,
                node_runner=runner)
            with patch.dict(os.environ, {"XIAOBAI_CODEX_DESKTOP_BRIDGE": "1"}), \
                 patch.object(bridge, "_find_pipe", return_value=Path(root) / "app.sock"):
                with self.assertRaises(CodexDesktopBridgeUnavailable):
                    await bridge.send_message(
                        thread_id="desktop-thread", text="插话",
                        client_message_id="pipe" * 8)

    async def test_adapter_uses_bridge_before_legacy_queue(self):
        bridge = AsyncMock()
        bridge.send_message.return_value = {"content": []}
        with patch("xiaobai_connector.adapters.codex.CodexDesktopBridge",
                   return_value=bridge):
            adapter = CodexAdapter({"binary": sys.executable})
            self.assertTrue(await adapter.queue_thread_message(
                thread_id="desktop-thread", text="当前任务先停止上传",
                attachments=[], client_message_id="c" * 32))

        bridge.send_message.assert_awaited_once_with(
            thread_id="desktop-thread", text="当前任务先停止上传",
            client_message_id="c" * 32)

    async def test_adapter_falls_back_only_when_bridge_is_unavailable(self):
        bridge = AsyncMock()
        bridge.send_message.side_effect = CodexDesktopBridgeUnavailable("pipe gone")
        with patch("xiaobai_connector.adapters.codex.CodexDesktopBridge",
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


if __name__ == "__main__":
    unittest.main()
