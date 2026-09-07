"""Windows Credential Manager storage for Connector secrets."""

from __future__ import annotations

import getpass
import platform


SERVICE = "com.xiaobaizzf.connector"
PAIRING_SERVICE = SERVICE + ".pairing"


class CredentialStore:
    def __init__(self, service: str = SERVICE):
        self.service = service

    def get(self, account: str) -> str | None:
        try:
            import keyring
            value = keyring.get_password(self.service, account)
            return str(value) if value else None
        except Exception:
            return None

    def set(self, account: str, value: str) -> None:
        if not account or not value:
            raise ValueError("credential account/value 不能为空")
        try:
            import keyring
            keyring.set_password(self.service, account, value)
        except Exception as exc:
            raise RuntimeError("写入系统凭据库失败，请确认 Keychain/Credential Manager 可用") from exc

    def delete(self, account: str) -> None:
        try:
            import keyring
            keyring.delete_password(self.service, account)
        except Exception:
            pass


def machine_account() -> str:
    return platform.node() or getpass.getuser() or "desktop"
