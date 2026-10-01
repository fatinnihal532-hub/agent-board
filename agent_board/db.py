"""SQLite persistence for tasks and their state machine."""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

QUEUED, RUNNING, REVIEW, DONE, FAILED = "queued", "running", "review", "done", "failed"
STATUSES = (QUEUED, RUNNING, REVIEW, DONE, FAILED)

# Every legal move. Anything else is rejected, so the board cannot reach an impossible state.
TRANSITIONS = {
    QUEUED: {RUNNING},
    RUNNING: {REVIEW, FAILED, QUEUED},  # back to queued only when a crashed run is recovered
    REVIEW: {DONE, QUEUED},             # approve, or send back with feedback
    FAILED: {QUEUED},                   # retry
    DONE: set(),
}


class InvalidTransition(Exception):
    pass


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    agent: str
    instructions: str
    budget: float
    status: str
    output: str
    feedback: str
    error: str
    attempts: int
    cost: float
    input_tokens: int
    output_tokens: int
    created_at: float
    updated_at: float

    def to_dict(self) -> dict:
        return asdict(self)


class Database:
    def __init__(self, path: str | Path = "agent-board.db"):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    instructions TEXT NOT NULL DEFAULT '',
                    budget REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'queued',
                    output TEXT NOT NULL DEFAULT '',
                    feedback TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    cost REAL NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def create(self, title: str, agent: str, instructions: str = "", budget: float = 0.0) -> Task:
        now = time.time()
        task_id = uuid.uuid4().hex
        with self._lock:
            self._db.execute(
                "INSERT INTO tasks (id, title, agent, instructions, budget, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, title, agent, instructions, budget, now, now),
            )
            self._db.commit()
        return self.get(task_id)

    def get(self, task_id: str) -> Task:
        with self._lock:
            row = self._db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return Task(**dict(row))

    def all(self) -> list[Task]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM tasks ORDER BY created_at, id").fetchall()
        return [Task(**dict(r)) for r in rows]

    def delete(self, task_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self._db.commit()
        return cur.rowcount > 0

    def move(self, task_id: str, status: str, **fields) -> Task:
        """Change a task's status, checking the move is legal, and set any other columns."""
        with self._lock:
            current = self.get(task_id)
            if status not in TRANSITIONS[current.status]:
                raise InvalidTransition(f"cannot move a task from {current.status} to {status}")
            columns = {"status": status, "updated_at": time.time(), **fields}
            assignments = ", ".join(f"{name} = ?" for name in columns)
            self._db.execute(f"UPDATE tasks SET {assignments} WHERE id = ?",
                             (*columns.values(), task_id))
            self._db.commit()
            return self.get(task_id)

    def claim(self, task_id: str) -> Task | None:
        """Atomically take a queued task for running. Returns None if someone else got it."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE tasks SET status = ?, attempts = attempts + 1, error = '', updated_at = ? "
                "WHERE id = ? AND status = ?",
                (RUNNING, time.time(), task_id, QUEUED),
            )
            self._db.commit()
            return self.get(task_id) if cur.rowcount else None

    def append_output(self, task_id: str, text: str) -> None:
        with self._lock:
            self._db.execute("UPDATE tasks SET output = output || ?, updated_at = ? WHERE id = ?",
                             (text, time.time(), task_id))
            self._db.commit()

    def recover(self) -> list[Task]:
        """Requeue tasks left running by a previous process that stopped mid-run."""
        return [self.move(t.id, QUEUED, error="") for t in self.all() if t.status == RUNNING]
