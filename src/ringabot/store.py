"""Accounts and phone numbers. The sqlite file is the whole database."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

PENDING = "pending_payment"
ACTIVE = "active"
CANCELLED = "cancelled"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Admin:
    """Caller authenticated with the admin API_TOKEN. Not an account row."""


@dataclass(frozen=True)
class Account:
    id: str
    email: str | None
    stripe_customer_id: str | None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: str):
        parent = Path(path).parent
        if parent and str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                api_token_hash TEXT NOT NULL UNIQUE,
                email TEXT,
                stripe_customer_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS numbers (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL REFERENCES accounts(id),
                e164 TEXT,
                status TEXT NOT NULL CHECK (status IN ('pending_payment', 'active', 'cancelled')),
                stripe_subscription_id TEXT,
                currency TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS numbers_account ON numbers(account_id);
            CREATE INDEX IF NOT EXISTS numbers_sub ON numbers(stripe_subscription_id);
            """
        )
        self._conn.commit()

    def create_account(self) -> tuple[Account, str]:
        token = secrets.token_urlsafe(32)
        account_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO accounts (id, api_token_hash, email, stripe_customer_id, created_at) "
                "VALUES (?, ?, NULL, NULL, ?)",
                (account_id, hash_token(token), _now()),
            )
            self._conn.commit()
        return Account(account_id, None, None), token

    def account_by_token(self, token: str) -> Account | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, email, stripe_customer_id FROM accounts WHERE api_token_hash = ?",
                (hash_token(token),),
            ).fetchone()
        if row is None:
            return None
        return Account(row["id"], row["email"], row["stripe_customer_id"])

    def create_number(self, account_id: str, currency: str) -> str:
        number_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO numbers "
                "(id, account_id, e164, status, stripe_subscription_id, currency, created_at) "
                "VALUES (?, ?, NULL, ?, NULL, ?, ?)",
                (number_id, account_id, PENDING, currency, _now()),
            )
            self._conn.commit()
        return number_id

    def delete_number(self, number_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM numbers WHERE id = ? AND status = ?",
                (number_id, PENDING),
            )
            self._conn.commit()

    def has_active(self, account_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM numbers WHERE account_id = ? AND status = ? LIMIT 1",
                (account_id, ACTIVE),
            ).fetchone()
        return row is not None

    def activate(
        self,
        number_id: str,
        subscription_id: str,
        customer_id: str | None,
        email: str | None,
    ) -> tuple[str, str] | None:
        """Move pending_payment to active. Return (account_id, currency) only on that transition."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE numbers SET status = ?, stripe_subscription_id = ? "
                "WHERE id = ? AND status = ?",
                (ACTIVE, subscription_id, number_id, PENDING),
            )
            if cur.rowcount != 1:
                self._conn.rollback()
                return None
            row = self._conn.execute(
                "SELECT account_id, currency FROM numbers WHERE id = ?",
                (number_id,),
            ).fetchone()
            if customer_id or email:
                self._conn.execute(
                    "UPDATE accounts SET "
                    "stripe_customer_id = COALESCE(?, stripe_customer_id), "
                    "email = COALESCE(?, email) "
                    "WHERE id = ?",
                    (customer_id, email, row["account_id"]),
                )
            self._conn.commit()
        return row["account_id"], row["currency"]

    def cancel_subscription(self, subscription_id: str) -> bool:
        """Mark the number cancelled. A repeat finds nothing left to change."""
        if not subscription_id:
            return False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE numbers SET status = ? WHERE stripe_subscription_id = ? AND status != ?",
                (CANCELLED, subscription_id, CANCELLED),
            )
            self._conn.commit()
        return cur.rowcount == 1
