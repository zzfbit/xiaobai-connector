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


SEQUENCE_GAP_SECONDS = 60


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
                PRAGMA foreign_keys=ON;
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
                CREATE TABLE IF NOT EXISTS task_sequences(
                  sequence_id TEXT PRIMARY KEY, expected_count INTEGER NOT NULL,
                  state TEXT NOT NULL, start_requested INTEGER NOT NULL DEFAULT 0,
                  next_position INTEGER NOT NULL DEFAULT 0, next_due_at TEXT,
                  active_run_id TEXT, manifest_json TEXT NOT NULL,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  started_at TEXT, finished_at TEXT, canceled_at TEXT,
                  error_detail TEXT,
                  CHECK(state IN ('receiving','ready','running','completed','failed','canceled')),
                  CHECK(start_requested IN (0,1)));
                CREATE TABLE IF NOT EXISTS task_sequence_items(
                  item_id TEXT PRIMARY KEY, sequence_id TEXT NOT NULL
                    REFERENCES task_sequences(sequence_id) ON DELETE CASCADE,
                  position INTEGER NOT NULL, run_id TEXT NOT NULL UNIQUE,
                  payload_json TEXT NOT NULL, state TEXT NOT NULL,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  started_at TEXT, finished_at TEXT, error_detail TEXT,
                  UNIQUE(sequence_id,position),
                  CHECK(state IN ('pending','running','completed','failed','canceled')));
                CREATE INDEX IF NOT EXISTS task_sequences_due
                  ON task_sequences(state,next_due_at);
                CREATE INDEX IF NOT EXISTS task_sequence_items_order
                  ON task_sequence_items(sequence_id,position);
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

    def persist_task_sequence(self, sequence_id: str,
                              payload: dict[str, Any]) -> bool:
        """Persist a queue manifest before its individual items arrive."""
        sequence_id = str(sequence_id or "").strip()
        try:
            expected_count = int(payload.get("item_count"))
        except (TypeError, ValueError):
            raise ValueError("顺序任务缺少有效 item_count") from None
        if not sequence_id or expected_count < 1:
            raise ValueError("顺序任务缺少 sequence_id/item_count")
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        now = _now()
        with self._lock, self._db() as db:
            existing = db.execute(
                "SELECT manifest_json,expected_count FROM task_sequences WHERE sequence_id=?",
                (sequence_id,),
            ).fetchone()
            if existing is not None:
                if (existing["manifest_json"] != raw
                        or int(existing["expected_count"]) != expected_count):
                    raise ValueError("duplicate sequence 内容冲突")
                return False
            db.execute(
                """INSERT INTO task_sequences(
                     sequence_id,expected_count,state,start_requested,next_position,
                     next_due_at,active_run_id,manifest_json,created_at,updated_at)
                   VALUES (?,?, 'receiving',0,0,NULL,NULL,?,?,?)""",
                (sequence_id, expected_count, raw, now, now),
            )
            return True

    def persist_task_sequence_item(self, sequence_id: str,
                                   payload: dict[str, Any]) -> bool:
        """Persist one ordered item; item commands are independently replayable."""
        sequence_id = str(sequence_id or "").strip()
        item_id = str(payload.get("sequence_item_id") or "").strip()
        run_id = str(payload.get("run_id") or "").strip()
        try:
            position = int(payload.get("position"))
        except (TypeError, ValueError):
            raise ValueError("顺序任务 position 无效") from None
        if not sequence_id or not item_id or not run_id or position < 0:
            raise ValueError("顺序任务 item 缺少 sequence_item_id/run_id/position")
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        now = _now()
        with self._lock, self._db() as db:
            sequence = db.execute(
                "SELECT expected_count,start_requested,state FROM task_sequences WHERE sequence_id=?",
                (sequence_id,),
            ).fetchone()
            if sequence is None:
                raise ValueError("顺序任务队列尚未保存")
            if position >= int(sequence["expected_count"]):
                raise ValueError("顺序任务 position 超出范围")
            existing = db.execute(
                "SELECT payload_json FROM task_sequence_items WHERE item_id=?",
                (item_id,),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != raw:
                    raise ValueError("duplicate sequence item 内容冲突")
                return False
            db.execute(
                """INSERT INTO task_sequence_items(
                     item_id,sequence_id,position,run_id,payload_json,state,created_at,updated_at)
                   VALUES (?,?,?,?,?,'pending',?,?)""",
                (item_id, sequence_id, position, run_id, raw, now, now),
            )
            if bool(sequence["start_requested"]):
                count = db.execute(
                    "SELECT count(*) FROM task_sequence_items WHERE sequence_id=?",
                    (sequence_id,),
                ).fetchone()[0]
                if count == int(sequence["expected_count"]):
                    db.execute(
                        """UPDATE task_sequences SET state='ready',next_due_at=COALESCE(next_due_at,?),
                           updated_at=? WHERE sequence_id=? AND state='receiving'""",
                        (now, now, sequence_id),
                    )
            return True

    def start_task_sequence(self, sequence_id: str) -> bool:
        """Arm a queue; it starts immediately or after the last item arrives."""
        sequence_id = str(sequence_id or "").strip()
        if not sequence_id:
            raise ValueError("顺序任务队列 ID 为空")
        now = _now()
        with self._lock, self._db() as db:
            row = db.execute(
                "SELECT expected_count,state FROM task_sequences WHERE sequence_id=?",
                (sequence_id,),
            ).fetchone()
            if row is None:
                return False
            if str(row["state"]) in {"completed", "failed", "canceled"}:
                return True
            count = db.execute(
                "SELECT count(*) FROM task_sequence_items WHERE sequence_id=?",
                (sequence_id,),
            ).fetchone()[0]
            if count == int(row["expected_count"]):
                db.execute(
                    """UPDATE task_sequences SET state='ready',start_requested=1,
                       next_due_at=COALESCE(next_due_at,?),updated_at=?
                       WHERE sequence_id=?""",
                    (now, now, sequence_id),
                )
            else:
                db.execute(
                    """UPDATE task_sequences SET start_requested=1,updated_at=?
                       WHERE sequence_id=?""", (now, sequence_id),
                )
            return True

    def cancel_task_sequence(self, sequence_id: str) -> str:
        """Cancel a local queue; a running item is interrupted by the client."""
        sequence_id = str(sequence_id or "").strip()
        if not sequence_id:
            raise ValueError("顺序任务队列 ID 为空")
        now = _now()
        with self._lock, self._db() as db:
            row = db.execute(
                "SELECT state FROM task_sequences WHERE sequence_id=?",
                (sequence_id,),
            ).fetchone()
            if row is None:
                return "missing"
            state = str(row["state"])
            if state not in {"completed", "failed", "canceled"}:
                db.execute(
                    """UPDATE task_sequences SET state='canceled',canceled_at=?,
                       finished_at=COALESCE(finished_at,?),updated_at=? WHERE sequence_id=?""",
                    (now, now, now, sequence_id),
                )
                db.execute(
                    """UPDATE task_sequence_items SET state='canceled',
                       finished_at=COALESCE(finished_at,?),updated_at=?
                       WHERE sequence_id=? AND state IN ('pending','running')""",
                    (now, now, sequence_id),
                )
                state = "canceled"
            return state

    def claim_due_task_sequence_items(
            self, now: str | None = None, limit: int = 32,
            owned_agent_keys: set[tuple[str, str]] | None = None) -> list[dict[str, Any]]:
        """Claim at most one due item per queue and create its local run.start."""
        now = str(now or _now())
        try:
            limit = max(1, min(int(limit), 256))
        except (TypeError, ValueError):
            limit = 32
        with self._lock, self._db() as db:
            owner_clause = ""
            params: list[Any] = [now]
            if owned_agent_keys is not None:
                if not owned_agent_keys:
                    return []
                predicates: list[str] = []
                for local_ref, adapter in sorted(owned_agent_keys):
                    if not local_ref:
                        continue
                    if adapter:
                        predicates.append(
                            "(json_extract(i.payload_json,'$.local_ref')=? "
                            "AND json_extract(i.payload_json,'$.adapter')=?)")
                        params.extend([local_ref, adapter])
                    else:
                        predicates.append("json_extract(i.payload_json,'$.local_ref')=?")
                        params.append(local_ref)
                if not predicates:
                    return []
                owner_clause = " AND (" + " OR ".join(predicates) + ")"
            params.append(limit)
            rows = db.execute(
                f"""SELECT s.*,i.item_id,i.position,i.run_id,i.payload_json,i.state item_state
                   FROM task_sequences s JOIN task_sequence_items i
                     ON i.sequence_id=s.sequence_id AND i.position=s.next_position
                   WHERE s.state='ready' AND s.next_due_at<=? AND s.active_run_id IS NULL
                     AND i.state='pending'{owner_clause}
                   ORDER BY s.next_due_at,s.sequence_id LIMIT ?""",
                params,
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                payload = json.loads(row["payload_json"])
                command = db.execute(
                    """SELECT event_id,payload_json FROM commands
                       WHERE run_id=? AND event_type='run.start'""",
                    (row["run_id"],),
                ).fetchone()
                if command is None:
                    event_id = "evt_" + uuid.uuid4().hex
                    db.execute(
                        """INSERT INTO commands(
                             event_id,run_id,event_type,payload_json,state,created_at,updated_at)
                           VALUES (?,?, 'run.start',?,'persisted',?,?)""",
                        (event_id, row["run_id"], row["payload_json"], now, now),
                    )
                else:
                    event_id = str(command["event_id"])
                    if command["payload_json"] != row["payload_json"]:
                        raise ValueError("顺序任务 run.start 内容冲突")
                db.execute(
                    """UPDATE task_sequences SET state='running',active_run_id=?,
                       started_at=COALESCE(started_at,?),updated_at=?
                       WHERE sequence_id=? AND state='ready' AND active_run_id IS NULL""",
                    (row["run_id"], now, now, row["sequence_id"]),
                )
                db.execute(
                    """UPDATE task_sequence_items SET state='running',
                       started_at=COALESCE(started_at,?),updated_at=?
                       WHERE item_id=? AND state='pending'""",
                    (now, now, row["item_id"]),
                )
                result.append({**dict(row), "event_id": event_id,
                               "payload": payload, "state": "running"})
            return result

    def finish_task_sequence_item(self, sequence_id: str, item_id: str,
                                  run_id: str, state: str,
                                  error_detail: str | None = None) -> None:
        """Advance after completion, or stop the queue on failure/cancel."""
        sequence_id = str(sequence_id or "").strip()
        item_id = str(item_id or "").strip()
        run_id = str(run_id or "").strip()
        if state not in {"completed", "failed", "canceled"}:
            raise ValueError("顺序任务终态无效")
        now = _now()
        with self._lock, self._db() as db:
            item = db.execute(
                "SELECT position,state FROM task_sequence_items WHERE item_id=? AND sequence_id=? AND run_id=?",
                (item_id, sequence_id, run_id),
            ).fetchone()
            if item is None or str(item["state"]) in {"completed", "failed", "canceled"}:
                return
            db.execute(
                """UPDATE task_sequence_items SET state=?,error_detail=?,
                   finished_at=COALESCE(finished_at,?),updated_at=?
                   WHERE item_id=? AND state NOT IN ('completed','failed','canceled')""",
                (state, str(error_detail or "")[:1000] or None, now, now, item_id),
            )
            sequence = db.execute(
                "SELECT state,expected_count FROM task_sequences WHERE sequence_id=?",
                (sequence_id,),
            ).fetchone()
            if sequence is None or str(sequence["state"]) in {"completed", "failed", "canceled"}:
                return
            if state == "completed":
                next_position = int(item["position"]) + 1
                if next_position >= int(sequence["expected_count"]):
                    db.execute(
                        """UPDATE task_sequences SET state='completed',next_position=?,active_run_id=NULL,
                           finished_at=COALESCE(finished_at,?),updated_at=? WHERE sequence_id=?""",
                        (next_position, now, now, sequence_id),
                    )
                else:
                    next_due = (datetime.now(timezone.utc) + timedelta(
                        seconds=SEQUENCE_GAP_SECONDS)).isoformat(
                        timespec="milliseconds").replace("+00:00", "Z")
                    db.execute(
                        """UPDATE task_sequences SET state='ready',next_position=?,active_run_id=NULL,
                           next_due_at=?,updated_at=? WHERE sequence_id=?""",
                        (next_position, next_due, now, sequence_id),
                    )
            else:
                db.execute(
                    """UPDATE task_sequences SET state=?,active_run_id=NULL,
                       error_detail=?,finished_at=COALESCE(finished_at,?),updated_at=?
                       WHERE sequence_id=? AND state NOT IN ('completed','canceled')""",
                    (state, str(error_detail or "")[:1000] or None, now, now, sequence_id),
                )
                db.execute(
                    """UPDATE task_sequence_items SET state='canceled',
                       error_detail='前一项未完成，后续任务未执行',
                       finished_at=COALESCE(finished_at,?),updated_at=?
                       WHERE sequence_id=? AND state='pending'""",
                    (now, now, sequence_id),
                )
