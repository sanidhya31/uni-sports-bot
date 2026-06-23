"""Phase 0 proof: log in over plain HTTP and list the live schedule.

Reads UNI_USERNAME / PASSWORD from .env, logs in via PortalClient (no browser),
fetches the schedule, and prints every parsed slot grouped by availability.

Run:  ./venv/Scripts/python.exe -m scripts.phase0_proof
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.portal import PortalClient
from app.slots import Availability


async def main() -> int:
    load_dotenv(override=True)  # .env wins over the OS USERNAME var on Windows
    username = os.getenv("UNI_USERNAME") or os.getenv("USERNAME", "")
    password = os.getenv("PASSWORD", "")
    if not username or not password:
        print("Missing UNI_USERNAME / PASSWORD in .env")
        return 1

    async with PortalClient(username, password) as portal:
        print(f"Logging in as {username} ...")
        await portal.login()
        print("Login OK. Fetching schedule ...\n")
        slots = await portal.list_slots()

    print(f"Parsed {len(slots)} slots: {dict(Counter(s.availability.value for s in slots))}\n")

    print("=== OPEN slots ===")
    for s in sorted((x for x in slots if x.availability == Availability.OPEN), key=lambda x: (x.day, x.start)):
        print(f"  {s.day:11} {s.time_range:15} rest={s.rest!s:>4}  {s.course[:45]}")

    print("\n=== WAITLIST / BOOKED ===")
    for s in slots:
        if s.availability in (Availability.WAITLIST, Availability.BOOKED):
            print(f"  {s.availability.value:9} {s.day:11} {s.time_range:15} {s.course[:45]}")

    # Spot-check the configured target resolves.
    sport = os.getenv("SPORT", "")
    day = os.getenv("DAY", "")
    time_slot = os.getenv("TIME_SLOT", "")
    if sport:
        match = next((s for s in slots if s.matches(sport, day, time_slot)), None)
        print(f"\n=== Target {sport} {day} {time_slot} ===")
        print(f"  {match.summary()}" if match else "  (not found in current schedule)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
