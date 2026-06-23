"""Pure-HTTP client for the Uni Trier uniSPORT portal.

Every portal action is a plain form POST with a session cookie, so we never need
a browser in the steady state:

* ``login``     -> POST ``login_neu.php`` with ``mail`` / ``passwort`` / ``sub``
* ``schedule``  -> GET (or POST ``spring=kurse``) ``index_account.php``
* ``book``      -> POST the slot's form action with its captured hidden fields

One ``PortalClient`` owns one ``httpx.AsyncClient`` (one cookie jar), so N users
are N independent lightweight clients sharing nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

from app.slots import Availability, Slot, parse_schedule

log = logging.getLogger(__name__)

BASE_URL = "https://ahs.uni-trier.de"
LOGIN_PATH = "/login_neu.php"
ACCOUNT_PATH = "/index_account.php"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Text that only appears once a session is authenticated.
LOGGED_IN_MARKERS = ("logout", "abmelden", "meine buchungen", "kurstermine der")
# Text on the schedule page proving the course list rendered.
SCHEDULE_MARKERS = ("kurstermine der", "werktage")


class PortalError(RuntimeError):
    pass


class LoginError(PortalError):
    pass


@dataclass
class BookingResult:
    ok: bool
    detail: str
    status_code: int | None = None
    permanent: bool = False   # rejection that won't clear by retrying (e.g. 24h rule)
    message: str = ""         # short human-readable snippet from the portal


class PortalClient:
    def __init__(
        self,
        username: str,
        password: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = 15.0,
    ) -> None:
        self.username = username
        self.password = password
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        )
        self._idkunde: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "PortalClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- auth ---------------------------------------------------------------
    async def login(self) -> None:
        """Authenticate; raises LoginError if the session is not confirmed."""
        resp = await self._client.post(
            LOGIN_PATH,
            data={"mail": self.username, "passwort": self.password, "sub": "EINLOGGEN"},
        )
        resp.raise_for_status()
        body = resp.text
        if not self._looks_logged_in(body):
            # The account page itself is the surest signal.
            account = await self._client.get(ACCOUNT_PATH)
            body = account.text
            if not self._looks_logged_in(body):
                raise LoginError("Login failed: no authenticated markers after submit.")
        self._idkunde = _find_idkunde(body)
        log.info("Login confirmed for %s (idkunde=%s).", self.username, self._idkunde)

    async def is_logged_in(self) -> bool:
        resp = await self._client.get(ACCOUNT_PATH)
        return self._looks_logged_in(resp.text)

    # -- schedule -----------------------------------------------------------
    async def fetch_schedule_html(self) -> str:
        """Return the HTML of the account page with the 5-day course schedule."""
        resp = await self._client.get(ACCOUNT_PATH)
        resp.raise_for_status()
        html = resp.text
        if not _contains_any(html, SCHEDULE_MARKERS):
            # Some accounts need an explicit POST to expand the KURSE area.
            data = {"spring": "kurse"}
            if self._idkunde:
                data["idkunde"] = self._idkunde
            resp = await self._client.post(ACCOUNT_PATH, data=data)
            resp.raise_for_status()
            html = resp.text
        return html

    async def list_slots(self) -> list[Slot]:
        return parse_schedule(await self.fetch_schedule_html())

    async def find_slot(self, sport: str, day: str, time_slot: str) -> Slot | None:
        for slot in await self.list_slots():
            if slot.matches(sport, day, time_slot):
                return slot
        return None

    # -- booking ------------------------------------------------------------
    async def book(self, slot: Slot) -> BookingResult:
        """Submit a slot's booking. Caller must ensure it is OPEN.

        The portal uses a two-step flow: the first POST returns a confirmation
        page with a ``<form id="ok">`` that JavaScript auto-submits; we must POST
        that second form to actually commit. Success is then verified by
        re-reading the schedule (the slot flips to BOOKED for this account),
        which is far more reliable than matching page text.
        """
        if not slot.form_action:
            return BookingResult(ok=False, detail="Slot has no booking form.")
        payload = dict(slot.fields)
        if slot.submit_name:
            payload[slot.submit_name] = slot.submit_value

        resp = await self._client.post(f"/{slot.form_action}", data=payload)
        final_text = resp.text
        confirm = _parse_confirm_form(resp.text)
        if confirm is not None:
            action, fields = confirm
            resp2 = await self._client.post(action, data=fields)
            final_text = resp2.text

        if await self._slot_now_booked(slot):
            log.info("Booking confirmed: %s %s %s.", slot.course, slot.day, slot.start)
            return BookingResult(ok=True, detail="confirmed booked", status_code=resp.status_code)

        permanent, message = _failure_hint(final_text)
        log.info("Booking not confirmed (permanent=%s): %s", permanent, message[:120])
        return BookingResult(
            ok=False, detail="not confirmed", status_code=resp.status_code,
            permanent=permanent, message=message,
        )

    async def cancel(self, slot: Slot) -> BookingResult:
        """Cancel a BOOKED slot via its storno form (same two-step pattern).

        Success is confirmed by re-reading the schedule (the slot is no longer
        BOOKED for this account).
        """
        if not slot.form_action:
            return BookingResult(ok=False, detail="Slot has no storno form.")
        payload = dict(slot.fields)
        if slot.submit_name:
            payload[slot.submit_name] = slot.submit_value

        resp = await self._client.post(f"/{slot.form_action}", data=payload)
        final_text = resp.text
        confirm = _parse_confirm_form(resp.text)
        if confirm is not None:
            action, fields = confirm
            resp2 = await self._client.post(action, data=fields)
            final_text = resp2.text

        if not await self._slot_now_booked(slot):
            log.info("Cancel confirmed: %s %s %s.", slot.course, slot.day, slot.start)
            return BookingResult(ok=True, detail="canceled", status_code=resp.status_code)

        _, message = _failure_hint(final_text)
        log.info("Cancel not confirmed: %s", message[:120])
        return BookingResult(ok=False, detail="cancel not confirmed", message=message)

    async def _slot_now_booked(self, slot: Slot) -> bool:
        """Re-read the schedule; True if this exact slot is now BOOKED for us."""
        try:
            for s in await self.list_slots():
                if s.course == slot.course and s.day == slot.day and s.start == slot.start:
                    return s.availability == Availability.BOOKED
        except Exception as exc:  # noqa: BLE001
            log.warning("Post-book verification failed: %s", exc)
        return False

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _looks_logged_in(html: str) -> bool:
        return _contains_any(html, LOGGED_IN_MARKERS)


def _contains_any(html: str, markers: tuple[str, ...]) -> bool:
    low = html.lower()
    return any(m in low for m in markers)


def _find_idkunde(html: str) -> str | None:
    import re

    m = re.search(r'name=["\']idkunde["\']\s+value=["\'](\d+)["\']', html)
    if not m:
        m = re.search(r'value=["\'](\d+)["\']\s+[^>]*name=["\']idkunde["\']', html)
    return m.group(1) if m else None


# Phrases (in a booking-failure page) that mean retrying won't help right now,
# e.g. the portal's 24-hour advance rule. Kept specific so they don't match the
# unrelated ``sperre24`` hidden field that appears on every form.
PERMANENT_MARKERS = (
    "innerhalb von 24", "24 stunden", "stunden vor dessen start",
    "kannst du diesen kurs erst", "bereits gebucht", "schon gebucht",
    "bereits angemeldet",
)


def _parse_confirm_form(html: str) -> tuple[str, dict[str, str]] | None:
    """If the response is the auto-submit confirmation page, return its
    (action, hidden fields); else None."""
    if "id='ok'" not in html and 'id="ok"' not in html:
        return None
    form = BeautifulSoup(html, "html.parser").find("form", id="ok")
    if form is None:
        return None
    action = (form.get("action") or "").strip()
    if not action:
        return None
    fields = {i.get("name"): i.get("value", "") for i in form.find_all("input") if i.get("name")}
    return action, fields


def _failure_hint(html: str) -> tuple[bool, str]:
    """Return (permanent, short_message) for a booking that didn't confirm.

    The portal puts the real reason in a hidden ``<input name="fehler"
    value="...">``, so we read that field first; stripping tags would discard it.
    """
    import html as html_mod
    import re

    m = re.search(r'name=["\']fehler["\']\s+value=["\']([^"\']+)["\']', html)
    if m:
        text = html_mod.unescape(m.group(1)).strip()
    else:
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
    low = text.lower()
    permanent = any(marker in low for marker in PERMANENT_MARKERS)
    return permanent, text[:300]
