from __future__ import annotations

import asyncio
import logging
import random

from playwright.async_api import async_playwright

from app.config import Config
from app.notifier import Notifier
from app.runtime_config import RuntimeConfig
from app.site import SportsSite
from app.telegram_control import TelegramControlBot


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def sleep_with_jitter(base_seconds: int, jitter_seconds: int) -> None:
    jitter = random.randint(-jitter_seconds, jitter_seconds) if jitter_seconds else 0
    delay = max(1, base_seconds + jitter)
    logging.getLogger(__name__).info("Sleeping for %ss.", delay)
    await asyncio.sleep(delay)


async def run() -> None:
    setup_logging()
    cfg = Config.load()

    cfg.screenshot_dir.mkdir(exist_ok=True)
    cfg.user_data_dir.mkdir(exist_ok=True)
    runtime = RuntimeConfig.load(cfg.runtime_config_path, cfg)
    runtime.apply_to(cfg)
    notifier = Notifier(
        telegram_token=cfg.telegram_token,
        telegram_chat_id=cfg.telegram_chat_id,
        email=cfg.notify_email,
    )
    log = logging.getLogger(__name__)

    log.info("Starting bot. dry_run=%s target=%s", cfg.dry_run, cfg.target_url)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(cfg.user_data_dir),
            headless=cfg.headless,
        )
        site = SportsSite(cfg, context)
        await site.open()
        site_lock = asyncio.Lock()

        async def current_status() -> str:
            runtime_now = RuntimeConfig.load(cfg.runtime_config_path, cfg)
            runtime_now.apply_to(cfg)
            async with site_lock:
                return await site.get_target_status()

        control_bot = TelegramControlBot(cfg, current_status)
        control_task = asyncio.create_task(control_bot.run())

        consecutive_errors = 0

        try:
            async with site_lock:
                await site.ensure_logged_in()

            while True:
                try:
                    runtime = RuntimeConfig.load(cfg.runtime_config_path, cfg)
                    runtime.apply_to(cfg)

                    if not runtime.enabled:
                        log.info("Bot is paused. Use /startbot to resume.")
                        await sleep_with_jitter(cfg.poll_interval_seconds, cfg.poll_jitter_seconds)
                        continue

                    async with site_lock:
                        found_or_booked = await site.check_and_book()
                    consecutive_errors = 0

                    if found_or_booked:
                        if cfg.dry_run:
                            notifier.send(
                                "Sports slot detected",
                                f"Dry-run found {cfg.sport} {cfg.day} {cfg.time_slot}.",
                            )
                            if cfg.telegram_bot_token and cfg.telegram_allowed_user_id:
                                await control_bot.send_message(
                                    cfg.telegram_allowed_user_id,
                                    f"Dry-run found: {cfg.sport} {cfg.day} {cfg.time_slot}",
                                )
                        else:
                            notifier.send(
                                "Sports slot booking submitted",
                                f"{cfg.sport} {cfg.day} {cfg.time_slot}.",
                            )
                            if cfg.telegram_bot_token and cfg.telegram_allowed_user_id:
                                await control_bot.send_message(
                                    cfg.telegram_allowed_user_id,
                                    f"Booking submitted: {cfg.sport} {cfg.day} {cfg.time_slot}",
                                )
                            break

                    await sleep_with_jitter(cfg.poll_interval_seconds, cfg.poll_jitter_seconds)

                except Exception as exc:
                    consecutive_errors += 1
                    log.exception("Polling error %s/%s: %s", consecutive_errors, cfg.max_consecutive_errors, exc)
                    async with site_lock:
                        await site.screenshot("error")

                    if consecutive_errors >= cfg.max_consecutive_errors:
                        notifier.send("Sports bot paused", f"Too many consecutive errors: {exc}")
                        break

                    await asyncio.sleep(cfg.error_backoff_seconds)
                    async with site_lock:
                        await site.ensure_logged_in()

        finally:
            control_task.cancel()
            try:
                await control_task
            except asyncio.CancelledError:
                pass
            try:
                await context.close()
            except Exception as exc:
                log.warning("Browser was already closed: %s", exc)


if __name__ == "__main__":
    asyncio.run(run())
