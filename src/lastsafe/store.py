from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from threading import Lock

from .models import PendingIntent, RunRecord


class RunStore:
    HASH_SCHEMA_VERSION = 2

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
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_intents (
                incident_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS attempts (
                incident_key TEXT PRIMARY KEY,
                attempt INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._connection.commit()
        hash_schema = self.get_metadata("hash_schema_version")
        run_count = self._connection.execute("SELECT COUNT(*) AS count FROM runs").fetchone()[
            "count"
        ]
        if hash_schema is None and run_count == 0:
            self.set_metadata("hash_schema_version", str(self.HASH_SCHEMA_VERSION))

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
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT record_hash FROM runs ORDER BY created_at DESC, rowid DESC LIMIT 1"
                ).fetchone()
                expected_previous = row["record_hash"] if row else "GENESIS"
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
            except Exception:
                self._connection.rollback()
                raise
        return record

    def verify(self) -> tuple[bool, int]:
        if str(self.get_metadata("hash_schema_version")) != str(
            self.HASH_SCHEMA_VERSION
        ):
            row = self._connection.execute("SELECT COUNT(*) AS count FROM runs").fetchone()
            return False, int(row["count"])
        records = list(reversed(self.list(limit=10_000)))
        previous = "GENESIS"
        for record in records:
            if record.previous_hash != previous or record.record_hash != self.hash_record(record):
                return False, len(records)
            previous = record.record_hash
        return True, len(records)

    def set_metadata(self, key: str, value: dict | str) -> None:
        payload = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO metadata (key, value, updated_at) VALUES (?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, payload),
            )
            self._connection.commit()

    def get_metadata(self, key: str) -> dict | str | None:
        row = self._connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return str(row["value"])

    def acquire_lease(self, owner: str, ttl_seconds: int = 600) -> str | None:
        now = time.time()
        token = hashlib.sha256(f"{owner}:{now}:{time.monotonic_ns()}".encode()).hexdigest()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT value FROM metadata WHERE key = 'worker_lease'"
                ).fetchone()
                current = json.loads(row["value"]) if row else None
                if isinstance(current, dict) and float(current.get("expires_at", 0)) > now:
                    self._connection.rollback()
                    return None
                self._connection.execute(
                    """
                    INSERT INTO metadata (key, value, updated_at)
                    VALUES (?, ?, datetime('now'))
                    ON CONFLICT(key) DO UPDATE SET
                        value=excluded.value,
                        updated_at=excluded.updated_at
                    """,
                    (
                        "worker_lease",
                        json.dumps(
                            {
                                "owner": owner,
                                "token": token,
                                "expires_at": now + ttl_seconds,
                            }
                        ),
                    ),
                )
                self._connection.commit()
                return token
            except Exception:
                self._connection.rollback()
                raise

    def renew_lease(self, token: str, ttl_seconds: int = 600) -> bool:
        now = time.time()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT value FROM metadata WHERE key = 'worker_lease'"
                ).fetchone()
                current = json.loads(row["value"]) if row else None
                if not isinstance(current, dict) or current.get("token") != token:
                    self._connection.rollback()
                    return False
                current["expires_at"] = now + ttl_seconds
                self._connection.execute(
                    "UPDATE metadata SET value = ?, updated_at = datetime('now') "
                    "WHERE key = 'worker_lease'",
                    (json.dumps(current),),
                )
                self._connection.commit()
                return True
            except Exception:
                self._connection.rollback()
                raise

    def owns_lease(self, token: str) -> bool:
        row = self._connection.execute(
            "SELECT value FROM metadata WHERE key = 'worker_lease'"
        ).fetchone()
        if row is None:
            return False
        current = json.loads(row["value"])
        return bool(
            isinstance(current, dict)
            and current.get("token") == token
            and float(current.get("expires_at", 0)) > time.time()
        )

    def release_lease(self, token: str) -> bool:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT value FROM metadata WHERE key = 'worker_lease'"
                ).fetchone()
                current = json.loads(row["value"]) if row else None
                if not isinstance(current, dict) or current.get("token") != token:
                    self._connection.rollback()
                    return False
                self._connection.execute("DELETE FROM metadata WHERE key = 'worker_lease'")
                self._connection.commit()
                return True
            except Exception:
                self._connection.rollback()
                raise

    def current_attempt(self, incident_key: str) -> int:
        row = self._connection.execute(
            "SELECT attempt FROM attempts WHERE incident_key = ?", (incident_key,)
        ).fetchone()
        return int(row["attempt"]) if row else 1

    def advance_attempt(self, incident_key: str) -> int:
        with self._lock:
            current = self.current_attempt(incident_key)
            next_attempt = current + 1
            self._connection.execute(
                """
                INSERT INTO attempts (incident_key, attempt, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(incident_key) DO UPDATE SET
                    attempt=excluded.attempt,
                    updated_at=excluded.updated_at
                """,
                (incident_key, next_attempt),
            )
            self._connection.commit()
            return next_attempt

    def save_intent(self, intent: PendingIntent) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO pending_intents (incident_key, payload, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(incident_key) DO UPDATE SET
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (intent.incident_key, intent.model_dump_json()),
            )
            self._connection.commit()

    def get_intent(self, incident_key: str) -> PendingIntent | None:
        row = self._connection.execute(
            "SELECT payload FROM pending_intents WHERE incident_key = ?", (incident_key,)
        ).fetchone()
        return PendingIntent.model_validate_json(row["payload"]) if row else None

    def list_pending_intents(self) -> list[PendingIntent]:
        rows = self._connection.execute(
            "SELECT payload FROM pending_intents ORDER BY updated_at"
        ).fetchall()
        intents = [PendingIntent.model_validate_json(row["payload"]) for row in rows]
        return [intent for intent in intents if intent.state != "terminal"]

    @staticmethod
    def hash_record(record: RunRecord) -> str:
        payload = record.model_dump(mode="json", exclude={"record_hash"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
