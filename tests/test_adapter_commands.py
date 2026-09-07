import unittest
from unittest.mock import patch

import xiaobai_connector.adapters.base as adapter_base
from xiaobai_connector.adapters.base import (
    executable_available,
    executable_command,
    subprocess_options,
)


class AdapterCommandTests(unittest.TestCase):
    def test_windows_command_file_uses_cmd(self):
        with patch("xiaobai_connector.adapters.base.platform.system", return_value="Windows"):
            with patch.dict("xiaobai_connector.adapters.base.os.environ", {"COMSPEC": "cmd.exe"}):
                self.assertEqual(
                    executable_command(r"C:\Program Files\Claude\claude.cmd", "--print"),
                    ["cmd.exe", "/d", "/s", "/c",
                     r"C:\Program Files\Claude\claude.cmd", "--print"],
                )

    def test_native_executable_is_unchanged(self):
        with patch("xiaobai_connector.adapters.base.platform.system", return_value="Windows"):
            self.assertEqual(
                executable_command(r"C:\Program Files\Codex\codex.exe", "app-server"),
                [r"C:\Program Files\Codex\codex.exe", "app-server"],
            )

    def test_windows_command_file_is_available_without_x_ok(self):
        with patch("xiaobai_connector.adapters.base.platform.system", return_value="Windows"):
            with patch("xiaobai_connector.adapters.base.Path.is_file", return_value=True):
                self.assertTrue(executable_available(r"C:\Tools\codex.cmd"))

    def test_windows_child_processes_use_hidden_console_flag(self):
        with patch("xiaobai_connector.adapters.base.platform.system", return_value="Windows"):
            with patch.object(adapter_base.subprocess, "CREATE_NO_WINDOW",
                              0x08000000, create=True):
                self.assertEqual(subprocess_options(), {"creationflags": 0x08000000})


if __name__ == "__main__":
    unittest.main()
