from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=True)


def _project_path(env_name: str, default: str) -> Path:
    path = Path(os.getenv(env_name, default))
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


@dataclass
class Config:
    login_url: str
    target_url: str

    username: str
    password: str

    sport: str
    day: str
    time_slot: str

    dry_run: bool
    headless: bool

    poll_interval_seconds: int
    poll_jitter_seconds: int

    error_backoff_seconds: int
    max_consecutive_errors: int

    email_selector: str
    password_selector: str
    login_submit_selector: str

    book_button_texts: list[str]
    confirm_button_texts: list[str]

    logged_in_text: str

    telegram_token: str
    telegram_chat_id: str
    telegram_bot_token: str
    telegram_allowed_user_id: int
    notify_email: str

    user_data_dir: Path
    screenshot_dir: Path
    runtime_config_path: Path

    @classmethod
    def load(cls) -> "Config":
        return cls(
            login_url=os.getenv("LOGIN_URL", ""),
            target_url=os.getenv("TARGET_URL", ""),
            username=os.getenv("UNI_USERNAME") or os.getenv("USERNAME", ""),
            password=os.getenv("PASSWORD", ""),

            sport=os.getenv("SPORT", ""),
            day=os.getenv("DAY", ""),
            time_slot=os.getenv("TIME_SLOT", ""),

            dry_run=os.getenv("DRY_RUN", "true").lower() == "true",
            headless=os.getenv("HEADLESS", "false").lower() == "true",

            poll_interval_seconds=int(
                os.getenv("POLL_INTERVAL_SECONDS", "30")
            ),

            poll_jitter_seconds=int(
                os.getenv("POLL_JITTER_SECONDS", "10")
            ),

            error_backoff_seconds=int(
                os.getenv("ERROR_BACKOFF_SECONDS", "60")
            ),

            max_consecutive_errors=int(
                os.getenv("MAX_CONSECUTIVE_ERRORS", "5")
            ),

            email_selector=os.getenv(
                "EMAIL_SELECTOR",
                ""
            ),

            password_selector=os.getenv(
                "PASSWORD_SELECTOR",
                ""
            ),

            login_submit_selector=os.getenv(
                "LOGIN_SUBMIT_SELECTOR",
                ""
            ),

            book_button_texts=[
                x.strip()
                for x in os.getenv(
                    "BOOK_BUTTON_TEXTS",
                    "Buchen,Anmelden,Book,Register"
                ).split(",")
                if x.strip()
            ],

            confirm_button_texts=[
                x.strip()
                for x in os.getenv(
                    "CONFIRM_BUTTON_TEXTS",
                    "Bestaetigen,Best,Confirm,Yes"
                ).split(",")
                if x.strip()
            ],

            logged_in_text=os.getenv(
                "LOGGED_IN_TEXT",
                ""
            ),

            telegram_token=os.getenv(
                "TELEGRAM_TOKEN",
                ""
            ),

            telegram_chat_id=os.getenv(
                "TELEGRAM_CHAT_ID",
                ""
            ),

            telegram_bot_token=os.getenv(
                "TELEGRAM_BOT_TOKEN",
                ""
            ),

            telegram_allowed_user_id=int(
                os.getenv("TELEGRAM_ALLOWED_USER_ID", "0") or "0"
            ),

            notify_email=os.getenv(
                "NOTIFY_EMAIL",
                ""
            ),

            user_data_dir=_project_path("USER_DATA_DIR", "user_data"),

            screenshot_dir=_project_path("SCREENSHOT_DIR", "screenshots"),

            runtime_config_path=_project_path("RUNTIME_CONFIG_PATH", "config.json"),
        )
