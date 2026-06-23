"""SQLite-backed user store for the multi-user booking bot.

One row per user. Uni-portal passwords are encrypted at rest with Fernet
(symmetric AES); the key lives in the ``USER_DB_KEY`` env var, never in the DB.
The store is synchronous sqlite3 — async callers wrap it with ``asyncio.to_thread``.

Generate a key once:  ``./venv/Scripts/python.exe -m app.db genkey``
"""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "users.db"


class Status(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class User:
    id: int
    telegram_user_id: int
    telegram_username: str
    display_name: str
    uni_username: str
    sport: str
    day: str
    time_slot: str
    status: str
    enabled: bool
    on_hold: bool
    priority: int
    created_at: str
    approved_at: str | None
    booked_at: str | None

    @property
    def is_approved(self) -> bool:
        return self.status == Status.APPROVED.value

    @property
    def is_active(self) -> bool:
        """Approved, switched on, and not parked by the admin."""
        return self.is_approved and self.enabled and not self.on_hold

    @property
    def has_target(self) -> bool:
        return bool(self.sport and self.day and self.time_slot)


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id  INTEGER UNIQUE NOT NULL,
    telegram_username TEXT    DEFAULT '',
    display_name      TEXT    DEFAULT '',
    uni_username      TEXT    DEFAULT '',
    uni_password_enc  BLOB,
    sport             TEXT    DEFAULT '',
    day               TEXT    DEFAULT '',
    time_slot         TEXT    DEFAULT '',
    status            TEXT    DEFAULT 'pending',
    enabled           INTEGER DEFAULT 0,
    on_hold           INTEGER DEFAULT 0,
    priority          INTEGER DEFAULT 100,
    created_at        TEXT,
    approved_at       TEXT,
    booked_at         TEXT
);
"""

_USER_COLUMNS = (
    "id, telegram_user_id, telegram_username, display_name, uni_username, "
    "sport, day, time_slot, status, enabled, on_hold, priority, "
    "created_at, approved_at, booked_at"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_fernet() -> Fernet:
    key = os.getenv("USER_DB_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "USER_DB_KEY is not set. Generate one with "
            "`python -m app.db genkey` and put it in your .env."
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"USER_DB_KEY is not a valid Fernet key: {exc}") from exc


class UserStore:
    def __init__(self, path: Path | str = DEFAULT_DB_PATH, fernet: Fernet | None = None) -> None:
        self.path = Path(path)
        self._fernet = fernet or get_fernet()
        # check_same_thread=False: the bot serializes all DB access through a
        # single-worker executor (see BotApp._db), so cross-thread use is safe.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- credentials --------------------------------------------------------
    def _encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def get_password(self, telegram_user_id: int) -> str | None:
        row = self._conn.execute(
            "SELECT uni_password_enc FROM users WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        if not row or row["uni_password_enc"] is None:
            return None
        try:
            return self._fernet.decrypt(row["uni_password_enc"]).decode("utf-8")
        except InvalidToken:
            return None

    # -- registration / lifecycle ------------------------------------------
    def register_pending(
        self,
        telegram_user_id: int,
        telegram_username: str,
        display_name: str,
        uni_username: str,
        uni_password: str,
    ) -> None:
        """Create or update a user as PENDING admin approval."""
        self._conn.execute(
            """
            INSERT INTO users (
                telegram_user_id, telegram_username, display_name,
                uni_username, uni_password_enc, status, created_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                telegram_username = excluded.telegram_username,
                display_name      = excluded.display_name,
                uni_username      = excluded.uni_username,
                uni_password_enc  = excluded.uni_password_enc,
                status            = 'pending'
            """,
            (
                telegram_user_id,
                telegram_username,
                display_name,
                uni_username,
                self._encrypt(uni_password),
                _now(),
            ),
        )
        self._conn.commit()

    def set_status(self, telegram_user_id: int, status: Status) -> None:
        approved_at = _now() if status == Status.APPROVED else None
        self._conn.execute(
            "UPDATE users SET status = ?, approved_at = COALESCE(?, approved_at) "
            "WHERE telegram_user_id = ?",
            (status.value, approved_at, telegram_user_id),
        )
        self._conn.commit()

    def set_enabled(self, telegram_user_id: int, enabled: bool) -> None:
        self._conn.execute(
            "UPDATE users SET enabled = ? WHERE telegram_user_id = ?",
            (int(enabled), telegram_user_id),
        )
        self._conn.commit()

    def set_hold(self, telegram_user_id: int, on_hold: bool) -> None:
        self._conn.execute(
            "UPDATE users SET on_hold = ? WHERE telegram_user_id = ?",
            (int(on_hold), telegram_user_id),
        )
        self._conn.commit()

    def set_priority(self, telegram_user_id: int, priority: int) -> None:
        self._conn.execute(
            "UPDATE users SET priority = ? WHERE telegram_user_id = ?",
            (priority, telegram_user_id),
        )
        self._conn.commit()

    def set_target(self, telegram_user_id: int, sport: str, day: str, time_slot: str) -> None:
        self._conn.execute(
            "UPDATE users SET sport = ?, day = ?, time_slot = ? WHERE telegram_user_id = ?",
            (sport, day, time_slot, telegram_user_id),
        )
        self._conn.commit()

    def mark_booked(self, telegram_user_id: int) -> None:
        self._conn.execute(
            "UPDATE users SET booked_at = ?, enabled = 0 WHERE telegram_user_id = ?",
            (_now(), telegram_user_id),
        )
        self._conn.commit()

    def delete_user(self, telegram_user_id: int) -> None:
        self._conn.execute(
            "DELETE FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
        )
        self._conn.commit()

    # -- queries ------------------------------------------------------------
    def _row_to_user(self, row: sqlite3.Row) -> User:
        return User(
            id=row["id"],
            telegram_user_id=row["telegram_user_id"],
            telegram_username=row["telegram_username"] or "",
            display_name=row["display_name"] or "",
            uni_username=row["uni_username"] or "",
            sport=row["sport"] or "",
            day=row["day"] or "",
            time_slot=row["time_slot"] or "",
            status=row["status"],
            enabled=bool(row["enabled"]),
            on_hold=bool(row["on_hold"]),
            priority=row["priority"],
            created_at=row["created_at"],
            approved_at=row["approved_at"],
            booked_at=row["booked_at"],
        )

    def get_user(self, telegram_user_id: int) -> User | None:
        row = self._conn.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        return self._row_to_user(row) if row else None

    def get_user_by_uni(self, uni_username: str) -> User | None:
        row = self._conn.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE lower(uni_username) = lower(?)",
            (uni_username,),
        ).fetchone()
        return self._row_to_user(row) if row else None

    def update_credentials(self, telegram_user_id: int, uni_username: str, uni_password: str) -> None:
        """Update creds without touching approval status (for re-verify)."""
        self._conn.execute(
            "UPDATE users SET uni_username = ?, uni_password_enc = ? WHERE telegram_user_id = ?",
            (uni_username, self._encrypt(uni_password), telegram_user_id),
        )
        self._conn.commit()

    def list_users(self, status: Status | None = None) -> list[User]:
        if status is None:
            rows = self._conn.execute(
                f"SELECT {_USER_COLUMNS} FROM users ORDER BY priority, id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE status = ? ORDER BY priority, id",
                (status.value,),
            ).fetchall()
        return [self._row_to_user(r) for r in rows]

    def list_active(self) -> list[User]:
        return [u for u in self.list_users(Status.APPROVED) if u.is_active]


def _genkey_cli() -> int:
    print(Fernet.generate_key().decode())
    print("\nAdd this to your .env as:  USER_DB_KEY=<the line above>", file=sys.stderr)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "genkey":
        raise SystemExit(_genkey_cli())
    print("Usage: python -m app.db genkey", file=sys.stderr)
    raise SystemExit(1)
