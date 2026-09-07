import sys
import unittest
from unittest.mock import patch

from xiaobai_connector.adapters.codex import CodexAdapter
from xiaobai_connector.codex_sessions import CodexSessionClient
from xiaobai_connector.adapters.hermes import HermesAdapter


class AgentIdentityTests(unittest.TestCase):
    def test_codex_publishes_mobile_avatar_and_sessions_extension(self):
        agent = CodexAdapter({
            "binary": sys.executable,
            "enabled": True,
        }).discover()[0]

        self.assertEqual(agent.avatar["kind"], "bundled_asset")
        self.assertEqual(agent.avatar["asset"], "AgentCodexAvatar")
        extension_ids = [item["id"] for item in agent.presentation["extensions"]]
        self.assertIn("codex_sessions_v1", extension_ids)
        self.assertIn("codex_usage_limits_v1", extension_ids)

    def test_hermes_publishes_the_bundled_desktop_avatar(self):
        agent = HermesAdapter({
            "binary": sys.executable,
            "enabled": True,
        }).discover()[0]

        self.assertEqual(agent.avatar["kind"], "data_url")
        self.assertTrue(agent.avatar["data_url"].startswith("data:image/png;base64,"))
        self.assertEqual(agent.avatar["shape"], "circle")
        self.assertEqual(agent.avatar["fallback"], "🪽")
        self.assertTrue(agent.presentation["show_avatars"])

    def test_codex_status_does_not_downgrade_an_available_binary(self):
        adapter = CodexAdapter({"binary": sys.executable, "enabled": True})
        with patch.object(CodexSessionClient, "status", return_value={
                "local_ref": "codex:default", "status": "offline"}):
            self.assertEqual(adapter.status_snapshots()[0]["status"], "online")


if __name__ == "__main__":
    unittest.main()
