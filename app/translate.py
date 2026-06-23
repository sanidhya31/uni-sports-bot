"""Translate portal messages (German) to English for user-facing notifications.

Uses deep-translator's free Google backend (no API key). Any failure falls back
to the original text, so we never lose the portal's message.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

try:
    from deep_translator import GoogleTranslator

    _AVAILABLE = True
except Exception:  # noqa: BLE001 - translation is best-effort
    _AVAILABLE = False

_cache: dict[str, str] = {}


def _sync_translate(text: str) -> str:
    return GoogleTranslator(source="auto", target="en").translate(text)


async def to_english(text: str) -> str:
    """Best-effort German->English; returns the original on any problem."""
    text = (text or "").strip()
    if not text or not _AVAILABLE:
        return text
    if text in _cache:
        return _cache[text]
    try:
        out = (await asyncio.to_thread(_sync_translate, text)) or text
    except Exception as exc:  # noqa: BLE001
        log.warning("Translation failed, using original: %s", exc)
        return text
    _cache[text] = out
    return out
