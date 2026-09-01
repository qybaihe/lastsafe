from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from threading import Lock

from .models import RunRecord


class RunStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._lock = Lock()
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                mode TEXT NOT NULL,
                payload TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                record_hash TEXT NOT NULL UNIQUE
            )
            """
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def latest(self) -> RunRecord | None:
        row = self._connection.execute(
            "SELECT payload FROM runs ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return RunRecord.model_validate_json(row["payload"]) if row else None

    def list(self, limit: int = 25) -> list[RunRecord]:
        rows = self._connection.execute(
            "SELECT payload FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [RunRecord.model_validate_json(row["payload"]) for row in rows]

    def append(self, record: RunRecord) -> RunRecord:
        with self._lock:
            latest = self.latest()
            expected_previous = latest.record_hash if latest else "GENESIS"
            if record.previous_hash != expected_previous:
                raise ValueError("Run record does not extend the current audit chain")
            expected_hash = self.hash_record(record)
            if record.record_hash != expected_hash:
                raise ValueError("Run record hash does not match its contents")
            self._connection.execute(
                """
                INSERT INTO runs (id, created_at, mode, payload, previous_hash, record_hash)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.created_at.isoformat(),
                    record.mode,
                    record.model_dump_json(),
                    record.previous_hash,
                    record.record_hash,
                ),
            )
            self._connection.commit()
        return record

    def verify(self) -> tuple[bool, int]:
        records = list(reversed(self.list(limit=10_000)))
        previous = "GENESIS"
        for record in records:
            if record.previous_hash != previous or record.record_hash != self.hash_record(record):
                return False, len(records)
            previous = record.record_hash
        return True, len(records)

    @staticmethod
    def hash_record(record: RunRecord) -> str:
        payload = record.model_dump(mode="json", exclude={"record_hash"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
