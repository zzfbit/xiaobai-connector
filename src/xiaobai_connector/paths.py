"""OS-specific application paths.

The Connector keeps non-secret state in an application-data directory and
never uses the voice project directory as an implicit configuration source.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path


APP_DIR_NAME = "Xiaobai Connector"


def data_dir() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    if system == "Windows":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if root:
            return Path(root) / APP_DIR_NAME
    root = os.environ.get("XDG_CONFIG_HOME")
    return (Path(root) / "xiaobai-connector" if root
            else Path.home() / ".config" / "xiaobai-connector")


def config_path() -> Path:
    return data_dir() / "config.json"


def spool_path() -> Path:
    return data_dir() / "spool.sqlite3"


def log_path() -> Path:
    return data_dir() / "connector.log"


def ensure_data_dir() -> Path:
    path = data_dir()
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path
