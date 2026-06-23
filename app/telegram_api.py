"""Minimal async Telegram Bot API wrapper (httpx) with long-polling.

Supports messages and callback queries (inline buttons), which the multi-user
registration/approval flow needs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]


class TelegramAPI:
    def __init__(self, token: str) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self._client = httpx.AsyncClient(timeout=40.0)
        self._offset = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        resp = await self._client.post(f"{self._base}/{method}", data=params)
        resp.raise_for_status()
        return resp.json()

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict | None = None,
    ) -> None:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            params["reply_markup"] = json.dumps(reply_markup)
        try:
            await self.call("sendMessage", **params)
        except httpx.HTTPError as exc:
            log.warning("sendMessage failed: %s", exc)

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except httpx.HTTPError as exc:
            log.warning("answerCallbackQuery failed: %s", exc)

    async def edit_message_text(self, chat_id: int | str, message_id: int, text: str) -> None:
        try:
            await self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text)
        except httpx.HTTPError as exc:
            log.warning("editMessageText failed: %s", exc)

    async def delete_message(self, chat_id: int | str, message_id: int) -> None:
        try:
            await self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except httpx.HTTPError as exc:
            log.debug("deleteMessage failed (often fine): %s", exc)

    async def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        try:
            await self.call("setMyCommands", commands=json.dumps(commands))
        except httpx.HTTPError as exc:
            log.warning("setMyCommands failed: %s", exc)

    async def set_my_description(self, description: str, short_description: str = "") -> None:
        try:
            await self.call("setMyDescription", description=description)
            if short_description:
                await self.call("setMyShortDescription", short_description=short_description)
        except httpx.HTTPError as exc:
            log.warning("setMyDescription failed: %s", exc)

    async def get_me(self) -> dict[str, Any]:
        return await self.call("getMe")

    async def poll(self, on_update: Handler) -> None:
        """Long-poll getUpdates forever, dispatching each update to on_update."""
        while True:
            try:
                data = await self.call(
                    "getUpdates",
                    offset=self._offset,
                    timeout=25,
                    allowed_updates=json.dumps(["message", "callback_query"]),
                )
                for update in data.get("result", []):
                    self._offset = max(self._offset, update["update_id"] + 1)
                    try:
                        await on_update(update)
                    except Exception as exc:  # noqa: BLE001 - keep the loop alive
                        log.exception("update handler error: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("getUpdates failed: %s", exc)
                await asyncio.sleep(3)
