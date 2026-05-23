from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import Config
from app.runtime_config import RuntimeConfig

log = logging.getLogger(__name__)

BOT_COMMANDS = [
    {"command": "status", "description": "Show target, pause state, dry-run, and interval"},
    {"command": "check", "description": "Check the current target availability now"},
    {"command": "target", "description": "Set target: /target Badminton Donnerstag 14:00"},
    {"command": "set", "description": "Set one field: /set day Donnerstag"},
    {"command": "startbot", "description": "Resume continuous booking checks"},
    {"command": "stopbot", "description": "Pause booking checks but keep Telegram alive"},
    {"command": "dryrun", "description": "Toggle real booking: /dryrun on or /dryrun off"},
    {"command": "interval", "description": "Set polling interval in seconds"},
    {"command": "help", "description": "Show command examples"},
]

BOT_DESCRIPTION = (
    "Uni Trier sports booking assistant. It watches one sport/day/time, reports availability, "
    "and can submit the booking form when dry-run is off. Keep it paused until the target looks right."
)

BOT_SHORT_DESCRIPTION = "Watch and book Uni Trier sports slots from Telegram."


class TelegramControlBot:
    def __init__(
        self,
        cfg: Config,
        status_provider: Callable[[], Awaitable[str]],
    ) -> None:
        self.cfg = cfg
        self.status_provider = status_provider
        self.offset = 0

    async def run(self) -> None:
        if not self.cfg.telegram_bot_token or not self.cfg.telegram_allowed_user_id:
            log.info("Telegram control bot is disabled; TELEGRAM_BOT_TOKEN or TELEGRAM_ALLOWED_USER_ID is missing.")
            return

        log.info("Telegram control bot started.")
        await self._configure_bot_profile()
        while True:
            try:
                updates = await self._api(
                    "getUpdates",
                    {
                        "offset": self.offset,
                        "timeout": 25,
                        "allowed_updates": json.dumps(["message"]),
                    },
                    timeout=35,
                )
                for update in updates.get("result", []):
                    self.offset = max(self.offset, update["update_id"] + 1)
                    await self._handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Telegram control polling failed: %s", exc)
                await asyncio.sleep(5)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        user = message.get("from") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        user_id = user.get("id")
        text = (message.get("text") or "").strip()

        if not chat_id or not text:
            return

        if user_id != self.cfg.telegram_allowed_user_id:
            await self.send_message(chat_id, "Unauthorized.")
            return

        response = await self._handle_command(text)
        await self.send_message(chat_id, response)

    async def _handle_command(self, text: str) -> str:
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        args = parts[1:]

        runtime = RuntimeConfig.load(self.cfg.runtime_config_path, self.cfg)

        if command in {"/start", "/help"}:
            return self._help_text()

        if command == "/status":
            return runtime.summary()

        if command == "/check":
            runtime.apply_to(self.cfg)
            return await self.status_provider()

        if command == "/startbot":
            runtime.enabled = True
            runtime.save(self.cfg.runtime_config_path)
            return "Bot running."

        if command == "/stopbot":
            runtime.enabled = False
            runtime.save(self.cfg.runtime_config_path)
            return "Bot paused."

        if command == "/dryrun":
            if len(args) != 1 or args[0].lower() not in {"on", "off", "true", "false"}:
                return "Usage: /dryrun on|off"
            runtime.dry_run = args[0].lower() in {"on", "true"}
            runtime.save(self.cfg.runtime_config_path)
            return f"Dry run: {'on' if runtime.dry_run else 'off'}"

        if command == "/interval":
            if len(args) != 1 or not args[0].isdigit():
                return "Usage: /interval 3"
            runtime.poll_interval_seconds = max(1, int(args[0]))
            runtime.save(self.cfg.runtime_config_path)
            return f"Interval: {runtime.poll_interval_seconds}s"

        if command == "/target":
            if len(args) < 3:
                return "Usage: /target <sport> <day> <time>, e.g. /target Badminton Donnerstag 14:00"
            runtime.time_slot = args[-1]
            runtime.day = args[-2]
            runtime.sport = " ".join(args[:-2])
            runtime.save(self.cfg.runtime_config_path)
            return f"Target set:\n{runtime.summary()}"

        if command == "/set":
            if len(args) < 2:
                return "Usage: /set sport|day|time <value>"
            key = args[0].lower()
            value = " ".join(args[1:])
            if key == "sport":
                runtime.sport = value
            elif key == "day":
                runtime.day = value
            elif key in {"time", "time_slot"}:
                runtime.time_slot = value
            else:
                return "Usage: /set sport|day|time <value>"
            runtime.save(self.cfg.runtime_config_path)
            return f"Updated {key}.\n{runtime.summary()}"

        return "Unknown command. Send /help."

    def _help_text(self) -> str:
        return (
            "Commands:\n"
            "/status - show current target and bot state\n"
            "/check - check availability once now\n"
            "/target Badminton Donnerstag 14:00 - set sport, day, and time\n"
            "/set sport Badminton - change only sport\n"
            "/set day Donnerstag - change only day\n"
            "/set time 14:00 - change only time\n"
            "/startbot - resume continuous checking\n"
            "/stopbot - pause checking\n"
            "/dryrun on|off - block or allow real booking\n"
            "/interval 3 - check every 3 seconds"
        )

    async def _configure_bot_profile(self) -> None:
        try:
            await self._api("setMyCommands", {"commands": json.dumps(BOT_COMMANDS)}, timeout=15)
            await self._api("setMyDescription", {"description": BOT_DESCRIPTION}, timeout=15)
            await self._api("setMyShortDescription", {"short_description": BOT_SHORT_DESCRIPTION}, timeout=15)
            log.info("Telegram command menu and descriptions configured.")
        except Exception as exc:
            log.warning("Could not configure Telegram command menu: %s", exc)

    async def send_message(self, chat_id: int | str, text: str) -> None:
        try:
            await self._api("sendMessage", {"chat_id": chat_id, "text": text}, timeout=15)
        except urllib.error.HTTPError as exc:
            log.warning("Telegram sendMessage failed with HTTP %s. Open the bot chat and send /start first.", exc.code)
        except Exception as exc:
            log.warning("Telegram sendMessage failed: %s", exc)

    async def _api(self, method: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
        return await asyncio.to_thread(self._api_sync, method, payload, timeout)

    def _api_sync(self, method: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/{method}"
        data = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
