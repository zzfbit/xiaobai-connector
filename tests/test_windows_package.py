import os
import unittest
from pathlib import Path
from unittest.mock import patch

from xiaobai_connector_windows import __version__
from xiaobai_connector_windows.adapters.base import (
    executable_available,
    executable_command,
)
from xiaobai_connector_windows.config import ConnectorConfig, current_platform
from xiaobai_connector_windows.paths import data_dir


class WindowsPackageTests(unittest.TestCase):
    def test_package_reports_windows_without_host_platform_detection(self):
        self.assertTrue(__version__)
        self.assertEqual(current_platform(), "windows")

    def test_data_dir_uses_local_app_data(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\test\AppData\Local",
                                     "APPDATA": r"C:\Users\test\AppData\Roaming"},
                        clear=False):
            self.assertEqual(
                data_dir(),
                Path(r"C:\Users\test\AppData\Local") / "Xiaobai Connector",
            )

    def test_config_defaults_to_windows_platform(self):
        self.assertEqual(ConnectorConfig().platform, "windows")

    def test_command_files_are_launched_through_cmd(self):
        with patch.dict(os.environ, {"COMSPEC": "cmd.exe"}, clear=False):
            self.assertEqual(
                executable_command(r"C:\Program Files\Claude\claude.cmd", "--print"),
                ["cmd.exe", "/d", "/s", "/c",
                 r"C:\Program Files\Claude\claude.cmd", "--print"],
            )

    def test_command_files_are_usable_without_execute_bit(self):
        with patch("xiaobai_connector_windows.adapters.base.Path.is_file", return_value=True):
            self.assertTrue(executable_available(r"C:\Tools\codex.cmd"))


if __name__ == "__main__":
    unittest.main()
