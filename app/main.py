from __future__ import annotations

import asyncio
import logging
import random

from playwright.async_api import async_playwright

from app.config import Config
from app.notifier import Notifier
from app.site import SportsSite


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def sleep_with_jitter(base_seconds: int, jitter_seconds: int) -> None:
    jitter = random.randint(-jitter_seconds, jitter_seconds) if jitter_seconds else 0
    delay = max(10, base_seconds + jitter)
    logging.getLogger(__name__).info("Sleeping for %ss.", delay)
    await asyncio.sleep(delay)


async def run() -> None:
    setup_logging()
    cfg = Config.load()

    cfg.screenshot_dir.mkdir(exist_ok=True)
    cfg.user_data_dir.mkdir(exist_ok=True)
    notifier = Notifier(cfg)
    log = logging.getLogger(__name__)

    log.info("Starting bot. dry_run=%s target=%s", cfg.dry_run, cfg.target_url)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(cfg.user_data_dir),
            headless=False,
        )
        site = SportsSite(cfg, context)
        await site.open()

        consecutive_errors = 0

        try:
            await site.ensure_logged_in()

            while True:
                try:
                    found_or_booked = await site.check_and_book()
                    consecutive_errors = 0

                    if found_or_booked:
                        if cfg.dry_run:
                            notifier.send(
                                "Sports slot detected",
                                f"Dry-run found {cfg.sport} {cfg.day} {cfg.time_slot}.",
                            )
                        else:
                            notifier.send(
                                "Sports slot booking submitted",
                                f"{cfg.sport} {cfg.day} {cfg.time_slot}.",
                            )
                        break

                    await sleep_with_jitter(cfg.poll_interval_seconds, cfg.poll_jitter_seconds)

                except Exception as exc:
                    consecutive_errors += 1
                    log.exception("Polling error %s/%s: %s", consecutive_errors, cfg.max_consecutive_errors, exc)
                    await site.screenshot("error")

                    if consecutive_errors >= cfg.max_consecutive_errors:
                        notifier.send("Sports bot paused", f"Too many consecutive errors: {exc}")
                        break

                    await asyncio.sleep(cfg.error_backoff_seconds)
                    await site.ensure_logged_in()

        finally:
            try:
                await context.close()
            except Exception as exc:
                log.warning("Browser was already closed: %s", exc)


if __name__ == "__main__":
    asyncio.run(run())
