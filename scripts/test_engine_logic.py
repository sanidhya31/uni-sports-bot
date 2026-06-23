"""Offline test of the priority-strike coordination (no network, no Telegram).

Simulates 3 active users targeting the SAME slot and checks that only the
top-N by priority (N = available seats) actually book, with cascade on drop-out.
"""

from __future__ import annotations

import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.db import User
from app.engine import UserRunner, _Booked
from app.portal import BookingResult
from app.settings import Settings
from app.slots import Availability, Slot


def make_user(uid: int, priority: int) -> User:
    return User(
        id=uid, telegram_user_id=uid, telegram_username=f"u{uid}", display_name=f"U{uid}",
        uni_username=f"u{uid}@uni-trier.de", sport="Badminton", day="Donnerstag",
        time_slot="14:00", status="approved", enabled=True, on_hold=False,
        priority=priority, created_at="", approved_at="", booked_at=None,
    )


def make_slot(rest: int | None) -> Slot:
    return Slot(
        card_id="1", kurs_id="1", id_kurs="1", course="Badminton (Do)", day="Donnerstag",
        day_label="Donnerstag", start="14:00", end="15:30", location="Halle",
        rest=rest, availability=Availability.OPEN, form_action="kurstermin_sst_buchen.php",
        submit_name="sub", submit_value="Buchen", fields={"kurs_id": "1"},
    )


class FakeStore:
    def __init__(self, users):
        self.users = {u.telegram_user_id: u for u in users}
        self.booked: list[int] = []

    def list_active(self):
        return [u for u in self.users.values() if u.is_active]

    def get_user(self, uid):
        return self.users.get(uid)

    def get_password(self, uid):
        return "pw"

    def mark_booked(self, uid):
        self.booked.append(uid)
        u = self.users[uid]
        self.users[uid] = User(**{**u.__dict__, "enabled": False, "booked_at": "now"})


class FakePortal:
    def __init__(self, slot):
        self.slot = slot
        self.book_calls: list[int] = []

    async def login(self):
        pass

    async def find_slot(self, sport, day, time_slot):
        return self.slot

    async def book(self, slot):
        # Model reality: a booking consumes a seat; at 0 the slot is no longer open.
        if self.slot.rest is not None:
            self.slot.rest -= 1
            if self.slot.rest <= 0:
                self.slot.availability = Availability.WAITLIST
        return BookingResult(ok=True, detail="ok")

    async def aclose(self):
        pass


async def run_case(seats, n_users=3):
    users = [make_user(101, 1), make_user(102, 2), make_user(103, 3)]  # priority 1 best
    store = FakeStore(users)
    slot = make_slot(seats)

    async def db_call(fn, *args):
        return fn(*args)

    booked_order = []
    for u in store.list_active():
        runner = UserRunner(u, "pw", Settings.load(), store, db_call, notify=_noop)
        runner._portal = FakePortal(slot)
        # one tick: decide whether THIS user books (_Booked = success signal)
        try:
            await runner._tick()
        except _Booked:
            pass
    return store.booked


async def _noop(uid, text):
    pass


async def main():
    for seats in (1, 2, 3):
        booked = await run_case(seats)
        expected = {1: [101], 2: [101, 102], 3: [101, 102, 103]}[seats]
        ok = sorted(booked) == sorted(expected)
        print(f"seats={seats}: booked={sorted(booked)} expected={expected} {'✅' if ok else '❌ FAIL'}")

    # Cascade: 1 seat, but top user already booked (dropped out) -> #2 takes it.
    users = [make_user(101, 1), make_user(102, 2)]
    store = FakeStore(users)
    store.mark_booked(101)  # 101 already booked, now inactive
    slot = make_slot(1)

    async def db_call(fn, *args):
        return fn(*args)

    runner = UserRunner(store.get_user(102), "pw", Settings.load(), store, db_call, notify=_noop)
    runner._portal = FakePortal(slot)
    try:
        await runner._tick()
    except _Booked:
        pass
    ok = 102 in store.booked
    print(f"cascade: after #1 booked, #2 books on 1 seat -> {'✅' if ok else '❌ FAIL'}")


if __name__ == "__main__":
    asyncio.run(main())
