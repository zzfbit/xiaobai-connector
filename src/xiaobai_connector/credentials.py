"""OS credential storage; secrets never enter the JSON config or spool."""

from __future__ import annotations

import getpass
import platform
import shutil
import subprocess


SERVICE = "com.xiaobaizzf.connector"
PAIRING_SERVICE = SERVICE + ".pairing"


class CredentialStore:
    def __init__(self, service: str = SERVICE):
        self.service = service

    def get(self, account: str) -> str | None:
        if platform.system() == "Darwin" and shutil.which("security"):
            result = subprocess.run(
                ["security", "find-generic-password", "-s", self.service,
                 "-a", account, "-w"], capture_output=True, text=True,
                timeout=10, check=False)
            value = result.stdout.strip()
            return value if result.returncode == 0 and value else None
        try:
            import keyring
            value = keyring.get_password(self.service, account)
            return str(value) if value else None
        except Exception:
            return None

    def set(self, account: str, value: str) -> None:
        if not account or not value:
            raise ValueError("credential account/value 不能为空")
        if platform.system() == "Darwin" and shutil.which("security"):
            result = subprocess.run(
                ["security", "add-generic-password", "-U", "-s", self.service,
                 "-a", account, "-w", value], capture_output=True, text=True,
                timeout=10, check=False)
            if result.returncode != 0:
                raise RuntimeError("写入 macOS Keychain 失败")
            return
        try:
            import keyring
            keyring.set_password(self.service, account, value)
        except Exception as exc:
            raise RuntimeError("写入系统凭据库失败，请确认 Keychain/Credential Manager 可用") from exc

    def delete(self, account: str) -> None:
        if platform.system() == "Darwin" and shutil.which("security"):
            subprocess.run(
                ["security", "delete-generic-password", "-s", self.service,
                 "-a", account], capture_output=True, text=True,
                timeout=10, check=False)
            return
        try:
            import keyring
            keyring.delete_password(self.service, account)
        except Exception:
            pass


def machine_account() -> str:
    return platform.node() or getpass.getuser() or "desktop"
