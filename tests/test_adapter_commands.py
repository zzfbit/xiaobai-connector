import unittest
from unittest.mock import patch

from xiaobai_connector.adapters.base import executable_command


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


if __name__ == "__main__":
    unittest.main()
