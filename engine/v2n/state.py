"""Append-only SQLite state store with leases and a dead-letter queue.

Why SQLite instead of JSON snapshots:

* events are append-only, so history is never overwritten by a crash;
* ``claim()`` is one atomic ``UPDATE ... RETURNING`` — two workers cannot take
  the same task;
* expired leases are reclaimed (visibility-timeout semantics);
* failures beyond ``max_attempts`` land in a dead-letter view for human review;
* progress is measured by "when was the last event", not "is a PID alive".

Windows/WAL note: when several processes write at once, a checkpoint can make
another process briefly see ``SQLITE_READONLY``. ``sqlite3.connect(timeout=30)``
only covers BUSY, so :func:`_exec_retry` retries the transient errors with
exponential backoff.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    lesson_key  TEXT NOT NULL,
    stage       TEXT NOT NULL,
    status      TEXT NOT NULL,
    detail      TEXT,
    actor       TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_lesson ON events(lesson_key, id);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_key  TEXT NOT NULL,
    stage       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    priority    INTEGER NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0,
    claimed_at  TEXT,
    claimed_by  TEXT,
    lease_sec   INTEGER NOT NULL DEFAULT 1800,
    last_error  TEXT,
    updated_at  TEXT,
    UNIQUE(lesson_key, stage)
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, priority DESC, id);

CREATE TABLE IF NOT EXISTS traces (
    lesson_key  TEXT PRIMARY KEY,
    data        TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE VIEW IF NOT EXISTS v_progress AS
SELECT e.lesson_key, e.stage, e.status, e.ts, e.detail
FROM events e
JOIN (SELECT lesson_key, MAX(id) AS mid FROM events GROUP BY lesson_key) m
  ON m.mid = e.id;

CREATE VIEW IF NOT EXISTS v_dead AS
SELECT lesson_key, stage, attempts, last_error, updated_at
FROM tasks WHERE status = 'dead';
"""

_RETRYABLE = ("readonly", "database is locked", "database is busy",
              "unable to open database")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _exec_retry(conn: sqlite3.Connection, sql: str, params: tuple = (),
                *, attempts: int = 6) -> sqlite3.Cursor:
    """Retry BUSY/LOCKED on a single connection.

    This does **not** recover SQLITE_READONLY: measured on Windows, a connection
    that hits "attempt to write a readonly database" failed 100/100 retries,
    while reopening the database always recovered. Use :meth:`Store._exec`,
    which reconnects, for anything that may see READONLY.
    """
    last: Exception | None = None
    for i in range(attempts):
        try:
            return conn.execute(sql, params)
        except sqlite3.OperationalError as exc:
            if not any(k in str(exc).lower() for k in _RETRYABLE):
                raise
            last = exc
            time.sleep(0.05 * (2 ** i))
    raise last  # type: ignore[misc]


class Store:
    """Thin wrapper around a single SQLite connection."""

    def __init__(self, path: Path, *, max_attempts: int = 3,
                 lease_sec: int = 1800):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max_attempts
        self.lease_sec = lease_sec
        self.conn = self._open()
        self._exec_script(SCHEMA)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30.0,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _reconnect(self) -> None:
        """Replace the connection after SQLITE_READONLY.

        Root cause (measured, 8 threads x 40 tasks x 20 rounds): in WAL mode a
        connection that loses its -shm write mapping stays read-only forever.
        Retrying the same connection recovered 0/100 times; reopening recovered
        5/5. Retrying in place is therefore not a fix.
        """
        with contextlib.suppress(sqlite3.Error):
            self.conn.close()
        self.conn = self._open()

    def _exec(self, sql: str, params: tuple = (), *,
              attempts: int = 8) -> sqlite3.Cursor:
        last: Exception | None = None
        for i in range(attempts):
            try:
                return self.conn.execute(sql, params)
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if "readonly" in msg:
                    self._reconnect()
                    last = exc
                    time.sleep(0.02 * (2 ** min(i, 4)))
                    continue
                if not any(k in msg for k in _RETRYABLE):
                    raise
                last = exc
                time.sleep(0.05 * (2 ** i))
        raise last  # type: ignore[misc]

    def _exec_script(self, script: str, *, attempts: int = 6) -> None:
        last: Exception | None = None
        for i in range(attempts):
            try:
                self.conn.executescript(script)
                return
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if "readonly" in msg:
                    self._reconnect()
                    last = exc
                    continue
                if not any(k in msg for k in _RETRYABLE):
                    raise
                last = exc
                time.sleep(0.05 * (2 ** i))
        raise last  # type: ignore[misc]

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self.conn.close()

    # `typing.Self` only exists on 3.11+; this package supports 3.10.
    def __enter__(self) -> Store:  # noqa: PYI034
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- events
    def log_event(self, lesson_key: str, stage: str, status: str,
                  detail: Any = None, *, actor: str = "agent") -> None:
        self._exec(
            "INSERT INTO events(ts, lesson_key, stage, status, detail, actor) "
            "VALUES(?,?,?,?,?,?)",
            (_now(), str(lesson_key), stage, status,
             json.dumps(detail, ensure_ascii=False) if detail is not None else None,
             actor),
        )

    def events(self, lesson_key: str) -> list[dict]:
        rows = self._exec(
            "SELECT ts, stage, status, detail, actor FROM events "
            "WHERE lesson_key=? ORDER BY id", (str(lesson_key),)).fetchall()
        return [dict(r) for r in rows]

    def last_stage(self, lesson_key: str) -> str | None:
        row = self._exec(
            "SELECT stage, status FROM events WHERE lesson_key=? "
            "ORDER BY id DESC LIMIT 1", (str(lesson_key),)).fetchone()
        if not row:
            return None
        return f"{row['stage']}:{row['status']}"

    # -------------------------------------------------------------- tasks
    def enqueue(self, lesson_key: str, stage: str, *, priority: int = 0) -> None:
        self._exec(
            "INSERT INTO tasks(lesson_key, stage, status, priority, updated_at) "
            "VALUES(?,?,'pending',?,?) "
            "ON CONFLICT(lesson_key, stage) DO UPDATE SET priority=excluded.priority "
            "WHERE tasks.status IN ('pending','failed')",
            (str(lesson_key), stage, int(priority), _now()),
        )

    def claim(self, *, worker: str) -> dict | None:
        row = self._exec(
            "UPDATE tasks SET status='claimed', claimed_at=?, claimed_by=?, "
            "lease_sec=?, attempts=attempts+1, updated_at=? "
            "WHERE id = (SELECT id FROM tasks WHERE status='pending' "
            "ORDER BY priority DESC, id LIMIT 1) "
            "RETURNING id, lesson_key, stage, attempts",
            (_now(), worker, self.lease_sec, _now()),
        ).fetchone()
        if not row:
            return None
        self.log_event(row["lesson_key"], row["stage"], "claimed",
                       {"worker": worker, "attempt": row["attempts"]}, actor=worker)
        return dict(row)

    def complete(self, task_id: int, *, detail: Any = None) -> None:
        row = self._exec(
            "SELECT lesson_key, stage FROM tasks WHERE id=?", (task_id,)).fetchone()
        self._exec(
                    "UPDATE tasks SET status='done', updated_at=? WHERE id=?",
                    (_now(), task_id))
        if row:
            self.log_event(row["lesson_key"], row["stage"], "done", detail)

    def fail(self, task_id: int, error: str) -> str:
        row = self._exec(
            "SELECT lesson_key, stage, attempts FROM tasks WHERE id=?",
            (task_id,)).fetchone()
        if not row:
            return "unknown"
        status = "dead" if row["attempts"] >= self.max_attempts else "pending"
        self._exec(
            "UPDATE tasks SET status=?, last_error=?, updated_at=?, "
            "claimed_at=NULL WHERE id=?",
            (status, str(error)[:500], _now(), task_id),
        )
        self.log_event(row["lesson_key"], row["stage"],
                       "failed" if status == "pending" else "dead",
                       {"error": str(error)[:300], "attempts": row["attempts"]})
        return status

    def reclaim_expired(self) -> int:
        now = datetime.now(timezone.utc)
        rows = self._exec(
            "SELECT id, claimed_at, lease_sec FROM tasks WHERE status='claimed'"
        ).fetchall()
        n = 0
        for r in rows:
            try:
                claimed = datetime.fromisoformat(r["claimed_at"])
            except (TypeError, ValueError):
                continue
            if now - claimed > timedelta(seconds=int(r["lease_sec"] or 1800)):
                self._exec(
                    "UPDATE tasks SET status='pending', claimed_at=NULL, "
                    "updated_at=? WHERE id=?", (_now(), r["id"]))
                n += 1
        return n

    def queue_depth(self) -> dict[str, int]:
        rows = self._exec(
            "SELECT status, COUNT(*) c FROM tasks GROUP BY status").fetchall()
        return {r["status"]: r["c"] for r in rows}

    # ------------------------------------------------------------- traces
    def upsert_trace(self, lesson_key: str, metrics: dict) -> None:
        self._exec(
            "INSERT INTO traces(lesson_key, data, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(lesson_key) DO UPDATE SET data=excluded.data, "
            "updated_at=excluded.updated_at",
            (str(lesson_key), json.dumps(metrics, ensure_ascii=False), _now()),
        )

    def get_trace(self, lesson_key: str) -> dict | None:
        row = self._exec(
            "SELECT data FROM traces WHERE lesson_key=?", (str(lesson_key),)
        ).fetchone()
        return json.loads(row["data"]) if row else None

    # ------------------------------------------------------------ derived
    def progress(self) -> dict:
        row = self._exec(
            "SELECT COUNT(*) c FROM events").fetchone()
        lessons = self._exec(
            "SELECT COUNT(DISTINCT lesson_key) c FROM events").fetchone()
        last = self._exec(
            "SELECT ts FROM events ORDER BY id DESC LIMIT 1").fetchone()
        stall = None
        if last:
            try:
                stall = round((datetime.now(timezone.utc)
                               - datetime.fromisoformat(last["ts"]))
                              .total_seconds() / 60.0, 1)
            except ValueError:
                stall = None
        return {
            "events": row["c"],
            "lessons_touched": lessons["c"],
            "queue": self.queue_depth(),
            "stall_minutes": stall,
        }

    def dead_letters(self) -> list[dict]:
        rows = self._exec(
            "SELECT lesson_key, stage, attempts, last_error FROM v_dead"
        ).fetchall()
        return [dict(r) for r in rows]
