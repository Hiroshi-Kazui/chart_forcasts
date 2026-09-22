from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, task_path TEXT NOT NULL, status TEXT NOT NULL,
 phase TEXT NOT NULL, controller_pid INTEGER, created REAL NOT NULL,
 updated REAL NOT NULL, message TEXT NOT NULL DEFAULT '', fix_count INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_task ON runs(task_path)
WHERE status IN ('QUEUED','RUNNING');
"""


class Store:
    def __init__(self, root: Path):
        self.path = root / ".harness" / "state.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def create(self, run_id: str, task_path: Path) -> None:
        now = time.time()
        with self.connect() as db:
            db.execute(
                "INSERT INTO runs(id,task_path,status,phase,created,updated) VALUES(?,?,?,?,?,?)",
                (run_id, str(task_path), "QUEUED", "準備", now, now),
            )

    def update(self, run_id: str, **values: Any) -> None:
        values["updated"] = time.time()
        columns = ",".join(f"{k}=?" for k in values)
        with self.connect() as db:
            cur = db.execute(f"UPDATE runs SET {columns} WHERE id=?", (*values.values(), run_id))
            if cur.rowcount != 1:
                raise KeyError(run_id)

    def transition(self, run_id: str, expected: tuple[str, ...], **values: Any) -> bool:
        values["updated"] = time.time()
        columns = ",".join(f"{k}=?" for k in values)
        marks = ",".join("?" for _ in expected)
        with self.connect() as db:
            cur = db.execute(
                f"UPDATE runs SET {columns} WHERE id=? AND status IN ({marks})",
                (*values.values(), run_id, *expected),
            )
            return cur.rowcount == 1

    def get(self, run_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return dict(row)

    def write_event(self, run_dir: Path, event: dict[str, Any]) -> None:
        event = {"time": time.time(), **event}
        with (run_dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
