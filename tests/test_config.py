import json
import tempfile
import unittest
from pathlib import Path

from xiaobai_connector.config import ConnectorConfig


class ConfigTests(unittest.TestCase):
    def test_round_trip_does_not_add_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = ConnectorConfig(device_id="dev_example123", device_name="办公室")
            config.agents = [{"adapter": "codex", "local_ref": "codex:default", "enabled": True}]
            config.save(path)
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("token", value)
            self.assertEqual(ConnectorConfig.load(path).device_id, "dev_example123")

    def test_secret_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"token": "never"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                ConnectorConfig.load(path)


if __name__ == "__main__":
    unittest.main()
