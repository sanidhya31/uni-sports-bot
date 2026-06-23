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

from app.slots import Slot, parse_schedule

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
        """Submit a slot's booking form. Caller must ensure it is OPEN."""
        if not slot.form_action:
            return BookingResult(ok=False, detail="Slot has no booking form.")
        payload = dict(slot.fields)
        if slot.submit_name:
            payload[slot.submit_name] = slot.submit_value
        resp = await self._client.post(f"/{slot.form_action}", data=payload)
        ok, permanent, detail, message = _interpret_booking(resp.text)
        log.info(
            "Booking POST %s -> HTTP %s ok=%s permanent=%s (%s)",
            slot.form_action, resp.status_code, ok, permanent, detail,
        )
        return BookingResult(
            ok=ok, detail=detail, status_code=resp.status_code,
            permanent=permanent, message=message,
        )

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


# Rejections that won't clear by retrying — the user must change something
# (e.g. cancel a nearby booking). The portal's 24h lock (sperre24) lives here.
PERMANENT_MARKERS = (
    "24 stunden", "24-stunden", "24h", "sperre", "innerhalb von 24",
    "bereits gebucht", "schon gebucht", "bereits angemeldet",
)
SUCCESS_MARKERS = ("erfolgreich", "gebucht", "buchung wurde", "meine buchungen")
FAILURE_MARKERS = ("fehlgeschlagen", "nicht gebucht", "nicht moeglich", "nicht möglich", "ausgebucht", "fehler")


def _snippet(html: str) -> str:
    import re

    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:200]


def _interpret_booking(html: str) -> tuple[bool, bool, str, str]:
    """Return (ok, permanent, detail, message_snippet)."""
    low = html.lower()
    snippet = _snippet(html)
    for marker in PERMANENT_MARKERS:
        if marker in low:
            return False, True, f"permanent rejection: {marker}", snippet
    # Success can co-occur with the word "fehler" in unrelated page chrome, so
    # check success before generic failure.
    if any(m in low for m in SUCCESS_MARKERS) and not any(m in low for m in FAILURE_MARKERS):
        return True, False, "success", snippet
    for marker in FAILURE_MARKERS:
        if marker in low:
            return False, False, f"failure marker: {marker}", snippet
    return False, False, "no clear success/failure marker", snippet
