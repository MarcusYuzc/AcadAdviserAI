"""Accounts, search ownership, plans and spend limits. Holds no student content."""
from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from acadadviser.billing import ACTIVE_STATUSES, StripeUpdate, SubscriptionRecord

_log = logging.getLogger("acadadviser")

# Version 1 tied searches to an anonymous cookie that no account can reach, so its tables are dropped.
_VERSION = 2
# Every write transaction takes this lock, so gateway tasks behave like SQLite's single writer.
_PG_WRITE_LOCK = 7_461_001
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    sub TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    created_at TEXT NOT NULL,
    stripe_customer_id TEXT UNIQUE,
    renewal_consent_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_sub TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS free_searches (
    email_key TEXT PRIMARY KEY,
    user_sub TEXT NOT NULL,
    thread_id TEXT,
    used_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    user_sub TEXT PRIMARY KEY,
    stripe_customer_id TEXT NOT NULL,
    stripe_subscription_id TEXT NOT NULL,
    status TEXT NOT NULL,
    current_period_end BIGINT,
    cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
    event_created BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS stripe_events (
    event_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS searches (
    thread_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    plan TEXT NOT NULL,
    consented_at TEXT NOT NULL,
    follow_ups INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS model_runs (
    at TEXT NOT NULL,
    ip_hash TEXT NOT NULL,
    kind TEXT NOT NULL,
    owner TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS model_runs_at ON model_runs (at);
"""


class LimitReached(Exception):
    """The message is shown to the visitor."""


class PaymentRequired(Exception):
    """The message is shown to the visitor, next to a link to subscribe."""


@dataclass(frozen=True)
class User:
    sub: str
    email: str
    stripe_customer_id: str | None


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _SqliteStore:
    """A local file, for development and tests."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        if self._conn.execute("PRAGMA user_version").fetchone()[0] < _VERSION:
            self._conn.executescript("DROP TABLE IF EXISTS searches; DROP TABLE IF EXISTS model_runs;")
            self._conn.execute(f"PRAGMA user_version = {_VERSION}")
        self._conn.executescript(_SCHEMA)

    @contextmanager
    def read(self) -> Iterator[Any]:
        with self._lock:
            yield self._conn

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")


class _PgConnection:
    """Gives a psycopg connection sqlite3's shape: "?" placeholders, execute() returns a cursor."""

    def __init__(self, conn: Any):
        self._conn = conn

    def execute(self, sql: str, args: Any = ()) -> Any:
        return self._conn.execute(sql.replace("?", "%s"), args)


class _PostgresStore:
    """RDS in production. Several gateway tasks can share it."""

    def __init__(self, conninfo: str):
        from psycopg_pool import ConnectionPool

        self._pool = ConnectionPool(
            conninfo, min_size=1, max_size=4, kwargs={"autocommit": True},
            check=ConnectionPool.check_connection, open=True,
        )
        with self.transaction() as db:
            for statement in filter(str.strip, _SCHEMA.split(";")):
                db.execute(statement)

    @contextmanager
    def read(self) -> Iterator[Any]:
        with self._pool.connection() as conn:
            yield _PgConnection(conn)

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_PG_WRITE_LOCK,))
            yield _PgConnection(conn)


class Database:
    def __init__(self, target: Path | str):
        """A Path is a SQLite file; a string is a Postgres connection string."""
        self._store = _SqliteStore(target) if isinstance(target, Path) else _PostgresStore(target)
        self._transaction = self._store.transaction
        self._read = self._store.read

    # Accounts and sessions

    def sign_in(self, sub: str, email: str, days: int) -> str:
        token = secrets.token_urlsafe(32)
        expires = (_now() + timedelta(days=days)).isoformat()
        with self._transaction() as db:
            db.execute(
                "INSERT INTO users (sub, email, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT (sub) DO UPDATE SET email = excluded.email",
                (sub, email, _now().isoformat()),
            )
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (_now().isoformat(),))
            db.execute(
                "INSERT INTO sessions (token_hash, user_sub, expires_at) VALUES (?, ?, ?)",
                (digest(token), sub, expires),
            )
        return token

    def session_user(self, token: str | None) -> User | None:
        if not token:
            return None
        with self._read() as db:
            row = db.execute(
                "SELECT u.sub, u.email, u.stripe_customer_id FROM sessions s "
                "JOIN users u ON u.sub = s.user_sub WHERE s.token_hash = ? AND s.expires_at > ?",
                (digest(token), _now().isoformat()),
            ).fetchone()
        return User(*row) if row else None

    def sign_out(self, token: str) -> None:
        with self._read() as db:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (digest(token),))

    def record_renewal_consent(self, sub: str) -> None:
        with self._read() as db:
            db.execute(
                "UPDATE users SET renewal_consent_at = ? WHERE sub = ?", (_now().isoformat(), sub)
            )

    def account(self, sub: str, email_key: str) -> dict:
        with self._read() as db:
            subscription = db.execute(
                "SELECT status, current_period_end, cancel_at_period_end FROM subscriptions "
                "WHERE user_sub = ?", (sub,),
            ).fetchone()
            free_used = self._free_used(db, sub, email_key)
        status, period_end, cancelling = subscription or (None, None, 0)
        return {
            "subscribed": status in ACTIVE_STATUSES,
            "status": status,
            "current_period_end": period_end,
            "cancel_at_period_end": bool(cancelling),
            "free_search_available": not free_used,
        }

    # Plans and spend limits

    def _subscribed(self, db: Any, sub: str) -> bool:
        row = db.execute("SELECT status FROM subscriptions WHERE user_sub = ?", (sub,)).fetchone()
        return row is not None and row[0] in ACTIVE_STATUSES

    def _free_used(self, db: Any, sub: str, email_key: str) -> bool:
        return db.execute(
            "SELECT 1 FROM free_searches WHERE email_key = ? OR user_sub = ?", (email_key, sub)
        ).fetchone() is not None

    def _count(self, db: Any, since: str, column: str | None = None, value: str | None = None,
               kind: str | None = None) -> int:
        sql, args = "SELECT COUNT(*) FROM model_runs WHERE at >= ?", [since]
        if column is not None:
            sql += f" AND {column} = ? AND kind = ?"
            args += [value, kind]
        return db.execute(sql, args).fetchone()[0]

    def _record_run(self, db: Any, ip: str, kind: str, owner: str, global_limit: int) -> None:
        since = (_now() - timedelta(days=1)).isoformat()
        if self._count(db, since) >= global_limit:
            raise LimitReached("We have reached today's search capacity. Please try again tomorrow.")
        db.execute(
            "INSERT INTO model_runs (at, ip_hash, kind, owner) VALUES (?, ?, ?, ?)",
            (_now().isoformat(), digest(ip), kind, owner),
        )

    def reserve_search(self, user: User, email_key: str, ip: str, *, global_limit: int,
                       ip_limit: int, user_limit: int) -> str:
        """Returns the plan the search runs on: "paid" for subscribers, otherwise "free"."""
        since = (_now() - timedelta(days=1)).isoformat()
        with self._transaction() as db:
            if self._subscribed(db, user.sub):
                if self._count(db, since, "owner", user.sub, "search") >= user_limit:
                    raise LimitReached(
                        f"You have reached the fair-use limit of {user_limit} searches a day. "
                        "Please try again tomorrow."
                    )
                plan = "paid"
            else:
                if self._free_used(db, user.sub, email_key):
                    raise PaymentRequired("You have used your free search. Subscribe to keep searching.")
                if self._count(db, since, "ip_hash", digest(ip), "search") >= ip_limit:
                    raise LimitReached("You have reached the daily search limit. Please try again tomorrow.")
                plan = "free"
            self._record_run(db, ip, "search", user.sub, global_limit)
            if plan == "free":
                db.execute(
                    "INSERT INTO free_searches (email_key, user_sub, used_at) VALUES (?, ?, ?)",
                    (email_key, user.sub, _now().isoformat()),
                )
        return plan

    def release_search(self, user: User, plan: str) -> None:
        """The upstream failed, so a free search that produced nothing is given back."""
        if plan == "free":
            with self._read() as db:
                db.execute(
                    "DELETE FROM free_searches WHERE user_sub = ? AND thread_id IS NULL", (user.sub,)
                )

    def add_search(self, thread_id: str, user: User, plan: str) -> None:
        with self._transaction() as db:
            db.execute(
                "INSERT INTO searches (thread_id, owner, plan, consented_at) VALUES (?, ?, ?, ?)",
                (thread_id, user.sub, plan, _now().isoformat()),
            )
            if plan == "free":
                db.execute(
                    "UPDATE free_searches SET thread_id = ? WHERE user_sub = ? AND thread_id IS NULL",
                    (thread_id, user.sub),
                )

    def reserve_follow_up(self, thread_id: str, user: User, ip: str, *, global_limit: int,
                          free_limit: int, paid_limit: int) -> None:
        with self._transaction() as db:
            row = db.execute(
                "SELECT follow_ups, plan FROM searches WHERE thread_id = ? AND owner = ?",
                (thread_id, user.sub),
            ).fetchone()
            if row is None:
                raise LimitReached("This search has reached its refinement limit. Please start a new search.")
            follow_ups, plan = row
            if self._subscribed(db, user.sub):
                if follow_ups >= paid_limit:
                    raise LimitReached(
                        "This search has reached its refinement limit. Please start a new search."
                    )
            elif plan == "paid":
                raise PaymentRequired("Your subscription has ended. Renew it to keep refining this list.")
            elif follow_ups >= free_limit:
                raise PaymentRequired(
                    "Your free search has used its refinements. Subscribe to keep refining."
                )
            self._record_run(db, ip, "follow_up", user.sub, global_limit)
            db.execute(
                "UPDATE searches SET follow_ups = follow_ups + 1 WHERE thread_id = ?", (thread_id,)
            )

    def owns(self, thread_id: str, user: User | None) -> bool:
        if user is None:
            return False
        with self._read() as db:
            row = db.execute(
                "SELECT owner FROM searches WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        return row is not None and row[0] == user.sub

    # Stripe

    def apply_stripe_update(self, update: StripeUpdate) -> bool:
        """Applies an event once. Returns False for an event already seen."""
        with self._transaction() as db:
            if db.execute(
                "SELECT 1 FROM stripe_events WHERE event_id = ?", (update.event_id,)
            ).fetchone():
                return False
            if update.customer_link:
                sub, customer = update.customer_link
                db.execute("UPDATE users SET stripe_customer_id = ? WHERE sub = ?", (customer, sub))
            if update.subscription:
                self._upsert_subscription(db, update.subscription, update.created)
            db.execute(
                "INSERT INTO stripe_events (event_id, type, received_at) VALUES (?, ?, ?)",
                (update.event_id, update.event_type, _now().isoformat()),
            )
        return True

    def _upsert_subscription(self, db: Any, record: SubscriptionRecord, created: int) -> None:
        sub = record.user_sub
        if sub is None:
            # Made outside our checkout, for example in the dashboard: match by customer.
            row = db.execute(
                "SELECT sub FROM users WHERE stripe_customer_id = ?", (record.customer_id,)
            ).fetchone()
            if row is None:
                _log.warning("stripe subscription %s has no known user", record.subscription_id)
                return
            sub = row[0]
        existing = db.execute(
            "SELECT stripe_subscription_id, status, event_created FROM subscriptions WHERE user_sub = ?",
            (sub,),
        ).fetchone()
        if existing:
            same = existing[0] == record.subscription_id
            # Stripe does not promise delivery order, so an older event never overwrites a newer one.
            if same and existing[2] > created:
                return
            # An old subscription ending must not cancel the new one that replaced it.
            if not same and existing[1] in ACTIVE_STATUSES and record.status not in ACTIVE_STATUSES:
                return
        db.execute(
            "INSERT INTO subscriptions (user_sub, stripe_customer_id, stripe_subscription_id, status, "
            "current_period_end, cancel_at_period_end, event_created) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (user_sub) DO UPDATE SET stripe_customer_id = excluded.stripe_customer_id, "
            "stripe_subscription_id = excluded.stripe_subscription_id, status = excluded.status, "
            "current_period_end = excluded.current_period_end, "
            "cancel_at_period_end = excluded.cancel_at_period_end, event_created = excluded.event_created",
            (sub, record.customer_id, record.subscription_id, record.status,
             record.current_period_end, int(record.cancel_at_period_end), created),
        )
        db.execute(
            "UPDATE users SET stripe_customer_id = COALESCE(stripe_customer_id, ?) WHERE sub = ?",
            (record.customer_id, sub),
        )
