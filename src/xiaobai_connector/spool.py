"""Durable, secret-free SQLite event and command spool."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Spool:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS commands(
                  event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, event_type TEXT NOT NULL,
                  payload_json TEXT NOT NULL, state TEXT NOT NULL,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS command_run_start
                  ON commands(run_id) WHERE event_type='run.start';
                CREATE TABLE IF NOT EXISTS events(
                  event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, event_type TEXT NOT NULL,
                  payload_json TEXT NOT NULL, state TEXT NOT NULL,
                  created_at TEXT NOT NULL, last_sent_at TEXT, acknowledged_at TEXT);
                CREATE INDEX IF NOT EXISTS event_pending ON events(state,created_at);
            """)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @contextmanager
    def _db(self):
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def persist_command(self, event_id: str, event_type: str, payload: dict[str, Any]) -> bool:
        now = _now()
        run_id = str(payload.get("run_id") or "")
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        with self._lock, self._db() as db:
            existing = db.execute("SELECT event_id,payload_json FROM commands WHERE event_id=?",
                                  (event_id,)).fetchone()
            if existing is None and event_type == "run.start":
                existing = db.execute(
                    "SELECT event_id,payload_json FROM commands WHERE run_id=? AND event_type='run.start'",
                    (run_id,)).fetchone()
            if existing:
                if existing["payload_json"] != raw:
                    raise ValueError("duplicate command 内容冲突")
                return False
            db.execute("INSERT INTO commands VALUES (?,?,?,?,?,?,?)",
                       (event_id, run_id, event_type, raw, "persisted", now, now))
            return True

    def set_command_state(self, event_id: str, state: str) -> None:
        with self._lock, self._db() as db:
            db.execute("UPDATE commands SET state=?,updated_at=? WHERE event_id=?",
                       (state, _now(), event_id))

    def command_state(self, event_id: str) -> str | None:
        with self._lock, self._db() as db:
            row = db.execute("SELECT state FROM commands WHERE event_id=?", (event_id,)).fetchone()
            return str(row["state"]) if row else None

    def unfinished_starts(self) -> list[dict[str, Any]]:
        with self._lock, self._db() as db:
            rows = db.execute(
                "SELECT * FROM commands WHERE event_type='run.start' AND state IN ('persisted','running')"
            ).fetchall()
            return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def enqueue_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> str:
        event_id = "evt_" + uuid.uuid4().hex
        value = {"run_id": run_id, **payload}
        with self._lock, self._db() as db:
            db.execute("INSERT INTO events VALUES (?,?,?,?, 'pending', ?,NULL,NULL)",
                       (event_id, run_id, event_type,
                        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True), _now()))
        return event_id

    def pending_events(self, limit: int = 64) -> list[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        with self._lock, self._db() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE state='pending' OR (state='sent' AND last_sent_at<=?) "
                "ORDER BY rowid LIMIT ?", (cutoff, limit)).fetchall()
            return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def mark_sent(self, event_id: str) -> None:
        with self._lock, self._db() as db:
            db.execute("UPDATE events SET state='sent',last_sent_at=? WHERE event_id=?",
                       (_now(), event_id))

    def acknowledge(self, event_id: str, accepted: bool) -> None:
        with self._lock, self._db() as db:
            db.execute("UPDATE events SET state=?,acknowledged_at=? WHERE event_id=?",
                       ("acknowledged" if accepted else "failed", _now(), event_id))

    def event_count(self, run_id: str, event_type: str) -> int:
        with self._lock, self._db() as db:
            return int(db.execute("SELECT count(*) FROM events WHERE run_id=? AND event_type=?",
                                  (run_id, event_type)).fetchone()[0])

    def output_sequence(self, run_id: str) -> int:
        with self._lock, self._db() as db:
            rows = db.execute("SELECT payload_json FROM events WHERE run_id=? AND event_type='run.output.delta'",
                              (run_id,)).fetchall()
        return max((int(json.loads(row["payload_json"]).get("seq") or 0) for row in rows), default=0)
