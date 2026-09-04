import unittest

from xiaobai_connector.protocol import ProtocolError, decode, make


class ProtocolTests(unittest.TestCase):
    def test_round_trip(self):
        envelope = make("hello", "dev_12345678", None, {"ok": True})
        decoded = decode(envelope.dumps())
        self.assertEqual(decoded.type, "hello")
        self.assertEqual(decoded.payload, {"ok": True})

    def test_rejects_unknown_fields(self):
        envelope = make("hello", "dev_12345678", None, {})
        value = envelope.as_dict()
        value["extra"] = True
        import json
        with self.assertRaises(ProtocolError):
            decode(json.dumps(value))


if __name__ == "__main__":
    unittest.main()
