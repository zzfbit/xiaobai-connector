"""HTTP client for the Connector pairing handshake."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class PairingError(RuntimeError):
    pass


class PairingPending(PairingError):
    pass


@dataclass(frozen=True)
class PairingRequest:
    pairing_id: str
    short_code: str
    proof_secret: str
    expires_at: str


def http_base(server_url: str) -> str:
    split = urllib.parse.urlsplit(server_url)
    scheme = "https" if split.scheme == "wss" else "http"
    if split.scheme not in {"ws", "wss"} or not split.netloc:
        raise PairingError("服务器地址必须是 ws:// 或 wss:// URL")
    return urllib.parse.urlunsplit((scheme, split.netloc, "", "", ""))


class PairingClient:
    def __init__(self, server_url: str, *, timeout: float = 15):
        self.base_url = http_base(server_url).rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path, data=body, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(2 * 1024 * 1024)
                value = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                value = json.loads(exc.read(64 * 1024).decode("utf-8"))
            except (OSError, ValueError, UnicodeError):
                value = {}
            raw_error = value.get("error") if isinstance(value, dict) else None
            error = raw_error if isinstance(raw_error, dict) else {}
            message = str(error.get("message") or raw_error
                          or f"服务器返回 HTTP {exc.code}")
            code = str(error.get("code") or "")
            if exc.code in {401, 404, 409} and code in {"unauthorized", "run_expired", ""}:
                raise PairingPending(message) from exc
            raise PairingError(message) from exc
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise PairingError(f"无法连接配对服务器：{exc}") from exc
        except (ValueError, UnicodeError) as exc:
            raise PairingError("配对服务器返回了无效 JSON") from exc
        if not isinstance(value, dict) or value.get("ok") is False:
            raise PairingError("配对服务器返回了无效结果")
        return value

    def start(self, *, device_name: str, platform: str,
              connector_version: str) -> PairingRequest:
        value = self._post("/api/connectors/pairings/start", {
            "device_name": device_name.strip()[:100],
            "platform": platform[:40],
            "connector_version": connector_version[:40],
        })
        pairing_id = str(value.get("pairing_id") or "")
        short_code = str(value.get("short_code") or "")
        proof_secret = str(value.get("proof_secret") or "")
        expires_at = str(value.get("expires_at") or "")
        if (not pairing_id.startswith("pair_") or len(short_code) != 6
                or not short_code.isdigit() or not proof_secret or not expires_at):
            raise PairingError("服务器没有返回完整的配对信息")
        return PairingRequest(pairing_id, short_code, proof_secret, expires_at)

    def exchange(self, pairing_id: str, proof_secret: str) -> dict[str, str]:
        try:
            value = self._post(
                f"/api/connectors/pairings/{urllib.parse.quote(pairing_id, safe='')}/exchange",
                {"proof_secret": proof_secret})
        except PairingPending:
            raise
        result = {
            "device_id": str(value.get("device_id") or ""),
            "connector_token": str(value.get("connector_token") or ""),
            "owner_user_id": str(value.get("owner_user_id") or ""),
        }
        if not result["device_id"].startswith("dev_") or not result["connector_token"]:
            raise PairingError("服务器没有返回有效的设备凭据")
        return result
