import tempfile
import unittest
from pathlib import Path

from xiaobai_connector.spool import Spool


class SpoolTests(unittest.TestCase):
    def test_event_is_replayed_until_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(Path(directory) / "spool.sqlite3")
            event_id = spool.enqueue_event("run_1", "run.output.delta", {"seq": 1, "delta": "hi"})
            self.assertEqual(spool.pending_events()[0]["event_id"], event_id)
            spool.mark_sent(event_id)
            self.assertEqual(spool.pending_events(), [])
            spool.acknowledge(event_id, True)
            self.assertEqual(spool.pending_events(), [])

    def test_duplicate_start_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(Path(directory) / "spool.sqlite3")
            payload = {"run_id": "run_1", "agent_id": "agent_1"}
            self.assertTrue(spool.persist_command("evt_12345678", "run.start", payload))
            self.assertFalse(spool.persist_command("evt_other", "run.start", payload))


if __name__ == "__main__":
    unittest.main()
