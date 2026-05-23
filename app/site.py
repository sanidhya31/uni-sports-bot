from __future__ import annotations

import logging
import re
import asyncio
import time
import unicodedata
from typing import Any

from playwright.async_api import BrowserContext, Locator, Page, TimeoutError as PlaywrightTimeoutError

from app.config import Config

log = logging.getLogger(__name__)

BOOKING_ACTION = "kurstermin_sst_buchen.php"
REQUIRED_BOOKING_FIELDS = (
    "kurs_id",
    "mitglied_id",
    "idkunde",
    "spring",
    "id_kurs",
    "mobile",
    "sperre24",
)

DAY_ALIASES = {
    "montag": ("montag", "mo", "mo."),
    "monday": ("montag", "mo", "mo.", "monday", "mon", "mon."),
    "dienstag": ("dienstag", "di", "di."),
    "tuesday": ("dienstag", "di", "di.", "tuesday", "tue", "tue."),
    "mittwoch": ("mittwoch", "mi", "mi."),
    "wednesday": ("mittwoch", "mi", "mi.", "wednesday", "wed", "wed."),
    "donnerstag": ("donnerstag", "do", "do."),
    "thursday": ("donnerstag", "do", "do.", "thursday", "thu", "thu."),
    "freitag": ("freitag", "fr", "fr."),
    "friday": ("freitag", "fr", "fr.", "friday", "fri", "fri."),
    "samstag": ("samstag", "sa", "sa."),
    "saturday": ("samstag", "sa", "sa.", "saturday", "sat", "sat."),
    "sonntag": ("sonntag", "so", "so."),
    "sunday": ("sonntag", "so", "so.", "sunday", "sun", "sun."),
}


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


def _normalized_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text.lower()).strip()


def _target_day_aliases(day: str) -> tuple[str, ...]:
    normalized = _normalized_text(day).rstrip(".")
    if not normalized:
        return ()
    aliases = DAY_ALIASES.get(normalized, ())
    return tuple(dict.fromkeys((normalized, f"{normalized}.", *aliases)))


def _text_contains_any(value: str, candidates: tuple[str, ...]) -> bool:
    normalized = _normalized_text(value)
    return any(candidate in normalized for candidate in candidates)


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
            log.warning("Login submitted, but login status is not confirmed yet.")
        else:
            log.info("Login confirmed.")

    async def check_and_book(self) -> bool:
        page = self._page()
        await self._navigate_to_booking_page()
        if not await self._wait_for_target_content():
            return False

        booking_form = await self._find_booking_form()
        if not booking_form:
            status = await self._target_slot_status()
            if status:
                log.info(
                    "Slot not available yet. target=%r day=%r time=%r button=%r action=%r rest=%r",
                    status.get("courseTitle", ""),
                    status.get("dayText", ""),
                    status.get("timeText", ""),
                    status.get("buttonText", ""),
                    status.get("formAction", ""),
                    status.get("restText", ""),
                )
            else:
                log.info("Slot not available yet. Target card was not found.")
            return False

        form_payload = await self._extract_booking_payload(booking_form)
        log.info(
            "Booking button detected. action=%s payload=%s",
            form_payload.get("action", BOOKING_ACTION),
            form_payload.get("fields", {}),
        )

        if self.cfg.dry_run:
            log.info("DRY_RUN=true, so no booking submit was performed.")
            return True

        submitted = await self._submit_booking_form(booking_form)
        if not submitted:
            log.error("Booking failed: could not submit the detected form.")
            return False

        confirm = await self._find_confirm_control()
        if confirm:
            log.info("Confirmation control found; clicking it.")
            await confirm.click()
            await page.wait_for_load_state("networkidle")

        booking_result = await self._booking_result()
        if booking_result is True:
            log.info("Booking successful.")
            return True
        if booking_result is False:
            log.error("Booking failed.")
            return False

        log.warning("Booking submitted, but success was not confirmed from the page text.")
        return True

    async def get_target_status(self) -> str:
        await self._navigate_to_booking_page()
        if not await self._wait_for_target_content():
            return "Target sport was not visible after loading KURSE."

        status = await self._target_slot_status()
        if not status:
            return f"Target not found: {self.cfg.sport} {self.cfg.day} {self.cfg.time_slot}"

        button = status.get("buttonText") or "none"
        action = status.get("formAction") or "none"
        rest = status.get("restText") or "Rest: unknown"
        availability = "open" if action == BOOKING_ACTION and button.lower() == "buchen" else "not open"
        return (
            f"{status.get('courseTitle', '')}\n"
            f"Day: {status.get('dayText', '')}\n"
            f"Time: {status.get('timeText', '')}\n"
            f"Status: {availability}\n"
            f"Button: {button}\n"
            f"Action: {action}\n"
            f"{rest}"
        )

    async def _navigate_to_booking_page(self) -> None:
        page = self._page()
        await page.goto(self.cfg.target_url, wait_until="load")

        if await self._page_contains_text(self.cfg.sport):
            return

        log.info("Target sport is not visible yet; opening KURSE area.")
        await self._open_courses_area()

    async def _open_courses_area(self) -> None:
        page = self._page()
        idkunde = await self._current_customer_id()
        if not idkunde:
            log.warning("Could not find idkunde on the account page, so KURSE cannot be opened directly.")
            return

        submitted = await page.evaluate(
            """({ idkunde, targetUrl }) => {
                const form = document.createElement('form');
                form.method = 'POST';
                form.action = targetUrl;
                form.target = '_self';
                form.style.display = 'none';

                const addHidden = (name, value) => {
                    const input = document.createElement('input');
                    input.type = 'hidden';
                    input.name = name;
                    input.value = value;
                    form.appendChild(input);
                };

                addHidden('idkunde', idkunde);
                addHidden('spring', 'kurse');

                document.body.appendChild(form);
                HTMLFormElement.prototype.submit.call(form);
                return true;
            }""",
            {"idkunde": idkunde, "targetUrl": self.cfg.target_url},
        )
        if submitted:
            log.info("Submitted direct KURSE POST with spring=kurse and idkunde=%s.", idkunde)
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except PlaywrightTimeoutError:
                log.debug("Network idle was not reached after submitting KURSE form.")

            if await self._wait_for_text_anywhere("Kurstermine", timeout=5_000):
                return
            if await self._wait_for_text_anywhere(self.cfg.sport, timeout=5_000):
                return

            log.warning("Submitted KURSE form, but the course schedule did not become visible.")

    async def _current_customer_id(self) -> str | None:
        page = self._page()
        idkunde_inputs = page.locator('input[name="idkunde"]')
        count = await idkunde_inputs.count()
        for index in range(count):
            value = await idkunde_inputs.nth(index).get_attribute("value")
            if value:
                return value
        return None

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

    async def _find_booking_form(self) -> Locator | None:
        page = self._page()
        if not self.cfg.sport or not self.cfg.day or not self.cfg.time_slot:
            raise ValueError("SPORT, DAY and TIME_SLOT must be configured before looking for a booking form.")

        click_predicate = self._clickable_text_predicate(self.cfg.book_button_texts)
        target_day_aliases = _target_day_aliases(self.cfg.day)
        if not target_day_aliases:
            raise ValueError(f"DAY={self.cfg.day!r} is not a supported weekday value.")

        try:
            forms = page.locator(
                "xpath=//form[contains(@action, "
                f"{_xpath_literal(BOOKING_ACTION)})]"
                f"[.//*[self::button or self::input][{click_predicate} and not(@disabled)]]"
            )
            count = await forms.count()
            for index in range(count):
                candidate = forms.nth(index)
                if not await self._form_has_required_fields(candidate):
                    continue

                context = await self._booking_candidate_context(candidate)
                if self._candidate_matches_target(context, target_day_aliases):
                    log.info(
                        "Matched target slot. day=%r course=%r card_id=%r",
                        context.get("dayText", ""),
                        context.get("courseTitle", ""),
                        context.get("cardId", ""),
                    )
                    return candidate

                log.debug(
                    "Rejected booking form candidate. day=%r course=%r text=%r",
                    context.get("dayText", ""),
                    context.get("courseTitle", ""),
                    context.get("cardText", "")[:200],
                )
        except PlaywrightTimeoutError:
            return None

        return None

    async def _target_slot_status(self) -> dict[str, str] | None:
        page = self._page()
        target_day_aliases = _target_day_aliases(self.cfg.day)
        if not target_day_aliases:
            return None

        cards = page.locator("xpath=//div[@id and .//span[@title]]")
        count = await cards.count()
        for index in range(count):
            card = cards.nth(index)
            context = await self._course_card_context(card)
            if self._candidate_matches_target(context, target_day_aliases):
                return context
        return None

    async def _course_card_context(self, card: Locator) -> dict[str, str]:
        return await card.evaluate(
            """(card) => {
                const weekdayPattern = /montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag/i;
                const text = (element) => element ? (element.innerText || element.textContent || '') : '';

                const findDayHeader = () => {
                    let cursor = card;
                    while (cursor && cursor !== document.body) {
                        let sibling = cursor.previousElementSibling;
                        while (sibling) {
                            const siblingText = text(sibling);
                            if (weekdayPattern.test(siblingText)) {
                                return siblingText;
                            }
                            sibling = sibling.previousElementSibling;
                        }
                        cursor = cursor.parentElement;
                    }
                    return '';
                };

                const titleElement = card.querySelector('span[title]');
                const form = card.querySelector('form');
                const submitter = card.querySelector('input[name="sub"], button[name="sub"]');
                const restMatch = text(card).match(/Rest:\\s*\\d+/i);
                const timeMatch = text(card).match(/\\|\\s*\\d{1,2}:\\d{2}\\s*-\\s*\\d{1,2}:\\d{2}\\s*Uhr/i);

                return {
                    cardId: card.id || '',
                    cardText: text(card),
                    courseTitle: titleElement ? (titleElement.getAttribute('title') || text(titleElement)) : '',
                    dayText: findDayHeader(),
                    timeText: timeMatch ? timeMatch[0].replace(/^\\|\\s*/, '') : '',
                    buttonText: submitter ? (submitter.value || text(submitter)) : '',
                    formAction: form ? (form.getAttribute('action') || '') : '',
                    restText: restMatch ? restMatch[0] : '',
                };
            }"""
        )

    async def _booking_candidate_context(self, form: Locator) -> dict[str, str]:
        return await form.evaluate(
            """(form) => {
                const weekdayPattern = /montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag/i;
                const text = (element) => element ? (element.innerText || element.textContent || '') : '';

                const findCourseCard = () => {
                    let node = form.parentElement;
                    while (node && node !== document.body) {
                        if (
                            node.tagName === 'DIV' &&
                            node.id &&
                            /^\\d+$/.test(node.id) &&
                            node.querySelector('table')
                        ) {
                            return node;
                        }
                        node = node.parentElement;
                    }
                    return form.closest('div[id]');
                };

                const findDayHeader = (card) => {
                    let cursor = card;
                    while (cursor && cursor !== document.body) {
                        let sibling = cursor.previousElementSibling;
                        while (sibling) {
                            const siblingText = text(sibling);
                            if (weekdayPattern.test(siblingText)) {
                                return siblingText;
                            }
                            sibling = sibling.previousElementSibling;
                        }
                        cursor = cursor.parentElement;
                    }
                    return '';
                };

                const card = findCourseCard();
                const titleElement = card ? card.querySelector('span[title]') : null;
                const titleText = titleElement ? (titleElement.getAttribute('title') || text(titleElement)) : '';

                return {
                    cardId: card ? card.id : '',
                    cardText: text(card),
                    courseTitle: titleText,
                    dayText: findDayHeader(card),
                };
            }"""
        )

    def _candidate_matches_target(self, context: dict[str, str], target_day_aliases: tuple[str, ...]) -> bool:
        card_text = context.get("cardText", "")
        course_title = context.get("courseTitle", "")
        day_text = context.get("dayText", "")

        sport_matches = _normalized_text(self.cfg.sport) in _normalized_text(f"{course_title} {card_text}")
        time_matches = _normalized_text(self.cfg.time_slot) in _normalized_text(card_text)
        day_matches = _text_contains_any(day_text, target_day_aliases)

        if not day_matches:
            normalized_title = _normalized_text(course_title)
            short_aliases = [alias.rstrip(".") for alias in target_day_aliases if len(alias.rstrip(".")) <= 3]
            day_matches = any(f"({alias})" in normalized_title for alias in short_aliases)

        return sport_matches and time_matches and day_matches

    async def _form_has_required_fields(self, form: Locator) -> bool:
        for field_name in REQUIRED_BOOKING_FIELDS:
            if await form.locator(f'input[name="{field_name}"]').count() == 0:
                log.debug("Booking form candidate is missing hidden field %s.", field_name)
                return False
        return True

    async def _extract_booking_payload(self, form: Locator) -> dict[str, Any]:
        return await form.evaluate(
            """(form, requiredNames) => {
                const fields = {};
                for (const name of requiredNames) {
                    const input = form.querySelector(`input[name="${name}"]`);
                    fields[name] = input ? input.value : null;
                }
                const submitter = form.querySelector('input[name="sub"], button[name="sub"]');
                if (submitter) {
                    fields[submitter.name || 'sub'] = submitter.value || submitter.textContent.trim();
                }
                return {
                    method: (form.method || 'GET').toUpperCase(),
                    action: form.getAttribute('action') || '',
                    fields,
                };
            }""",
            list(REQUIRED_BOOKING_FIELDS),
        )

    async def _submit_booking_form(self, form: Locator) -> bool:
        page = self._page()
        submitter = form.locator(
            "xpath=.//*[self::input or self::button]"
            f"[{self._clickable_text_predicate(self.cfg.book_button_texts)} and not(@disabled)]"
        ).first

        if await submitter.count() == 0:
            return False

        try:
            async with page.expect_response(lambda response: BOOKING_ACTION in response.url, timeout=15_000) as response_info:
                await submitter.click()
            response = await response_info.value
            log.info("Booking POST completed with HTTP %s.", response.status)
        except PlaywrightTimeoutError:
            log.warning("Booking submit did not expose a matching response before timeout; checking page state anyway.")

        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightTimeoutError:
            log.debug("Network idle was not reached after booking submit.")

        return True

    async def _wait_for_target_content(self) -> bool:
        page = self._page()
        try:
            await page.locator("body").wait_for(state="visible", timeout=15_000)
            if await self._wait_for_text_anywhere(self.cfg.sport, timeout=15_000):
                return True
            raise PlaywrightTimeoutError(f"Target text {self.cfg.sport!r} was not visible.")
        except PlaywrightTimeoutError:
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

    async def _booking_result(self) -> bool | None:
        text = await self._page_and_frame_text()
        normalized = text.lower()
        success_markers = [
            "erfolgreich",
            "gebucht",
            "buchung wurde",
            "meine buchungen",
        ]
        failure_markers = [
            "fehlgeschlagen",
            "nicht gebucht",
            "nicht moeglich",
            "ausgebucht",
            "fehler",
        ]

        if any(marker in normalized for marker in failure_markers):
            log.error("Booking failed. Page text starts with: %s", text[:500].replace("\n", " | "))
            return False
        if any(marker in normalized for marker in success_markers):
            return True
        return None

    async def _page_and_frame_text(self) -> str:
        page = self._page()
        chunks: list[str] = []
        try:
            chunks.append(await page.locator("body").inner_text(timeout=5_000))
        except PlaywrightTimeoutError:
            pass

        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                body = frame.locator("body")
                if await body.count() > 0:
                    chunks.append(await body.inner_text(timeout=2_000))
            except PlaywrightTimeoutError:
                continue

        return "\n".join(chunks)

    async def _page_contains_text(self, text: str) -> bool:
        return _normalized_text(text) in _normalized_text(await self._page_and_frame_text())

    async def _wait_for_text_anywhere(self, text: str, timeout: int) -> bool:
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            if await self._page_contains_text(text):
                return True
            await asyncio.sleep(0.25)
        return False

    def _clickable_text_predicate(self, texts: list[str]) -> str:
        checks = []
        if not texts:
            return "false()"

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
