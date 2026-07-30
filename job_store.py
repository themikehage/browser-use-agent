from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, db_path: Path, max_jobs: int = 100):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_jobs = max_jobs
        self._lock = threading.Lock()
        self._init_db()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    task TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    steps INTEGER NOT NULL DEFAULT 0,
                    current_url TEXT,
                    last_screenshot TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_events_job ON job_events(job_id, id);
                CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
                """
            )

    def create_job(self, task: str) -> dict[str, Any]:
        job_id = uuid.uuid4().hex[:12]
        now = _utc_now()
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO jobs (id, task, status, created_at)
                VALUES (?, ?, 'queued', ?)
                """,
                (job_id, task, now),
            )
            self._add_event(conn, job_id, "status", {"status": "queued"})
            self._prune(conn)
        return self.get_job(job_id)  # type: ignore[return-value]

    def _prune(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT id FROM jobs ORDER BY created_at DESC"
        ).fetchall()
        if len(rows) <= self.max_jobs:
            return
        for row in rows[self.max_jobs :]:
            conn.execute("DELETE FROM jobs WHERE id = ?", (row["id"],))

    def _add_event(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> int:
        cur = conn.execute(
            """
            INSERT INTO job_events (job_id, ts, type, payload)
            VALUES (?, ?, ?, ?)
            """,
            (job_id, _utc_now(), event_type, json.dumps(payload, ensure_ascii=False)),
        )
        return int(cur.lastrowid)

    def add_event(self, job_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self._conn() as conn:
            event_id = self._add_event(conn, job_id, event_type, payload)
            row = conn.execute(
                "SELECT id, job_id, ts, type, payload FROM job_events WHERE id = ?",
                (event_id,),
            ).fetchone()
        return self._event_row(row)

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "status",
            "result",
            "error",
            "steps",
            "current_url",
            "last_screenshot",
            "started_at",
            "finished_at",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_job(job_id)
        sets = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [job_id]
        with self._lock, self._conn() as conn:
            conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", values)
            if "status" in updates:
                self._add_event(conn, job_id, "status", {"status": updates["status"]})
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._job_row(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._job_row(r) for r in rows]

    def get_active_job(self) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('queued', 'running')
                ORDER BY created_at ASC
                LIMIT 1
                """
            ).fetchone()
        return self._job_row(row) if row else None

    def list_events(
        self, job_id: str, after_id: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, job_id, ts, type, payload FROM job_events
                WHERE job_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (job_id, after_id, limit),
            ).fetchall()
        return [self._event_row(r) for r in rows]

    @staticmethod
    def _job_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "task": row["task"],
            "status": row["status"],
            "result": row["result"],
            "error": row["error"],
            "steps": row["steps"],
            "current_url": row["current_url"],
            "last_screenshot": row["last_screenshot"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "ts": row["ts"],
            "type": row["type"],
            "payload": json.loads(row["payload"]),
        }
