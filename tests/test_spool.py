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

    def test_recovery_payload_recovers_hermes_durable_session_id(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(Path(directory) / "spool.sqlite3")
            payload = {
                "run_id": "run_hermes_recovery",
                "agent_id": "agent_1",
                "local_ref": "hermes:bot:default",
                "adapter": "hermes",
                "input": {"text": "继续"},
            }
            spool.persist_command("evt_hermes_recovery", "run.start", payload)
            spool.enqueue_event("run_hermes_recovery", "run.started", {
                "adapter_session_id": "20260907_123456_abcdef",
            })

            recovered = spool.recovery_payload("run_hermes_recovery")

        self.assertEqual(
            recovered.get("hermes_session_id"), "20260907_123456_abcdef")


if __name__ == "__main__":
    unittest.main()
