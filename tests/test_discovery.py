import unittest
import sys
from pathlib import Path
from unittest.mock import patch

from xiaobai_connector.discovery import scan_agents


class DiscoveryTests(unittest.TestCase):
    def test_known_agent_rows_are_returned(self):
        with patch("xiaobai_connector.discovery._which", side_effect=lambda names: "/tmp/" + names[0]):
            with patch("xiaobai_connector.discovery._version", return_value="test 1"):
                result = scan_agents()
        self.assertEqual([item.kind for item in result], ["codex", "claude", "hermes"])
        self.assertTrue(all(item.found for item in result))

    def test_configured_binary_path_is_used(self):
        with patch("xiaobai_connector.discovery._which", return_value=None):
            with patch("xiaobai_connector.discovery._codex_binary", return_value=None):
                result = scan_agents([{
                    "adapter": "claude",
                    "binary": sys.executable,
                }])
        claude = next(item for item in result if item.kind == "claude")
        self.assertEqual(claude.executable, str(Path(sys.executable).resolve()))
        self.assertEqual(claude.status, "online")
        self.assertEqual(claude.detail, "已使用手动指定路径")

    def test_invalid_configured_binary_is_reported(self):
        with patch("xiaobai_connector.discovery._which", return_value=None):
            with patch("xiaobai_connector.discovery._codex_binary", return_value=None):
                result = scan_agents([{
                    "adapter": "hermes",
                    "binary": "/path/that/does/not/exist/hermes",
                }])
        hermes = next(item for item in result if item.kind == "hermes")
        self.assertFalse(hermes.can_connect)
        self.assertIn("手动指定路径不可用", hermes.detail)


if __name__ == "__main__":
    unittest.main()
