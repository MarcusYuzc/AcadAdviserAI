"""Search ownership and spend limits. Holds no student content."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    thread_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    consented_at TEXT NOT NULL,
    follow_ups INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS model_runs (
    at TEXT NOT NULL,
    ip_hash TEXT NOT NULL,
    kind TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS model_runs_at ON model_runs (at);
"""


class LimitReached(Exception):
    """The message is shown to the visitor."""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.executescript(_SCHEMA)

    def _reserve(self, ip: str, kind: str, global_limit: int, ip_limit: int | None) -> None:
        since = (_now() - timedelta(days=1)).isoformat()
        ip_hash = digest(ip)
        if self._count(since) >= global_limit:
            raise LimitReached(
                "We have reached today's search capacity. Please try again tomorrow."
            )
        if ip_limit is not None and self._count(since, ip_hash, kind) >= ip_limit:
            raise LimitReached(
                "You have reached the daily search limit. Please try again tomorrow."
            )
        self._conn.execute(
            "INSERT INTO model_runs (at, ip_hash, kind) VALUES (?, ?, ?)",
            (_now().isoformat(), ip_hash, kind),
        )

    def _count(self, since: str, ip_hash: str | None = None, kind: str | None = None) -> int:
        sql, args = "SELECT COUNT(*) FROM model_runs WHERE at >= ?", [since]
        if ip_hash is not None:
            sql += " AND ip_hash = ? AND kind = ?"
            args += [ip_hash, kind]
        return self._conn.execute(sql, args).fetchone()[0]

    def reserve_search(self, ip: str, global_limit: int, ip_limit: int) -> None:
        with self._lock:
            self._reserve(ip, "search", global_limit, ip_limit)

    def reserve_follow_up(self, thread_id: str, ip: str, global_limit: int, limit: int) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT follow_ups FROM searches WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if row is None or row[0] >= limit:
                raise LimitReached(
                    "This search has reached its refinement limit. Please start a new search."
                )
            self._reserve(ip, "follow_up", global_limit, None)
            self._conn.execute(
                "UPDATE searches SET follow_ups = follow_ups + 1 WHERE thread_id = ?",
                (thread_id,),
            )

    def add_search(self, thread_id: str, owner_token: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO searches (thread_id, owner, consented_at) VALUES (?, ?, ?)",
                (thread_id, digest(owner_token), _now().isoformat()),
            )

    def owns(self, thread_id: str, owner_token: str | None) -> bool:
        if not owner_token:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT owner FROM searches WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        return row is not None and row[0] == digest(owner_token)
