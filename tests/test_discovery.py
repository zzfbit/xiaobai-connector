import unittest
from unittest.mock import patch

from xiaobai_connector.discovery import scan_agents


class DiscoveryTests(unittest.TestCase):
    def test_known_agent_rows_are_returned(self):
        with patch("xiaobai_connector.discovery._which", side_effect=lambda names: "/tmp/" + names[0]):
            with patch("xiaobai_connector.discovery._version", return_value="test 1"):
                result = scan_agents()
        self.assertEqual([item.kind for item in result], ["codex", "claude", "hermes"])
        self.assertTrue(all(item.found for item in result))


if __name__ == "__main__":
    unittest.main()
