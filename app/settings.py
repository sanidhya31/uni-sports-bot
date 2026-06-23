"""Global settings for the multi-user booking app (read once from .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=True)


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw.lstrip("-").isdigit() else default


@dataclass
class Settings:
    bot_token: str          # public user-facing bot
    admin_bot_token: str    # private admin bot (approvals, hold/release, etc.)
    admin_user_id: int
    db_path: Path
    portal_base_url: str

    # engine knobs (used from Phase 3 on)
    max_concurrent_users: int
    poll_interval_seconds: int
    poll_jitter_seconds: int

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            admin_bot_token=os.getenv("ADMIN_BOT_TOKEN", "").strip(),
            # ADMIN_USER_ID preferred; fall back to the legacy single-user var.
            admin_user_id=_int("ADMIN_USER_ID", _int("TELEGRAM_ALLOWED_USER_ID", 0)),
            db_path=Path(os.getenv("USER_DB_PATH", str(PROJECT_ROOT / "users.db"))),
            portal_base_url=os.getenv("PORTAL_BASE_URL", "https://ahs.uni-trier.de"),
            max_concurrent_users=_int("MAX_CONCURRENT_USERS", 5),
            poll_interval_seconds=_int("POLL_INTERVAL_SECONDS", 2),
            poll_jitter_seconds=_int("POLL_JITTER_SECONDS", 1),
        )
