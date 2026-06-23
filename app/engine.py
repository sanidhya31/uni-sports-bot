"""Booking engine.

The engine reconciles the set of *active* users (approved, enabled, not on
hold, with a target) against a set of running per-user tasks. Each
``UserRunner`` owns one ``PortalClient`` (one httpx cookie jar) and polls the
schedule for that user's target, booking it when it opens.

Phase 3 scope: correct single-user polling + dry-run/real booking + user
notifications. Multi-user priority "strike" coordination arrives in Phase 4;
the per-user runner structure is already in place for it.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor

from app.db import User, UserStore
from app.portal import PortalClient
from app.settings import Settings
from app.slots import Availability, Slot, _day_key, _norm
from app.translate import to_english

log = logging.getLogger(__name__)

Notify = Callable[[int, str], Awaitable[None]]
NotifyAdmin = Callable[[str], Awaitable[None]]

RECONCILE_INTERVAL = 5.0  # seconds between active-user scans


async def _noop() -> None:
    return None


def target_key(user: User) -> tuple[str, str, str]:
    """Normalized (sport, day, time) so users wanting the same slot group up."""
    return (_norm(user.sport), _day_key(user.day), _norm(user.time_slot))


class UserRunner:
    def __init__(
        self,
        user: User,
        password: str,
        settings: Settings,
        store: UserStore,
        db_call: Callable[..., Awaitable],
        notify: Notify,
        notify_admin: NotifyAdmin | None = None,
    ) -> None:
        self.user = user
        self.password = password
        self.settings = settings
        self.store = store
        self._db = db_call
        self._notify = notify
        self._notify_admin = notify_admin or (lambda _msg: _noop())
        self._portal: PortalClient | None = None
        self._standby_notified = False

    @property
    def uid(self) -> int:
        return self.user.telegram_user_id

    async def run(self) -> None:
        try:
            await self._ensure_login()
            while True:
                fresh = await self._db(self.store.get_user, self.uid)
                if fresh is None or not fresh.is_active or not fresh.has_target:
                    log.info("Runner %s stopping (no longer active).", self.uid)
                    return
                self.user = fresh
                await self._tick()
                await self._sleep()
        except _Booked:
            log.info("Runner %s finished: booked.", self.uid)
            return
        except _Stopped:
            log.info("Runner %s stopped: portal refused.", self.uid)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Runner %s crashed: %s", self.uid, exc)
            await self._notify(self.uid, f"⚠️ Welp, your watcher tripped over something: {exc}\nI'll dust myself off and retry shortly.")
        finally:
            if self._portal is not None:
                await self._portal.aclose()

    async def _ensure_login(self) -> None:
        if self._portal is None:
            self._portal = PortalClient(
                self.user.uni_username, self.password, base_url=self.settings.portal_base_url
            )
        await self._portal.login()

    async def _tick(self) -> None:
        assert self._portal is not None
        try:
            slot = await self._portal.find_slot(self.user.sport, self.user.day, self.user.time_slot)
        except Exception:  # noqa: BLE001 - session may have expired; re-login once
            log.info("Runner %s re-logging in.", self.uid)
            await self._ensure_login()
            slot = await self._portal.find_slot(self.user.sport, self.user.day, self.user.time_slot)

        if slot is None:
            log.debug("Runner %s: target not in schedule yet.", self.uid)
            self._standby_notified = False
            return

        if slot.availability == Availability.BOOKED:
            await self._db(self.store.mark_booked, self.uid)
            await self._notify(
                self.uid,
                f"🎉 You already hold {slot.course} {slot.day} {slot.time_range}. "
                f"Nothing for me to do here, clocking out.",
            )
            who = self.user.display_name or self.user.uni_username or str(self.uid)
            await self._notify_admin(f"ℹ️ {who} (id {self.uid}) already holds {slot.course} {slot.day} {slot.time_range}.")
            raise _Booked()

        if slot.availability != Availability.OPEN:
            self._standby_notified = False
            return

        # Slot is OPEN. Coordinate with other users wanting the SAME slot so we
        # never have our own accounts fighting over a single seat.
        rank, group_size = await self._rank_in_group()
        seats = slot.rest if slot.rest is not None else 1

        if rank < seats:
            # Among the top-N by priority for N available seats -> strike.
            await self._book(slot)
        else:
            # Stand by; a higher-priority teammate is taking it. If they fail or
            # book (and drop out), our rank rises and we strike on a later tick.
            if not self._standby_notified and group_size > 1:
                self._standby_notified = True
                ahead = rank
                await self._notify(
                    self.uid,
                    f"🟡 {slot.course} {slot.day} {slot.time_range} popped open with {seats} seat(s), "
                    f"but {ahead} higher-priority folk get first dibs. I'm hovering right behind them - "
                    f"if a seat survives, it's yours. 🪑",
                )

    async def _rank_in_group(self) -> tuple[int, int]:
        """This user's 0-based position among active users with the same target."""
        active = await self._db(self.store.list_active)
        mine = target_key(self.user)
        group = [u for u in active if u.has_target and target_key(u) == mine]
        group.sort(key=lambda u: (u.priority, u.id))
        ids = [u.telegram_user_id for u in group]
        rank = ids.index(self.uid) if self.uid in ids else 0
        return rank, len(group)

    async def _book(self, slot: Slot) -> None:
        assert self._portal is not None
        result = await self._portal.book(slot)
        if result.ok:
            await self._db(self.store.mark_booked, self.uid)
            line = f"{slot.course} · {slot.day} {slot.time_range}"
            await self._notify(
                self.uid,
                f"✅ GOT IT. {line} is yours. Go act surprised at how athletic you are. "
                f"I'm clocking out - booking done. 🏆",
            )
            who = self.user.display_name or self.user.uni_username or str(self.uid)
            await self._notify_admin(f"🏆 {who} (id {self.uid}) just bagged {line}.")
            raise _Booked()

        if result.permanent:
            # The portal refused for a standing reason (e.g. its 24h rule). Retrying
            # won't help, so stop the run and tell the user why.
            await self._db(self.store.set_enabled, self.uid, False)
            line = f"{slot.course} {slot.day} {slot.time_range}"
            reason = (await to_english(result.message)) or "the portal refused the booking."
            await self._notify(
                self.uid,
                f"⚠️ Couldn't book {line}.\nReason: {reason}\n\n"
                f"Stopping your run now. /mystart again once it's bookable.",
            )
            who = self.user.display_name or self.user.uni_username or str(self.uid)
            await self._notify_admin(
                f"⚠️ {who} (id {self.uid}) couldn't book {line} - run stopped.\nReason: {reason}"
            )
            raise _Stopped()
        log.info("Runner %s booking not confirmed (will retry): %s", self.uid, result.detail)

    async def _sleep(self) -> None:
        base = max(1, self.settings.poll_interval_seconds)
        jitter = random.uniform(0, max(0, self.settings.poll_jitter_seconds))
        await asyncio.sleep(base + jitter)


class _Booked(Exception):
    """Internal signal: booking succeeded, stop this runner cleanly."""


class _Stopped(Exception):
    """Internal signal: stop this runner (portal refused for a standing reason)."""


class Engine:
    def __init__(
        self,
        settings: Settings,
        store: UserStore,
        db_executor: ThreadPoolExecutor,
        notify: Notify,
        notify_admin: NotifyAdmin | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self._executor = db_executor
        self._notify = notify
        self._notify_admin = notify_admin or (lambda _msg: _noop())
        self._runners: dict[int, asyncio.Task] = {}
        self._queued_notified: set[int] = set()

    async def _db(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    async def run(self) -> None:
        log.info("Engine started.")
        try:
            while True:
                await self._reconcile()
                await asyncio.sleep(RECONCILE_INTERVAL)
        except asyncio.CancelledError:
            for task in self._runners.values():
                task.cancel()
            raise

    async def _reconcile(self) -> None:
        active = await self._db(self.store.list_active)
        active = [u for u in active if u.has_target]
        wanted = {u.telegram_user_id for u in active}

        # Drop finished/cancelled runners.
        for uid in list(self._runners):
            task = self._runners[uid]
            if task.done():
                self._runners.pop(uid, None)

        # Stop runners whose user is no longer active.
        for uid in list(self._runners):
            if uid not in wanted:
                self._runners.pop(uid).cancel()

        # Respect the concurrency cap (lowest priority value first = first served).
        slots_left = self.settings.max_concurrent_users - len(self._runners)
        for u in active:
            if u.telegram_user_id in self._runners:
                continue
            if slots_left <= 0:
                # Over capacity -> queued until a running slot frees up.
                if u.telegram_user_id not in self._queued_notified:
                    self._queued_notified.add(u.telegram_user_id)
                    await self._notify(
                        u.telegram_user_id,
                        "⏳ You're in the queue - I'm maxed out juggling other people's "
                        "slots right now. The second a lane frees up, I'm on yours. Hang tight.",
                    )
                continue
            password = await self._db(self.store.get_password, u.telegram_user_id)
            if not password:
                continue
            runner = UserRunner(
                u, password, self.settings, self.store, self._db,
                self._notify, self._notify_admin,
            )
            self._runners[u.telegram_user_id] = asyncio.create_task(runner.run())
            self._queued_notified.discard(u.telegram_user_id)
            slots_left -= 1
            log.info("Engine started runner for %s (%s).", u.telegram_user_id, u.uni_username)
