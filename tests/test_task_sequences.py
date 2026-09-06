import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from xiaobai_connector.config import ConnectorConfig
from xiaobai_connector.gateway import GatewayClient
from xiaobai_connector.adapters.router import AdapterRouter
from xiaobai_connector.spool import Spool


def _manifest(sequence_id: str = "seq_test", item_count: int = 2) -> dict[str, object]:
    return {
        "sequence_id": sequence_id,
        "agent_id": "agt_test",
        "local_ref": "codex:test",
        "adapter": "codex",
        "item_count": item_count,
    }


def _item(manifest: dict[str, object], position: int, run_id: str) -> dict[str, object]:
    return {
        **manifest,
        "sequence_item_id": f"item_{position}",
        "position": position,
        "run_id": run_id,
        "input": {"message_id": f"message_{position}", "text": f"任务 {position}"},
        "codex_session": {"cwd": "/tmp/project"},
    }


class TaskSequenceSpoolTests(unittest.TestCase):
    def test_items_run_in_order_with_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(Path(directory) / "spool.sqlite3")
            manifest = _manifest()
            first = _item(manifest, 0, "run_1")
            second = _item(manifest, 1, "run_2")

            self.assertTrue(spool.persist_task_sequence("seq_test", manifest))
            self.assertTrue(spool.persist_task_sequence_item("seq_test", first))
            self.assertTrue(spool.persist_task_sequence_item("seq_test", second))
            self.assertTrue(spool.start_task_sequence("seq_test"))

            claimed = spool.claim_due_task_sequence_items()
            self.assertEqual([row["run_id"] for row in claimed], ["run_1"])
            self.assertEqual(spool.command_state(claimed[0]["event_id"]), "persisted")
            self.assertEqual(spool.claim_due_task_sequence_items(), [])

            spool.finish_task_sequence_item("seq_test", "item_0", "run_1", "completed")
            with spool._db() as db:
                next_due = db.execute(
                    "SELECT next_due_at FROM task_sequences WHERE sequence_id=?",
                    ("seq_test",),
                ).fetchone()[0]
            self.assertEqual(
                [row["run_id"] for row in spool.claim_due_task_sequence_items(next_due)],
                ["run_2"],
            )

    def test_failed_item_cancels_the_remaining_items(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(Path(directory) / "spool.sqlite3")
            manifest = _manifest()
            spool.persist_task_sequence("seq_test", manifest)
            spool.persist_task_sequence_item("seq_test", _item(manifest, 0, "run_1"))
            spool.persist_task_sequence_item("seq_test", _item(manifest, 1, "run_2"))
            spool.start_task_sequence("seq_test")
            spool.claim_due_task_sequence_items()
            spool.finish_task_sequence_item("seq_test", "item_0", "run_1", "failed", "失败")

            with spool._db() as db:
                sequence = db.execute(
                    "SELECT state FROM task_sequences WHERE sequence_id=?", ("seq_test",)
                ).fetchone()[0]
                remaining = db.execute(
                    "SELECT state FROM task_sequence_items WHERE item_id=?", ("item_1",)
                ).fetchone()[0]
            self.assertEqual(sequence, "failed")
            self.assertEqual(remaining, "canceled")


class TaskSequenceGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_persists_and_cancels_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            router = AdapterRouter([{
                "adapter": "codex", "local_ref": "codex:test", "enabled": True,
                "binary": "codex",
            }])
            gateway = GatewayClient(
                ConnectorConfig(server_url="ws://127.0.0.1:1/agent/connect",
                                device_id="dev_sequence", device_name="测试 Mac"),
                "connector-token", router, spool_path=Path(directory) / "spool.sqlite3",
            )
            gateway.registered_agents = [{
                "agent_id": "agt_test", "local_ref": "codex:test",
                "adapter": "codex", "enabled": True,
            }]
            manifest = _manifest(item_count=1)
            item = _item(manifest, 0, "run_test")

            self.assertTrue(await gateway._dispatch(
                SimpleNamespace(type="codex.sequence.create", payload=manifest)))
            self.assertTrue(await gateway._dispatch(
                SimpleNamespace(type="codex.sequence.item", payload=item)))
            self.assertTrue(await gateway._dispatch(
                SimpleNamespace(type="codex.sequence.start", payload=manifest)))
            self.assertEqual(
                gateway.spool.claim_due_task_sequence_items()[0]["run_id"], "run_test")
            self.assertTrue(await gateway._dispatch(SimpleNamespace(
                type="codex.sequence.cancel",
                payload={**manifest, "active_run_id": "run_test"},
            )))


if __name__ == "__main__":
    unittest.main()
