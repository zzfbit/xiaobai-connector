import json
import sys
import tempfile
import unittest
from pathlib import Path

from xiaobai_connector.codex_sessions import (
    CodexSessionClient,
    mobile_runtime_status,
)


class CodexSessionTests(unittest.TestCase):
    def test_snapshot_contains_portable_turns_and_hides_rollout_path(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            rollout = home / "sessions" / "2026" / "09" / "07" / "rollout.jsonl"
            rollout.parent.mkdir(parents=True)
            records = [
                {"type": "session_meta", "payload": {"originator": "Codex Desktop"}},
                {"type": "event_msg", "timestamp": "2026-09-07T01:00:00Z",
                 "payload": {"type": "turn_started", "turn_id": "turn_1"}},
                {"type": "response_item", "timestamp": "2026-09-07T01:00:01Z",
                 "payload": {"id": "user_1", "role": "user",
                             "content": [{"type": "input_text", "text": "做一个检查"}]}},
                {"type": "response_item", "timestamp": "2026-09-07T01:00:02Z",
                 "payload": {"id": "assistant_1", "role": "assistant",
                             "content": [{"type": "output_text", "text": "已完成"}]}},
                {"type": "event_msg", "timestamp": "2026-09-07T01:00:03Z",
                 "payload": {"type": "turn_completed", "status": "completed"}},
            ]
            rollout.write_text("\n".join(json.dumps(item) for item in records) + "\n",
                               encoding="utf-8")

            client = CodexSessionClient(binary=sys.executable, home=home)
            client._request = lambda _method, _params: {"data": [{
                "id": "thread_1", "name": None, "preview": "做一个检查",
                "createdAt": "2026-09-07T01:00:00Z",
                "updatedAt": "2026-09-07T01:00:03Z",
                "cwd": "/tmp/project", "path": str(rollout),
            }]}

            snapshot = client.snapshot()
            thread = snapshot["threads"][0]
            self.assertNotIn("path", thread)
            self.assertEqual(thread["codex_owner"], "desktop")
            self.assertEqual(thread["runtime_status"], "ready")
            self.assertEqual(thread["turns"][0]["items"][0]["type"], "userMessage")
            self.assertEqual(thread["turns"][0]["items"][1]["text"], "已完成")

    def test_rollout_paths_are_confined_to_codex_home(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            outside = home.parent / "outside-rollout.jsonl"
            outside.write_text("{}\n", encoding="utf-8")
            client = CodexSessionClient(binary=sys.executable, home=home)
            self.assertIsNone(client._safe_rollout_path(str(outside)))

    def test_mobile_terminal_state_is_ready(self):
        self.assertEqual(mobile_runtime_status("completed"), "ready")
        self.assertEqual(mobile_runtime_status("thinking"), "thinking")


if __name__ == "__main__":
    unittest.main()
