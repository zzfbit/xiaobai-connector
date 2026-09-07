import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xiaobai_connector_windows.codex_sessions import (
    CodexSessionClient,
    desktop_codex_binary,
    resolve_codex_binary,
)


class _FakeProcess:
    def __init__(self):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(
            '{"id":1,"result":{}}\n'
            '{"id":2,"result":{"data":[]}}\n'
        )
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class WindowsCodexSessionTests(unittest.TestCase):
    def test_prefers_desktop_binary_over_saved_npm_shim(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = (Path(directory) / "OpenAI" / "Codex" / "bin"
                      / "8e5b6932251c2c1c" / "codex.exe")
            binary.parent.mkdir(parents=True)
            binary.write_text("placeholder", encoding="utf-8")
            with patch.dict(os.environ, {
                "LOCALAPPDATA": directory,
                "XIAOBAI_CODEX_BIN": "",
            }, clear=False):
                expected = str(binary.resolve())
                self.assertEqual(desktop_codex_binary(), expected)
                self.assertEqual(
                    resolve_codex_binary(
                        r"C:\Users\test\AppData\Roaming\npm\codex.cmd"),
                    expected,
                )

    def test_explicit_environment_binary_still_wins(self):
        with patch.dict(os.environ, {
            "XIAOBAI_CODEX_BIN": r"C:\Tools\codex.cmd",
        }, clear=False):
            self.assertEqual(
                resolve_codex_binary(r"C:\Users\test\codex.cmd"),
                r"C:\Tools\codex.cmd",
            )

    def test_app_server_stdout_is_decoded_as_utf8(self):
        process = _FakeProcess()
        client = CodexSessionClient(binary=sys.executable)
        with patch(
            "xiaobai_connector_windows.codex_sessions.subprocess.Popen",
            return_value=process,
        ) as popen:
            result = client._request("thread/list", {})

        self.assertEqual(result, {"data": []})
        self.assertEqual(popen.call_args.kwargs["encoding"], "utf-8")
        self.assertTrue(popen.call_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
