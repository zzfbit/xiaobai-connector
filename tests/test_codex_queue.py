import asyncio
import json
import os
import sys
import unittest
from unittest.mock import patch

from xiaobai_connector.adapters.codex import (
    CodexAdapter,
    CodexQueueError,
    resolve_codex_binary,
)


class _FakeWriter:
    def __init__(self):
        self.messages = []

    def write(self, value):
        self.messages.append(json.loads(value.decode()))

    async def drain(self):
        return None


class _FakeReader:
    def __init__(self, values):
        self.values = list(values)

    async def readline(self):
        return self.values.pop(0) if self.values else b""


class _FakeProcess:
    def __init__(self):
        self.stdin = _FakeWriter()
        self.stdout = _FakeReader([
            b'{"id":1,"result":{}}\n',
            b'{"id":2,"result":{}}\n',
        ])
        self.returncode = None

    def terminate(self):
        self.returncode = 0

    async def wait(self):
        return self.returncode


class CodexQueueTests(unittest.TestCase):
    def test_environment_override_wins(self):
        with patch.dict(os.environ, {"XIAOBAI_CODEX_BIN": "/custom/codex"}):
            self.assertEqual(resolve_codex_binary("/configured/codex"), "/custom/codex")

    def test_queue_uses_thread_queue_add(self):
        process = _FakeProcess()

        async def fake_create(*_args, **_kwargs):
            return process

        with patch.dict(os.environ, {"XIAOBAI_CODEX_BIN": sys.executable}):
            adapter = CodexAdapter({"local_ref": "codex:default", "enabled": True})
            with patch("xiaobai_connector.adapters.codex.asyncio.create_subprocess_exec",
                       new=fake_create):
                result = asyncio.run(adapter.queue_thread_message(
                    thread_id="thread_1", text="继续处理",
                    client_message_id="a" * 32))

        self.assertTrue(result)
        methods = [message.get("method") for message in process.stdin.messages]
        self.assertEqual(methods, ["initialize", "initialized", "thread/queue/add"])
        queue_request = process.stdin.messages[-1]
        self.assertEqual(queue_request["params"]["threadId"], "thread_1")
        self.assertEqual(queue_request["params"]["clientUserMessageId"], "a" * 32)
        self.assertEqual(queue_request["params"]["input"][0]["text"], "继续处理")

    def test_queue_process_failure_is_a_retryable_connector_error(self):
        async def fake_create(*_args, **_kwargs):
            raise OSError("Codex 正在重启")

        with patch.dict(os.environ, {"XIAOBAI_CODEX_BIN": sys.executable}):
            adapter = CodexAdapter({"local_ref": "codex:default", "enabled": True})
            with patch("xiaobai_connector.adapters.codex.asyncio.create_subprocess_exec",
                       new=fake_create):
                with self.assertRaises(CodexQueueError):
                    asyncio.run(adapter.queue_thread_message(
                        thread_id="thread_1", text="继续处理",
                        client_message_id="b" * 32))


if __name__ == "__main__":
    unittest.main()
