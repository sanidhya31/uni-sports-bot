"""Telegram control - Phase 1: registration + admin approval, on TWO bots.

* User bot  (public)  : users send ``/register``, ``/mystatus``, ``/help``.
* Admin bot (private) : the admin gets access requests with inline
  ``[✅ Approve] [❌ Reject]`` buttons and runs ``/users``, ``/hold``, etc.

The app owns both bot tokens and runs both long-poll loops against one shared
``UserStore``. A registration on the user bot pushes the request to the admin
bot; an approval on the admin bot messages the user back on the user bot.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.db import Status, User, UserStore
from app.portal import PortalClient
from app.settings import Settings
from app.slots import Availability, Slot
from app.telegram_api import TelegramAPI

log = logging.getLogger(__name__)

USER_COMMANDS = [
    {"command": "register", "description": "Request access: /register <uni_email> <password>"},
    {"command": "slots", "description": "Browse live slots and pick your target"},
    {"command": "mytarget", "description": "Set target manually: /mytarget Badminton Donnerstag 14:00"},
    {"command": "mystart", "description": "Start auto-booking your target"},
    {"command": "mystop", "description": "Stop auto-booking"},
    {"command": "mystatus", "description": "Show your approval status and target"},
    {"command": "help", "description": "Show available commands"},
]

DAY_ORDER = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]

ADMIN_COMMANDS = [
    {"command": "users", "description": "List everyone"},
    {"command": "pending", "description": "List pending requests"},
    {"command": "stats", "description": "Counts by status / running / queued"},
    {"command": "info", "description": "/info <id> - full details for a user"},
    {"command": "approve", "description": "/approve <id>"},
    {"command": "reject", "description": "/reject <id>"},
    {"command": "hold", "description": "/hold <id> - park a user"},
    {"command": "release", "description": "/release <id> - un-park a user"},
    {"command": "pauseall", "description": "Hold every user"},
    {"command": "resumeall", "description": "Release every user"},
    {"command": "kick", "description": "/kick <id> - remove a user"},
    {"command": "priority", "description": "/priority <id> <n>"},
    {"command": "settarget", "description": "/settarget <id> <sport> <day> <time>"},
    {"command": "interval", "description": "/interval <seconds> - polling speed"},
    {"command": "broadcast", "description": "/broadcast <message> - message all users"},
    {"command": "adminhelp", "description": "Show admin commands"},
]

USER_BOT_DESCRIPTION = (
    "🏸 uniSPORT auto-booking for Uni Trier. Register with your uni login, pick a "
    "sport/day/time, and I grab the slot the instant it opens - even if it's full "
    "now, I wait and strike. Send /register to begin, then /help."
)
USER_BOT_SHORT = "Auto-books your Uni Trier uniSPORT slot the moment it opens."
ADMIN_BOT_DESCRIPTION = (
    "Admin console for the uniSPORT booking bot: approve users, set priority, "
    "hold/release, broadcast. Send /adminhelp."
)

ADMIN_COMMAND_SET = {
    "/users", "/pending", "/stats", "/info", "/approve", "/reject", "/hold",
    "/release", "/pauseall", "/resumeall", "/kick", "/priority", "/settarget",
    "/interval", "/broadcast", "/adminhelp",
}


class BotApp:
    def __init__(
        self,
        settings: Settings,
        store: UserStore,
        db_executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.admin_id = settings.admin_user_id
        self.user_api = TelegramAPI(settings.bot_token)
        # Fall back to the single bot if no dedicated admin token is configured.
        self.single_bot = not settings.admin_bot_token
        self.admin_api = self.user_api if self.single_bot else TelegramAPI(settings.admin_bot_token)
        # One worker => all sqlite access serialized on a single, consistent thread.
        # Shared with the engine when run together (see app.app).
        self._db_executor = db_executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="db")
        # Per-user cache of the last fetched slot list, so callbacks can reference
        # a slot by index without re-scraping.
        self._slot_cache: dict[int, list[Slot]] = {}

    # -- lifecycle ----------------------------------------------------------
    async def run(self) -> None:
        if not self.settings.bot_token or not self.admin_id:
            log.error("Disabled: TELEGRAM_BOT_TOKEN or ADMIN_USER_ID missing.")
            return

        user_me = await self.user_api.get_me()
        log.info("User bot online as @%s", user_me.get("result", {}).get("username"))
        await self.user_api.set_my_commands(USER_COMMANDS)
        await self.user_api.set_my_description(USER_BOT_DESCRIPTION, USER_BOT_SHORT)

        loops = [self.user_api.poll(self._on_user_update)]
        if self.single_bot:
            log.warning("ADMIN_BOT_TOKEN not set - admin runs on the same bot.")
        else:
            admin_me = await self.admin_api.get_me()
            log.info("Admin bot online as @%s", admin_me.get("result", {}).get("username"))
            await self.admin_api.set_my_commands(ADMIN_COMMANDS)
            await self.admin_api.set_my_description(ADMIN_BOT_DESCRIPTION)
            loops.append(self.admin_api.poll(self._on_admin_update))

        try:
            await asyncio.gather(*loops)
        finally:
            await self.user_api.aclose()
            if not self.single_bot:
                await self.admin_api.aclose()

    async def _db(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._db_executor, fn, *args)

    # -- USER BOT -----------------------------------------------------------
    async def _on_user_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            cb = update["callback_query"]
            data = cb.get("data") or ""
            # In single-bot mode, admin approve/reject buttons arrive here too.
            if data.startswith(("approve:", "reject:")):
                await self._on_admin_callback(cb)
            else:
                await self._on_user_callback(cb)
            return
        msg = update.get("message") or {}
        # In single-bot mode, admin commands come through the user bot.
        if self.single_bot and (msg.get("from") or {}).get("id") == self.admin_id and self._is_admin_command(msg.get("text")):
            await self._route_admin_message(msg)
            return
        if msg:
            await self._handle_user_message(msg)

    async def _handle_user_message(self, message: dict[str, Any]) -> None:
        text = (message.get("text") or "").strip()
        if not text:
            return
        chat_id = (message.get("chat") or {}).get("id")
        user = message.get("from") or {}
        user_id = user.get("id")
        message_id = message.get("message_id")
        if chat_id is None or user_id is None:
            return

        command = text.split()[0].split("@", 1)[0].lower()
        args = text.split()[1:]

        if command in {"/start", "/help"}:
            await self.user_api.send_message(chat_id, self._user_help())
        elif command == "/register":
            await self._handle_register(chat_id, user_id, user, args, message_id)
        elif command == "/mystatus":
            await self._handle_mystatus(chat_id, user_id)
        elif command == "/slots":
            await self._handle_slots(chat_id, user_id)
        elif command == "/mytarget":
            await self._handle_mytarget(chat_id, user_id, args)
        elif command in {"/mystart", "/mystop"}:
            await self._handle_toggle(chat_id, user_id, enable=command == "/mystart")
        else:
            await self.user_api.send_message(chat_id, "🤨 No idea what that was. /help, my friend.")

    async def _require_approved(self, chat_id: int, user_id: int) -> User | None:
        u = await self._db(self.store.get_user, user_id)
        if u is None:
            await self.user_api.send_message(chat_id, "Who dis? You're not registered. /register first, stranger.")
            return None
        if not u.is_approved:
            await self.user_api.send_message(
                chat_id, f"Patience - you're '{u.status}'. The admin hasn't waved you through yet. 🚧"
            )
            return None
        return u

    async def _fetch_user_slots(self, u: User) -> list[Slot]:
        password = await self._db(self.store.get_password, u.telegram_user_id)
        if not password:
            raise RuntimeError("No stored password (re-register).")
        async with PortalClient(u.uni_username, password, base_url=self.settings.portal_base_url) as portal:
            await portal.login()
            return await portal.list_slots()

    async def _handle_slots(self, chat_id: int, user_id: int) -> None:
        u = await self._require_approved(chat_id, user_id)
        if u is None:
            return
        await self.user_api.send_message(chat_id, "🔎 Rummaging through the schedule on your behalf…")
        try:
            slots = await self._fetch_user_slots(u)
        except Exception as exc:  # noqa: BLE001
            await self.user_api.send_message(chat_id, f"😵 Couldn't load your slots: {exc}")
            return
        # Only slots you can target: open now or currently full (waitlist).
        targetable = [s for s in slots if s.availability in (Availability.OPEN, Availability.WAITLIST)]
        self._slot_cache[user_id] = targetable
        if not targetable:
            await self.user_api.send_message(chat_id, "Nothing bookable right now. The portal's emptier than the gym in January.")
            return
        days = [d for d in DAY_ORDER if any(s.day == d for s in targetable)]
        buttons = [[{"text": d, "callback_data": f"day:{d}"}] for d in days]
        await self.user_api.send_message(
            chat_id, "Pick your day of suffering:", reply_markup={"inline_keyboard": buttons}
        )

    async def _on_user_callback(self, cb: dict[str, Any]) -> None:
        user_id = (cb.get("from") or {}).get("id")
        message = cb.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        data = cb.get("data") or ""
        action, _, value = data.partition(":")
        slots = self._slot_cache.get(user_id, [])

        if action == "day":
            day_slots = [(i, s) for i, s in enumerate(slots) if s.day == value]
            if not day_slots:
                await self.user_api.answer_callback(cb["id"], "No slots; run /slots again.")
                return
            buttons = []
            for i, s in sorted(day_slots, key=lambda t: t[1].start):
                mark = "🟢" if s.availability == Availability.OPEN else "🟡"
                label = f"{mark} {s.start} {s.course[:30]}"
                buttons.append([{"text": label, "callback_data": f"pick:{i}"}])
            await self.user_api.answer_callback(cb["id"])
            await self.user_api.send_message(
                chat_id, f"{value} - pick your poison (🟢 open now · 🟡 full, I'll stalk it):",
                reply_markup={"inline_keyboard": buttons},
            )
        elif action == "pick":
            if not value.isdigit() or int(value) >= len(slots):
                await self.user_api.answer_callback(cb["id"], "Expired; run /slots again.")
                return
            s = slots[int(value)]
            await self._db(self.store.set_target, user_id, s.course, s.day, s.start)
            await self.user_api.answer_callback(cb["id"], "Locked on. 🎯")
            await self.user_api.send_message(
                chat_id,
                f"🎯 Target acquired:\n{s.course}\n{s.day} {s.time_range}\n"
                f"(right now it's {s.availability.value})\n\n"
                "Hit /mystart and go live your life - I've got this.",
            )

    async def _handle_mytarget(self, chat_id: int, user_id: int, args: list[str]) -> None:
        u = await self._require_approved(chat_id, user_id)
        if u is None:
            return
        if len(args) < 3:
            await self.user_api.send_message(
                chat_id, "Usage: /mytarget <sport> <day> <time>\nE.g. /mytarget Badminton Donnerstag 14:00"
            )
            return
        sport, day, time_slot = " ".join(args[:-2]), args[-2], args[-1]
        await self._db(self.store.set_target, user_id, sport, day, time_slot)
        await self.user_api.send_message(
            chat_id, f"🎯 Noted: {sport} {day} {time_slot}. Spelling's on you - I match what the portal calls it. /mystart when ready."
        )

    async def _handle_toggle(self, chat_id: int, user_id: int, enable: bool) -> None:
        u = await self._require_approved(chat_id, user_id)
        if u is None:
            return
        if enable and not u.has_target:
            await self.user_api.send_message(chat_id, "Aim before you fire - set a target with /slots or /mytarget first. 🎯")
            return
        await self._db(self.store.set_enabled, user_id, enable)
        if enable:
            await self.user_api.send_message(
                chat_id,
                "▶️ I'm on it. The instant that slot cracks open, it's yours.\n\n"
                "⚠️ Heads-up on the portal's 24-hour rule: if you already hold another "
                "booking within 24h of this one, the portal will reject it (same-day is "
                "fine; booking in advance needs a 24h gap). I'll still try and tell you if "
                "it slaps me - no harm in attempting.",
            )
        else:
            await self.user_api.send_message(chat_id, "⏸️ Fine, I'll stop. Back to refreshing the page yourself, I guess. 🥱")

    async def _handle_register(
        self, chat_id, user_id, user, args, message_id
    ) -> None:
        if len(args) < 2:
            await self.user_api.send_message(
                chat_id,
                "Usage: /register <uni_email> <password>\n"
                "e.g. /register max@uni-trier.de hunter2\n\n"
                "🔒 Your uniSPORT password is encrypted and nobody (not even the admin) "
                "ever sees it. Don't trust that? Then fuck off. 🫡",
            )
            return
        uni_email = args[0]
        uni_password = " ".join(args[1:])
        display_name = " ".join(p for p in [user.get("first_name"), user.get("last_name")] if p)
        username = user.get("username") or ""

        # Reject if this uni account is already claimed by someone else.
        owner = await self._db(self.store.get_user_by_uni, uni_email)
        if owner is not None and owner.telegram_user_id != user_id:
            await self.user_api.send_message(
                chat_id,
                "⛔ Someone already registered this uniSPORT account. One account, one human. "
                "If that's you on another Telegram, pick a lane. If it's not you, spicy. 👀",
            )
            return

        await self.user_api.send_message(chat_id, "🔐 Poking the portal to see if these creds are real, one sec.")
        if not await self._verify_credentials(uni_email, uni_password):
            await self.user_api.send_message(
                chat_id,
                "❌ That login bounced. Wrong email or password (or the portal's napping). "
                "Fix it and send /register again.",
            )
            return

        existing = await self._db(self.store.get_user, user_id)
        if existing is not None and existing.is_approved:
            await self._db(self.store.update_credentials, user_id, uni_email, uni_password)
            await self.user_api.send_message(
                chat_id, "✅ Creds updated and you're still approved, no need to grovel twice. Hit /slots.",
            )
            return

        await self._db(self.store.register_pending, user_id, username, display_name, uni_email, uni_password)
        await self.user_api.send_message(
            chat_id,
            "✅ Creds check out. Request's with the admin now, you'll get a ping the moment you're in.",
        )
        await self._notify_admin_pending(user_id, username, display_name, uni_email)

    async def _verify_credentials(self, uni_email: str, uni_password: str) -> bool:
        try:
            async with PortalClient(
                uni_email, uni_password, base_url=self.settings.portal_base_url
            ) as portal:
                await portal.login()
            return True
        except Exception as exc:  # noqa: BLE001 - any failure means "not verified"
            log.info("Credential check failed for %s: %s", uni_email, exc)
            return False

    async def _handle_mystatus(self, chat_id, user_id) -> None:
        u = await self._db(self.store.get_user, user_id)
        if u is None:
            await self.user_api.send_message(chat_id, "You're not registered. Send /register to request access.")
        else:
            await self.user_api.send_message(chat_id, self._user_summary(u))

    # -- ADMIN BOT ----------------------------------------------------------
    async def _on_admin_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self._on_admin_callback(update["callback_query"])
        elif "message" in update:
            await self._route_admin_message(update["message"])

    async def _route_admin_message(self, message: dict[str, Any]) -> None:
        text = (message.get("text") or "").strip()
        chat_id = (message.get("chat") or {}).get("id")
        user_id = (message.get("from") or {}).get("id")
        if not text or chat_id is None:
            return
        if user_id != self.admin_id:
            await self.admin_api.send_message(chat_id, "Not authorized.")
            return
        command = text.split()[0].split("@", 1)[0].lower()
        args = text.split()[1:]
        await self._handle_admin(chat_id, command, args)

    @staticmethod
    def _is_admin_command(text: str | None) -> bool:
        if not text:
            return False
        cmd = text.split()[0].split("@", 1)[0].lower()
        return cmd in ADMIN_COMMAND_SET

    async def _notify_admin_pending(self, user_id, username, display_name, uni_email) -> None:
        handle = f"@{username}" if username else "(no username)"
        text = (
            "🆕 New access request\n"
            f"Name: {display_name or '-'}\n"
            f"Telegram: {handle} (id {user_id})\n"
            f"Uni email: {uni_email}\n\n"
            "Approve this user?"
        )
        markup = {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"approve:{user_id}"},
                {"text": "❌ Reject", "callback_data": f"reject:{user_id}"},
            ]]
        }
        await self.admin_api.send_message(self.admin_id, text, reply_markup=markup)

    async def _handle_admin(self, chat_id, command, args) -> None:
        if command in {"/start", "/help", "/adminhelp"}:
            await self.admin_api.send_message(chat_id, self._admin_help())
            return
        if command in {"/users", "/pending"}:
            status = Status.PENDING if command == "/pending" else None
            users = await self._db(self.store.list_users, status)
            await self.admin_api.send_message(chat_id, self._users_table(users))
            return
        if command == "/stats":
            await self.admin_api.send_message(chat_id, await self._stats_text())
            return
        if command in {"/pauseall", "/resumeall"}:
            hold = command == "/pauseall"
            users = await self._db(self.store.list_users, Status.APPROVED)
            for u in users:
                await self._db(self.store.set_hold, u.telegram_user_id, hold)
            await self.admin_api.send_message(
                chat_id, f"{'Held' if hold else 'Released'} {len(users)} user(s)."
            )
            return
        if command == "/interval":
            if not args or not args[0].isdigit():
                await self.admin_api.send_message(chat_id, "Usage: /interval <seconds>")
                return
            self.settings.poll_interval_seconds = max(1, int(args[0]))
            await self.admin_api.send_message(
                chat_id, f"Polling interval set to {self.settings.poll_interval_seconds}s (applies to running watchers)."
            )
            return
        if command == "/broadcast":
            msg = " ".join(args).strip()
            if not msg:
                await self.admin_api.send_message(chat_id, "Usage: /broadcast <message>")
                return
            users = await self._db(self.store.list_users, Status.APPROVED)
            for u in users:
                await self.user_api.send_message(u.telegram_user_id, f"📢 {msg}")
            await self.admin_api.send_message(chat_id, f"Broadcast sent to {len(users)} user(s).")
            return
        if not args or not args[0].lstrip("-").isdigit():
            await self.admin_api.send_message(chat_id, f"Usage: {command} <telegram_user_id> [value]")
            return
        target = int(args[0])

        if command == "/info":
            u = await self._db(self.store.get_user, target)
            await self.admin_api.send_message(chat_id, self._user_detail(u) if u else f"No such user: {target}")
            return
        if command == "/settarget":
            if len(args) < 4:
                await self.admin_api.send_message(chat_id, "Usage: /settarget <id> <sport> <day> <time>")
                return
            sport, day, time_slot = " ".join(args[1:-2]), args[-2], args[-1]
            await self._db(self.store.set_target, target, sport, day, time_slot)
            await self.admin_api.send_message(chat_id, f"Target for {target} set: {sport} {day} {time_slot}")
            await self.user_api.send_message(target, f"🎯 Admin set your target: {sport} {day} {time_slot}")
            return

        if command == "/approve":
            await self._approve(target, chat_id)
        elif command == "/reject":
            await self._reject(target, chat_id)
        elif command == "/hold":
            await self._db(self.store.set_hold, target, True)
            await self.admin_api.send_message(chat_id, f"User {target} put on hold.")
        elif command == "/release":
            await self._db(self.store.set_hold, target, False)
            await self.admin_api.send_message(chat_id, f"User {target} released.")
        elif command == "/kick":
            await self._db(self.store.delete_user, target)
            await self.admin_api.send_message(chat_id, f"User {target} removed.")
            await self.user_api.send_message(target, "Your access has been removed by the admin.")
        elif command == "/priority":
            if len(args) < 2 or not args[1].isdigit():
                await self.admin_api.send_message(chat_id, "Usage: /priority <id> <number> (lower = first)")
                return
            await self._db(self.store.set_priority, target, int(args[1]))
            await self.admin_api.send_message(chat_id, f"User {target} priority set to {int(args[1])}.")

    async def _on_admin_callback(self, cb: dict[str, Any]) -> None:
        api = self.admin_api  # callbacks live on the admin bot's messages
        if (cb.get("from") or {}).get("id") != self.admin_id:
            await api.answer_callback(cb["id"], "Not authorized.")
            return
        data = cb.get("data") or ""
        message = cb.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        action, _, raw_id = data.partition(":")
        if not raw_id.lstrip("-").isdigit():
            await api.answer_callback(cb["id"], "Bad data.")
            return
        target = int(raw_id)

        if action == "approve":
            await self._approve(target, chat_id)
            verb = "approved ✅"
        elif action == "reject":
            await self._reject(target, chat_id)
            verb = "rejected ❌"
        else:
            await api.answer_callback(cb["id"], "Unknown action.")
            return
        await api.answer_callback(cb["id"], f"User {target} {verb}")
        if chat_id is not None and message_id is not None:
            await api.edit_message_text(chat_id, message_id, f"User {target} {verb}.")

    async def _approve(self, target: int, admin_chat_id) -> None:
        u = await self._db(self.store.get_user, target)
        if u is None:
            if admin_chat_id:
                await self.admin_api.send_message(admin_chat_id, f"No such user: {target}")
            return
        await self._db(self.store.set_status, target, Status.APPROVED)
        await self.user_api.send_message(
            target,
            "🎉 You're in! The bouncer (admin) let you past the velvet rope.\n"
            "Now hit /slots, point me at a slot, and /mystart. Easy.",
        )

    async def _reject(self, target: int, admin_chat_id) -> None:
        await self._db(self.store.set_status, target, Status.REJECTED)
        await self.user_api.send_message(
            target, "❌ Admin said no. Don't take it personally… or do, I'm a bot, I won't judge. 🤷",
        )

    # -- text helpers -------------------------------------------------------
    def _user_help(self) -> str:
        return (
            "🏸 Your personal slot-snatching goblin.\n"
            "You sleep, I refresh the portal like a maniac. The menu:\n\n"
            "/register <uni_email> <password> - hand over your creds\n"
            "/slots - browse what's bookable and point me at one\n"
            "/mytarget <sport> <day> <time> - set it manually, if you're fancy\n"
            "/mystart - unleash me, /mystop - put me back on the leash\n"
            "/mystatus - what I'm currently obsessing over\n"
            "/help - this glorious wall of text"
        )

    def _admin_help(self) -> str:
        return (
            "Admin commands:\n"
            "/users - list everyone · /pending - requests\n"
            "/stats - counts (active/idle/queued/booked)\n"
            "/info <id> - full details for one user\n"
            "/approve <id> · /reject <id>\n"
            "/hold <id> · /release <id> - park/un-park\n"
            "/pauseall · /resumeall - hold/release everyone\n"
            "/priority <id> <n> - lower = books first\n"
            "/settarget <id> <sport> <day> <time>\n"
            "/interval <seconds> - polling speed\n"
            "/broadcast <message> - message all users\n"
            "/kick <id> - remove a user"
        )

    async def _stats_text(self) -> str:
        users = await self._db(self.store.list_users, None)
        pending = sum(1 for u in users if u.status == Status.PENDING.value)
        approved = [u for u in users if u.is_approved]
        active = sum(1 for u in approved if u.is_active)
        on_hold = sum(1 for u in approved if u.on_hold)
        booked = sum(1 for u in users if u.booked_at)
        cap = self.settings.max_concurrent_users
        queued = max(0, active - cap)
        return (
            f"Users: {len(users)} total\n"
            f"Pending: {pending}\n"
            f"Approved: {len(approved)} (active {active}, on-hold {on_hold})\n"
            f"Running cap: {cap}  ·  queued: {queued}\n"
            f"Booked: {booked}\n"
            f"Poll interval: {self.settings.poll_interval_seconds}s"
        )

    def _user_detail(self, u: User) -> str:
        handle = f"@{u.telegram_username}" if u.telegram_username else "(no username)"
        target = f"{u.sport} {u.day} {u.time_slot}".strip() or "(none)"
        return (
            f"id {u.telegram_user_id} · {handle}\n"
            f"Name: {u.display_name or '-'}\n"
            f"Uni: {u.uni_username}\n"
            f"Status: {u.status}{' · on hold' if u.on_hold else ''}\n"
            f"Target: {target}\n"
            f"Auto-booking: {'on' if u.is_active else 'off'}\n"
            f"Priority: {u.priority}\n"
            f"Booked: {u.booked_at or 'no'}\n"
            f"Registered: {u.created_at}"
        )

    def _users_table(self, users: list[User]) -> str:
        if not users:
            return "No users yet."
        lines = []
        for u in users:
            flags = []
            if u.is_active:
                flags.append("active")
            elif u.is_approved:
                flags.append("idle")
            if u.on_hold:
                flags.append("hold")
            if u.booked_at:
                flags.append("booked")
            tag = ",".join(flags) or u.status
            target = f"{u.sport} {u.day} {u.time_slot}".strip() or "no target"
            handle = f"@{u.telegram_username}" if u.telegram_username else u.display_name or "-"
            lines.append(f"{u.telegram_user_id} {handle} [{u.status}/{tag}] p{u.priority} · {target}")
        return "\n".join(lines)

    def _user_summary(self, u: User) -> str:
        target = f"{u.sport} {u.day} {u.time_slot}".strip() or "(not set)"
        state = "approved" if u.is_approved else u.status
        extra = " · on hold" if u.on_hold else ""
        booked = "\nBooked: yes" if u.booked_at else ""
        running = "running" if u.is_active else ("idle" if u.is_approved else state)
        return (
            f"Status: {state}{extra}\n"
            f"Target: {target}\n"
            f"Auto-booking: {running}\n"
            f"Priority: {u.priority}{booked}"
        )
