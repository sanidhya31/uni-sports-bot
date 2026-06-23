"""Run the full multi-user app: control bots + booking engine together.

  ./venv/Scripts/python.exe -m app.app
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from app.bot import BotApp
from app.db import UserStore
from app.engine import Engine
from app.settings import Settings


async def run_app() -> None:
    settings = Settings.load()
    store = UserStore(settings.db_path)
    # Single shared worker => all sqlite access (bot + engine) on one thread.
    db_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db")

    bot = BotApp(settings, store, db_executor=db_executor)

    async def notify_admin(text: str) -> None:
        await bot.admin_api.send_message(settings.admin_user_id, text)

    engine = Engine(
        settings, store, db_executor,
        notify=bot.user_api.send_message,
        notify_admin=notify_admin,
    )

    logging.getLogger(__name__).info("Starting bots + engine.")
    try:
        await asyncio.gather(bot.run(), engine.run())
    finally:
        store.close()
        db_executor.shutdown(wait=False)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run_app())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
