"""Run the multi-user control bot (Phase 1: registration + admin approval).

  ./venv/Scripts/python.exe -m scripts.run_bot
"""

from __future__ import annotations

import asyncio
import logging
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.bot import BotApp
from app.db import UserStore
from app.settings import Settings


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.load()
    store = UserStore(settings.db_path)
    bot = BotApp(settings, store)
    try:
        await bot.run()
    finally:
        store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
