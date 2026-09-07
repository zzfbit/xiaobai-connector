import json
import unittest
from unittest.mock import patch

from xiaobai_connector import __version__
from xiaobai_connector.pairing import PairingClient, PairingError, PairingRequest, http_base


class _Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit=-1):
        return json.dumps(self.value).encode()


class _RequestCapture:
    def __init__(self, response):
        self.response = response
        self.request = None

    def __call__(self, request, **_kwargs):
        self.request = request
        return self.response


class PairingTests(unittest.TestCase):
    def test_http_base(self):
        self.assertEqual(http_base("wss://example.test/agent/connect"), "https://example.test")

    def test_start_validates_response(self):
        capture = _RequestCapture(_Response({
            "ok": True, "pairing_id": "pair_12345678", "short_code": "123456",
            "proof_secret": "proof", "expires_at": "2030-01-01T00:00:00Z",
        }))
        with patch("urllib.request.urlopen", side_effect=capture):
            result = PairingClient("wss://example.test/agent/connect").start(
                device_name="Mac", platform="macos", connector_version="0.1.0")
        self.assertIsInstance(result, PairingRequest)
        self.assertEqual(result.short_code, "123456")
        self.assertEqual(capture.request.get_header("User-agent"),
                         f"Xiaobai-Connector/{__version__}")

    def test_invalid_server_is_rejected(self):
        with self.assertRaises(PairingError):
            PairingClient("https://example.test")


if __name__ == "__main__":
    unittest.main()
