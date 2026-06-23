"""Parse the Uni Trier uniSPORT schedule HTML into structured Slot objects.

The schedule ("Kurstermine der nächsten 5 Werktage") renders every course as a
card ``<div id="N" title="<kurs_id> - <id_kurs>">`` containing:

* a ``<span title="...">`` with the course name (often suffixed with the
  weekday in brackets, e.g. ``Badminton (Do)``),
* a details span with the time range (``14:00 - 15:30 Uhr``) and location,
* optionally a ``<form>`` whose ``action`` reveals the availability state,
* a ``Rest: N`` free-seat counter.

Cards are grouped under big day headers (``<span style="font-size:2em">Montag,
22.06.</span>``). We walk the document in order so each card inherits the most
recent day header, and we cross-check against the bracketed day in the title.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum

from bs4 import BeautifulSoup, Tag

# Form actions map directly onto availability states.
ACTION_BOOK_COURSE = "kurstermin_sst_buchen.php"   # open course slot -> "Buchen"
ACTION_BOOK_PLATZ = "buchen_a.php"                  # open court/"SLOT" booking
ACTION_WAITLIST = "warteliste_buchen.php"           # full -> "Warteliste"
ACTION_CANCEL = "kurs_storno_a.php"                 # already booked -> can cancel

CARD_TITLE_RE = re.compile(r"^\s*\d+\s*-\s*\d+\s*$")
TIME_RE = re.compile(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})")
REST_RE = re.compile(r"Rest:\s*(\d+)", re.IGNORECASE)
DAY_HEADER_RE = re.compile(
    r"montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag", re.IGNORECASE
)
TITLE_DAY_RE = re.compile(r"\((mo|di|mi|do|fr|sa|so)\d*\)", re.IGNORECASE)

# Canonical German weekday for every abbreviation we may encounter.
DAY_CANON = {
    "mo": "Montag", "montag": "Montag",
    "di": "Dienstag", "dienstag": "Dienstag",
    "mi": "Mittwoch", "mittwoch": "Mittwoch",
    "do": "Donnerstag", "donnerstag": "Donnerstag",
    "fr": "Freitag", "freitag": "Freitag",
    "sa": "Samstag", "samstag": "Samstag",
    "so": "Sonntag", "sonntag": "Sonntag",
}


class Availability(str, Enum):
    OPEN = "open"           # bookable right now
    WAITLIST = "waitlist"   # full, only waitlist
    BOOKED = "booked"       # this account already holds the slot
    CLOSED = "closed"       # no booking form (info-only / prerequisite missing)


@dataclass
class Slot:
    card_id: str
    kurs_id: str
    id_kurs: str
    course: str                       # full course name from span[title]
    day: str                          # canonical German weekday, or ""
    day_label: str                    # header text incl. date, e.g. "Montag, 22.06."
    start: str                        # "14:00"
    end: str                          # "15:30"
    location: str
    rest: int | None
    availability: Availability
    form_action: str                  # bare php filename, or ""
    submit_name: str                  # e.g. "sub"
    submit_value: str                 # e.g. "Buchen"
    fields: dict[str, str] = field(default_factory=dict)  # hidden inputs to POST

    @property
    def time_slot(self) -> str:
        return self.start

    @property
    def time_range(self) -> str:
        return f"{self.start} - {self.end}" if self.start and self.end else ""

    def matches(self, sport: str, day: str, time_slot: str) -> bool:
        """True if this slot is the configured target (sport+day+time)."""
        sport_ok = _norm(sport) in _norm(self.course)
        time_ok = not time_slot or _norm(time_slot) in _norm(self.time_range)
        day_ok = not day or _day_key(day) == _day_key(self.day)
        return sport_ok and time_ok and day_ok

    def summary(self) -> str:
        rest = f" · Rest {self.rest}" if self.rest is not None else ""
        return f"{self.course} · {self.day} {self.time_range} · {self.availability.value}{rest}"


def _norm(value: str) -> str:
    ascii_text = (
        unicodedata.normalize("NFKD", value or "")
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    return re.sub(r"\s+", " ", ascii_text.lower()).strip()


def _day_key(day: str) -> str:
    return DAY_CANON.get(_norm(day).rstrip("."), _norm(day))


def _is_day_header(el: Tag) -> bool:
    if el.name != "span":
        return False
    style = el.get("style") or ""
    return "2em" in style and bool(DAY_HEADER_RE.search(el.get_text()))


def _is_card(el: Tag) -> bool:
    return el.name == "div" and bool(CARD_TITLE_RE.match(el.get("title") or ""))


def _availability_for(action: str) -> Availability:
    if action in (ACTION_BOOK_COURSE, ACTION_BOOK_PLATZ):
        return Availability.OPEN
    if action == ACTION_WAITLIST:
        return Availability.WAITLIST
    if action == ACTION_CANCEL:
        return Availability.BOOKED
    return Availability.CLOSED


def _booking_form(card: Tag) -> Tag | None:
    """Pick the form that represents the slot's booking state.

    Prefer an actionable form (book / waitlist / cancel) over unrelated ones.
    """
    priority = {
        ACTION_BOOK_COURSE: 0,
        ACTION_BOOK_PLATZ: 1,
        ACTION_WAITLIST: 2,
        ACTION_CANCEL: 3,
    }
    best: Tag | None = None
    best_rank = 99
    for form in card.find_all("form"):
        action = (form.get("action") or "").rsplit("/", 1)[-1]
        rank = priority.get(action, 50)
        if rank < best_rank:
            best, best_rank = form, rank
    return best


def _extract_day(card: Tag, course: str, header_day: str) -> str:
    """Day in the bracketed title (``Badminton (Do)``) wins; else the header."""
    m = TITLE_DAY_RE.search(course)
    if m:
        return DAY_CANON[m.group(1).lower()]
    return header_day


def _extract_location(details_text: str) -> str:
    # Details look like: "Art: |14:00 - 15:30 Uhr | Halle 1-3 | benötigt SST ..."
    parts = [p.strip() for p in details_text.split("|")]
    for part in parts:
        if TIME_RE.search(part) or part.lower().startswith("art"):
            continue
        if part and "benötigt" not in part.lower() and "benotigt" not in _norm(part):
            return part
    return ""


def parse_schedule(html: str) -> list[Slot]:
    """Parse full schedule HTML into a list of Slot objects (document order)."""
    soup = BeautifulSoup(html, "html.parser")
    slots: list[Slot] = []
    current_day = ""
    current_day_label = ""

    for el in soup.find_all(["span", "div"]):
        if _is_day_header(el):
            current_day_label = re.sub(r"\s+", " ", el.get_text(strip=True))
            m = DAY_HEADER_RE.search(current_day_label)
            current_day = DAY_CANON[m.group(0).lower()] if m else ""
            continue
        if not _is_card(el):
            continue

        kurs_id, id_kurs = [p.strip() for p in el["title"].split("-", 1)]
        title_span = el.find("span", title=True)
        course = (title_span.get("title") or title_span.get_text()) if title_span else ""
        course = re.sub(r"\s+", " ", course).strip()

        full_text = el.get_text(" ", strip=True)
        time_m = TIME_RE.search(full_text)
        start = time_m.group(1) if time_m else ""
        end = time_m.group(2) if time_m else ""
        rest_m = REST_RE.search(full_text)
        rest = int(rest_m.group(1)) if rest_m else None

        details_span = title_span.find_next("span") if title_span else None
        location = _extract_location(details_span.get_text(" ", strip=True)) if details_span else ""

        form = _booking_form(el)
        action = (form.get("action") or "").rsplit("/", 1)[-1] if form else ""
        fields: dict[str, str] = {}
        submit_name = submit_value = ""
        if form is not None:
            for inp in form.find_all("input"):
                name = inp.get("name")
                if not name:
                    continue
                if (inp.get("type") or "").lower() == "submit":
                    submit_name, submit_value = name, inp.get("value", "")
                else:
                    fields[name] = inp.get("value", "")

        slots.append(
            Slot(
                card_id=el.get("id", ""),
                kurs_id=kurs_id,
                id_kurs=id_kurs,
                course=course,
                day=_extract_day(el, course, current_day),
                day_label=current_day_label,
                start=start,
                end=end,
                location=location,
                rest=rest,
                availability=_availability_for(action),
                form_action=action,
                submit_name=submit_name,
                submit_value=submit_value,
                fields=fields,
            )
        )

    return slots
