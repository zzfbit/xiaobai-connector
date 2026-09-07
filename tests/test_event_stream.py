import asyncio
import unittest

from xiaobai_connector.event_stream import ConnectorEventStream, normalize_progress


class EventStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_output_is_coalesced_and_boundary_is_preserved(self):
        events = []

        async def emit(event_type, payload):
            events.append((event_type, payload))

        stream = ConnectorEventStream(emit)
        await stream("run.output.delta", {
            "seq": 10, "delta": "hello", "message_start": True,
        })
        await stream("run.output.delta", {
            "seq": 11, "delta": " world", "message_end": True,
        })

        self.assertEqual(events, [("run.output.delta", {
            "seq": 1, "delta": "hello world",
            "message_start": True, "message_end": True,
        })])

    async def test_large_output_is_bounded_and_lifecycle_flushes_tail(self):
        events = []

        async def emit(event_type, payload):
            events.append((event_type, payload))

        stream = ConnectorEventStream(emit, output_seq_start=4)
        await stream("run.output.delta", {"delta": "x" * 1_300})
        await stream("run.completed", {"detail": "done", "secret": "drop"})

        output = [payload for event, payload in events if event == "run.output.delta"]
        self.assertEqual([item["seq"] for item in output], [5, 6, 7])
        self.assertEqual([len(item["delta"]) for item in output], [512, 512, 276])
        self.assertEqual(events[-1], ("run.completed", {"detail": "done"}))


class ProgressNormalizationTests(unittest.TestCase):
    def test_unknown_and_unhelpful_progress_fields_are_removed(self):
        self.assertEqual(normalize_progress({
            "progress": {
                "phase": "thinking", "message": "正在处理\n下一行",
                "elapsed_seconds": "12", "unknown": "secret",
            },
        }), {
            "phase": "thinking", "message": "正在处理 下一行",
            "elapsed_seconds": 12,
        })


if __name__ == "__main__":
    unittest.main()
