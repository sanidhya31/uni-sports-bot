from __future__ import annotations

import logging
from datetime import datetime

from playwright.async_api import BrowserContext, Locator, Page, TimeoutError as PlaywrightTimeoutError

from app.config import Config

log = logging.getLogger(__name__)


def _xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    return "concat(" + ', "\"\'\"", '.join(f"'{part}'" for part in parts) + ")"


def _case_insensitive_contains_expr(text: str) -> str:
    upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    lower = "abcdefghijklmnopqrstuvwxyz"
    return (
        "contains(translate(normalize-space(.), "
        f"{_xpath_literal(upper)}, {_xpath_literal(lower)}), "
        f"{_xpath_literal(text.lower())})"
    )


class SportsSite:
    def __init__(self, cfg: Config, context: BrowserContext) -> None:
        self.cfg = cfg
        self.context = context
        self.page: Page | None = None

    async def open(self) -> None:
        self.page = await self.context.new_page()
        self.page.set_default_timeout(15_000)

    async def ensure_logged_in(self) -> None:
        page = self._page()
        await page.goto(self.cfg.target_url, wait_until="domcontentloaded")

        if await self._looks_logged_in():
            log.info("Session already logged in.")
            return

        log.info("Logging in.")
        await page.goto(self.cfg.login_url, wait_until="domcontentloaded")
        await page.locator(self.cfg.email_selector).fill(self.cfg.username)
        await page.locator(self.cfg.password_selector).fill(self.cfg.password)
        await page.locator(self.cfg.login_submit_selector).click()
        await page.wait_for_load_state("networkidle")

        if not await self._looks_logged_in():
            await self.screenshot("login-check-failed")
            log.warning("Login submitted, but login status is not confirmed yet.")
        else:
            log.info("Login confirmed.")

    async def check_and_book(self) -> bool:
        page = self._page()
        await page.goto(self.cfg.target_url, wait_until="load")
        if not await self._wait_for_target_content():
            return False

        slot_control = await self._find_booking_control()
        if not slot_control:
            log.info("Target slot not available yet.")
            await self.screenshot("slot-not-found")
            return False

        log.info("Target slot appears available.")
        await self.screenshot("slot-found")

        if self.cfg.dry_run:
            log.info("DRY_RUN=true, so no booking click was performed.")
            return True

        await slot_control.click()
        await page.wait_for_load_state("networkidle")

        confirm = await self._find_confirm_control()
        if confirm:
            log.info("Confirmation control found; clicking it.")
            await confirm.click()
            await page.wait_for_load_state("networkidle")

        await self.screenshot("booking-submitted")
        log.info("Booking flow submitted.")
        return True

    async def screenshot(self, label: str) -> None:
        page = self._page()
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.cfg.screenshot_dir / f"{timestamp}-{label}.png"
        await page.screenshot(path=str(path), full_page=True)
        log.info("Screenshot saved: %s", path)

    async def _looks_logged_in(self) -> bool:
        page = self._page()
        if self.cfg.logged_in_text:
            if await page.get_by_text(self.cfg.logged_in_text, exact=False).count() > 0:
                return True

        account_markers = ["Hallo", "Meine Buchungen"]
        for marker in account_markers:
            if await page.get_by_text(marker, exact=False).count() > 0:
                return True

        email_fields = await page.locator(self.cfg.email_selector).count()
        password_fields = await page.locator(self.cfg.password_selector).count()
        return email_fields == 0 and password_fields == 0

    async def _find_booking_control(self) -> Locator | None:
        page = self._page()
        required_parts = [self.cfg.sport, self.cfg.time_slot]

        container_predicate = " and ".join(_case_insensitive_contains_expr(part) for part in required_parts)
        click_predicate = self._clickable_text_predicate(self.cfg.book_button_texts)
        container_xpath = (
            "xpath=(//*[self::tr or self::div or self::section or self::article]"
            f"[{container_predicate}])[last()]"
        )

        try:
            containers = page.locator(container_xpath)
            if await containers.count() == 0:
                return None

            container = containers.first
            control = container.locator(
                f"xpath=.//*[self::a or self::button or self::input][{click_predicate} and not(@disabled)]"
            )
            if await control.count() > 0:
                return control.first

            # Some UniSport pages render the button as a sibling inside a broader card.
            sibling_control = container.locator(
                f"xpath=following-sibling::*//*[self::a or self::button or self::input]"
                f"[{click_predicate} and not(@disabled)]"
            )
            if await sibling_control.count() > 0:
                return sibling_control.first

            broad_control = page.locator(
                "xpath=(//*[self::a or self::button or self::input]"
                f"[{click_predicate} and not(@disabled)]"
                f"[ancestor::*[{container_predicate}]])[1]"
            )
            if await broad_control.count() > 0:
                return broad_control.first
        except PlaywrightTimeoutError:
            return None

        return None

    async def _wait_for_target_content(self) -> bool:
        page = self._page()
        try:
            await page.locator("body").wait_for(state="visible", timeout=15_000)
            await page.get_by_text(self.cfg.sport, exact=False).first.wait_for(timeout=15_000)
            return True
        except PlaywrightTimeoutError:
            await self.screenshot("target-page-not-ready")
            log.warning(
                "Target page did not show %r. current_url=%s title=%r",
                self.cfg.sport,
                page.url,
                await page.title(),
            )
            body_text = await page.locator("body").inner_text(timeout=5_000)
            log.warning("Visible page text starts with: %s", body_text[:500].replace("\n", " | "))
            return False

    async def _find_confirm_control(self) -> Locator | None:
        page = self._page()
        xpath = (
            "xpath=(//*[self::a or self::button or self::input]"
            f"[{self._clickable_text_predicate(self.cfg.confirm_button_texts)} and not(@disabled)])[1]"
        )
        locator = page.locator(xpath)
        if await locator.count() > 0:
            return locator.first
        return None

    def _clickable_text_predicate(self, texts: list[str]) -> str:
        checks = []
        for text in texts:
            text_lit = _xpath_literal(text.lower())
            checks.append(_case_insensitive_contains_expr(text))
            checks.append(
                "contains(translate(normalize-space(@value), "
                f"{_xpath_literal('ABCDEFGHIJKLMNOPQRSTUVWXYZ')}, "
                f"{_xpath_literal('abcdefghijklmnopqrstuvwxyz')}), {text_lit})"
            )
            checks.append(
                "contains(translate(normalize-space(@title), "
                f"{_xpath_literal('ABCDEFGHIJKLMNOPQRSTUVWXYZ')}, "
                f"{_xpath_literal('abcdefghijklmnopqrstuvwxyz')}), {text_lit})"
            )
        return "(" + " or ".join(checks) + ")"

    def _page(self) -> Page:
        if self.page is None:
            raise RuntimeError("SportsSite.open() must be called first.")
        return self.page
