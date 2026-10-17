"""SQLite (WAL) state. This is the only database. No separate DB server."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ringabot.crypto import decrypt, encrypt

_CALL_FIELDS = frozenset({"status", "started_at", "ended_at", "end_reason", "summary", "error"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS secrets (
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    nonce BLOB NOT NULL,
    ciphertext BLOB NOT NULL,
    PRIMARY KEY (tenant_id, name)
);
CREATE TABLE IF NOT EXISTS settings (
    tenant_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (tenant_id, key)
);
CREATE TABLE IF NOT EXISTS calls (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    destination TEXT NOT NULL,
    briefing TEXT NOT NULL,
    questions_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    end_reason TEXT,
    summary TEXT,
    error TEXT,
    max_seconds INTEGER NOT NULL,
    direction TEXT NOT NULL DEFAULT 'outbound'
);
CREATE TABLE IF NOT EXISTS transcript (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recordings (
    call_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_usage (
    tenant_id TEXT NOT NULL,
    day TEXT NOT NULL,
    calls INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, day)
);
CREATE INDEX IF NOT EXISTS calls_tenant_created ON calls (tenant_id, created_at);
"""


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_iso(moment: datetime | None = None) -> str:
    return (moment or utc_now()).isoformat()


class Store:
    def __init__(self, path: Path, master_key: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.master_key = master_key
        self._conn = sqlite3.connect(path, check_same_thread=True)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=2000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._ensure_direction()
        self._commit()

    def close(self) -> None:
        self._conn.close()

    def _ensure_direction(self) -> None:
        columns = {
            str(row["name"]) for row in self._conn.execute("PRAGMA table_info(calls)").fetchall()
        }
        if columns and "direction" not in columns:
            self._conn.execute(
                "ALTER TABLE calls ADD COLUMN direction TEXT NOT NULL DEFAULT 'outbound'"
            )

    def _commit(self) -> None:
        self._conn.commit()
        self._protect()

    def _protect(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.path}{suffix}")
            if candidate.exists():
                os.chmod(candidate, 0o600)

    def put_secret(self, tenant: str, name: str, plaintext: str) -> None:
        nonce, ciphertext = encrypt(
            self.master_key, plaintext.encode("utf-8"), f"{tenant}:{name}".encode()
        )
        self._conn.execute(
            """
            INSERT INTO secrets (tenant_id, name, nonce, ciphertext)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(tenant_id, name) DO UPDATE SET nonce=excluded.nonce,
                ciphertext=excluded.ciphertext
            """,
            (tenant, name, nonce, ciphertext),
        )
        self._commit()

    def get_secret(self, tenant: str, name: str) -> str | None:
        row = self._conn.execute(
            "SELECT nonce, ciphertext FROM secrets WHERE tenant_id=? AND name=?",
            (tenant, name),
        ).fetchone()
        if row is None:
            return None
        return decrypt(
            self.master_key, row["nonce"], row["ciphertext"], f"{tenant}:{name}".encode()
        ).decode("utf-8")

    def delete_secrets(self, tenant: str) -> None:
        self._conn.execute("DELETE FROM secrets WHERE tenant_id=?", (tenant,))
        self._commit()

    def put_setting(self, tenant: str, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO settings (tenant_id, key, value) VALUES (?, ?, ?)
            ON CONFLICT(tenant_id, key) DO UPDATE SET value=excluded.value
            """,
            (tenant, key, value),
        )
        self._commit()

    def get_setting(self, tenant: str, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM settings WHERE tenant_id=? AND key=?",
            (tenant, key),
        ).fetchone()
        return None if row is None else str(row["value"])

    def delete_setting(self, tenant: str, key: str) -> None:
        self._conn.execute("DELETE FROM settings WHERE tenant_id=? AND key=?", (tenant, key))
        self._commit()

    def clear_settings(self, tenant: str) -> None:
        self._conn.execute("DELETE FROM settings WHERE tenant_id=?", (tenant,))
        self._commit()

    def insert_call(
        self,
        *,
        call_id: str,
        tenant: str,
        destination: str,
        briefing: str,
        questions: list[str],
        max_seconds: int,
        direction: str = "outbound",
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO calls (
                id, tenant_id, destination, briefing, questions_json, status,
                created_at, max_seconds, direction
            ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)
            """,
            (
                call_id,
                tenant,
                destination,
                briefing,
                json.dumps(questions),
                utc_iso(),
                max_seconds,
                direction if direction in {"outbound", "inbound"} else "outbound",
            ),
        )
        self._commit()

    def update_call(self, call_id: str, **fields: str | None) -> None:
        if not fields:
            return
        unknown = set(fields) - _CALL_FIELDS
        if unknown:
            raise ValueError("unknown call field")
        columns = ", ".join(f"{key}=?" for key in fields)
        self._conn.execute(
            f"UPDATE calls SET {columns} WHERE id=?",
            (*fields.values(), call_id),
        )
        self._commit()

    def get_call(self, tenant: str, call_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM calls WHERE id=? AND tenant_id=?",
            (call_id, tenant),
        ).fetchone()
        return None if row is None else _call_dict(row)

    def list_calls(self, tenant: str, *, limit: int, since: str | None) -> list[dict]:
        if since:
            rows = self._conn.execute(
                """
                SELECT * FROM calls
                WHERE tenant_id=? AND created_at>=?
                ORDER BY created_at DESC LIMIT ?
                """,
                (tenant, since, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM calls WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
                (tenant, limit),
            ).fetchall()
        return [_call_dict(row) for row in rows]

    def add_transcript(self, call_id: str, role: str, text: str) -> None:
        cleaned = text.strip()[:4000]
        if not cleaned:
            return
        last = self._conn.execute(
            "SELECT role, text, seq FROM transcript WHERE call_id=? ORDER BY seq DESC LIMIT 1",
            (call_id,),
        ).fetchone()
        if last is not None and last["role"] == role and last["text"] == cleaned:
            return
        seq = 1 if last is None else int(last["seq"]) + 1
        self._conn.execute(
            """
            INSERT INTO transcript (call_id, seq, role, text, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (call_id, seq, role, cleaned, utc_iso()),
        )
        self._commit()

    def transcript(self, call_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT role, text, created_at FROM transcript WHERE call_id=? ORDER BY seq",
            (call_id,),
        ).fetchall()
        return [{"role": row["role"], "text": row["text"], "at": row["created_at"]} for row in rows]

    def recent_call_ids(self, tenant: str, limit: int = 20) -> list[str]:
        rows = self._conn.execute(
            "SELECT id FROM calls WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
            (tenant, limit),
        ).fetchall()
        return [str(row["id"]) for row in rows]

    def add_recording(self, tenant: str, call_id: str, path: str) -> None:
        self._conn.execute(
            """
            INSERT INTO recordings (call_id, tenant_id, path, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(call_id) DO UPDATE SET path=excluded.path
            """,
            (call_id, tenant, path, utc_iso()),
        )
        self._commit()

    def list_recordings(self, tenant: str) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT call_id, path, created_at FROM recordings
            WHERE tenant_id=? ORDER BY created_at DESC
            """,
            (tenant,),
        ).fetchall()
        return [
            {"call_id": row["call_id"], "path": row["path"], "created_at": row["created_at"]}
            for row in rows
        ]

    def calls_today(self, tenant: str, day: str | None = None) -> int:
        stamp = day or utc_now().date().isoformat()
        row = self._conn.execute(
            "SELECT calls FROM daily_usage WHERE tenant_id=? AND day=?",
            (tenant, stamp),
        ).fetchone()
        return 0 if row is None else int(row["calls"])

    def increment_calls_today(self, tenant: str) -> int:
        stamp = utc_now().date().isoformat()
        self._conn.execute(
            """
            INSERT INTO daily_usage (tenant_id, day, calls) VALUES (?, ?, 1)
            ON CONFLICT(tenant_id, day) DO UPDATE SET calls=calls+1
            """,
            (tenant, stamp),
        )
        self._commit()
        return self.calls_today(tenant, stamp)

    def active_call_count(self, tenant: str | None = None) -> int:
        if tenant is None:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS n FROM calls
                WHERE status IN ('queued', 'dialing', 'in_progress', 'wrapping_up')
                """
            ).fetchone()
        else:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS n FROM calls
                WHERE tenant_id=? AND status IN ('queued', 'dialing', 'in_progress', 'wrapping_up')
                """,
                (tenant,),
            ).fetchone()
        return int(row["n"])

    def active_call_ids(self, tenant: str) -> list[str]:
        rows = self._conn.execute(
            """
            SELECT id FROM calls
            WHERE tenant_id=? AND status IN ('queued', 'dialing', 'in_progress', 'wrapping_up')
            """,
            (tenant,),
        ).fetchall()
        return [str(row["id"]) for row in rows]

    def delete_account(self, tenant: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT path FROM recordings WHERE tenant_id=?",
            (tenant,),
        ).fetchall()
        paths = [str(row["path"]) for row in rows]
        call_ids = [
            row["id"]
            for row in self._conn.execute(
                "SELECT id FROM calls WHERE tenant_id=?", (tenant,)
            ).fetchall()
        ]
        if call_ids:
            marks = ",".join("?" for _ in call_ids)
            self._conn.execute(f"DELETE FROM transcript WHERE call_id IN ({marks})", call_ids)
        self._conn.execute("DELETE FROM recordings WHERE tenant_id=?", (tenant,))
        self._conn.execute("DELETE FROM calls WHERE tenant_id=?", (tenant,))
        self._conn.execute("DELETE FROM secrets WHERE tenant_id=?", (tenant,))
        self._conn.execute("DELETE FROM settings WHERE tenant_id=?", (tenant,))
        self._conn.execute("DELETE FROM daily_usage WHERE tenant_id=?", (tenant,))
        self.put_setting(tenant, "credentials_revoked", "1")
        return paths

    def expired_recordings(self, retention_days: int, now: datetime | None = None) -> list[dict]:
        cutoff = (now or utc_now()) - timedelta(days=retention_days)
        rows = self._conn.execute("SELECT call_id, path, created_at FROM recordings").fetchall()
        expired = []
        for row in rows:
            created = datetime.fromisoformat(row["created_at"])
            if created <= cutoff:
                expired.append(
                    {
                        "call_id": row["call_id"],
                        "path": row["path"],
                        "created_at": row["created_at"],
                    }
                )
        return expired

    def delete_recording_row(self, call_id: str) -> None:
        self._conn.execute("DELETE FROM recordings WHERE call_id=?", (call_id,))
        self._commit()

    def delete_transcripts_before(self, cutoff: datetime) -> int:
        stamp = cutoff.isoformat()
        cursor = self._conn.execute("DELETE FROM transcript WHERE created_at<=?", (stamp,))
        self._commit()
        return cursor.rowcount

    def fail_orphans(self) -> None:
        self._conn.execute(
            """
            UPDATE calls SET status='failed', error='interrupted', ended_at=?
            WHERE status IN ('queued', 'dialing', 'in_progress', 'wrapping_up')
            """,
            (utc_iso(),),
        )
        self._commit()


def _call_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "destination": row["destination"],
        "briefing": row["briefing"],
        "questions": json.loads(row["questions_json"]),
        "status": row["status"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "end_reason": row["end_reason"],
        "summary": row["summary"],
        "error": row["error"],
        "max_seconds": row["max_seconds"],
        "direction": row["direction"] if "direction" in row.keys() else "outbound",
    }
