"""Durable state in one SQLite file: runs, the audit trail, and approval requests.

A run can pause for hours waiting for a person, survive a restart, and resume exactly where it
stopped, because its whole state (stage, loop counts, case file, agent conversation) is saved
after every step. Every model call, tool call, policy decision, approval and hand-off is an
event, so the trace of a run can be replayed and audited.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, scenario TEXT NOT NULL, status TEXT NOT NULL,
    created REAL NOT NULL, updated REAL NOT NULL, state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, ts REAL NOT NULL,
    agent TEXT, kind TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run ON events (run_id, seq);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, agent TEXT NOT NULL, tool TEXT NOT NULL,
    arguments TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL,
    decided_by TEXT, note TEXT, created REAL NOT NULL, decided REAL
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------ runs

    def create_run(self, scenario: str, state: dict) -> str:
        run_id = "run_" + uuid.uuid4().hex[:10]
        now = time.time()
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, scenario, state["status"], now, now, json.dumps(state)),
            )
        return run_id

    def save_run(self, run_id: str, state: dict) -> None:
        # A run stopped by the kill switch stays stopped: a save from the runner that raced
        # with the halt must not quietly set it back to running.
        with self._lock, self.connect() as db:
            db.execute(
                "UPDATE runs SET status = ?, updated = ?, state = ? "
                "WHERE id = ? AND (status != 'halted' OR ? = 'halted')",
                (state["status"], time.time(), json.dumps(state), run_id, state["status"]),
            )

    def run_status(self, run_id: str) -> str:
        with self.connect() as db:
            row = db.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"No run {run_id}")
        return row["status"]

    def halt(self, run_id: str) -> None:
        """Kill switch for one run: it stops before its next model call and cannot resume."""
        with self._lock, self.connect() as db:
            db.execute(
                "UPDATE runs SET status = 'halted', updated = ? WHERE id = ?", (time.time(), run_id)
            )
            db.execute(
                "UPDATE approvals SET status = 'cancelled' WHERE run_id = ? AND status = 'pending'",
                (run_id,),
            )

    def load_run(self, run_id: str) -> tuple[str, dict]:
        with self.connect() as db:
            row = db.execute("SELECT scenario, state FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"No run {run_id}")
        return row["scenario"], json.loads(row["state"])

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, scenario, status, created FROM runs ORDER BY created DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ audit trail

    def event(self, run_id: str, agent: str | None, kind: str, **detail) -> None:
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO events (run_id, ts, agent, kind, detail) VALUES (?, ?, ?, ?, ?)",
                (run_id, time.time(), agent, kind, json.dumps(detail, ensure_ascii=False)),
            )

    def events(self, run_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT seq, ts, agent, kind, detail FROM events WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]

    # ------------------------------------------------------------------ approvals

    def request_approval(self, run_id, agent, tool, arguments, reason) -> str:
        approval_id = "apr_" + uuid.uuid4().hex[:8]
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO approvals (id, run_id, agent, tool, arguments, reason, status, "
                "created) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                (approval_id, run_id, agent, tool, json.dumps(arguments), reason, time.time()),
            )
        return approval_id

    def pending_approvals(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM approvals WHERE status = 'pending' ORDER BY created"
            ).fetchall()
        return [{**dict(r), "arguments": json.loads(r["arguments"])} for r in rows]

    def get_approval(self, approval_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            raise KeyError(f"No approval {approval_id}")
        return {**dict(row), "arguments": json.loads(row["arguments"])}

    def decide_approval(self, approval_id: str, approved: bool, by: str, note: str) -> None:
        with self._lock, self.connect() as db:
            updated = db.execute(
                "UPDATE approvals SET status = ?, decided_by = ?, note = ?, decided = ? "
                "WHERE id = ? AND status = 'pending'",
                ("approved" if approved else "rejected", by, note, time.time(), approval_id),
            ).rowcount
        if not updated:
            raise ValueError(f"Approval {approval_id} is not pending")
