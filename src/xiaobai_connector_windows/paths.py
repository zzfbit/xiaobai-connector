"""Windows application paths.

The Connector keeps non-secret state in an application-data directory and
never uses the voice project directory as an implicit configuration source.
"""

from __future__ import annotations

import os
from pathlib import Path


APP_DIR_NAME = "Xiaobai Connector"


def data_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    return (Path(root) / APP_DIR_NAME if root
            else Path.home() / "AppData" / "Local" / APP_DIR_NAME)


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
