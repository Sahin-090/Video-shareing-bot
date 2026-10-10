"""Access gate (bot OFF / maintenance / admin block) and the global error handler.

The gate runs before every private message and button press. Admins (ADMIN_IDS) always pass,
so they can turn the bot back on, end maintenance and use every admin tool while the public
side is closed. The decision is made on the server for every update.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, ErrorEvent, Message, TelegramObject

import ui
from config import Config
from logging_setup import log_event
from services.settings_service import (BANNED_TEXT, BOT_OFF_TEXT, SettingsService,
                                       plain_text)
from services.user_service import UserService

log = logging.getLogger("gate")


class AccessGate(BaseMiddleware):
    def __init__(self, cfg: Config, settings: SettingsService, users: UserService) -> None:
        self.cfg = cfg
        self.settings = settings
        self.users = users

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        user = getattr(event, "from_user", None)
        if user is None or user.id in self.cfg.admin_ids:
            return await handler(event, data)
        if isinstance(event, Message):
            if event.chat.type != "private":
                return await handler(event, data)
            if (event.text or "").lower().split("@")[0].split(" ")[0] == "/admin":
                return await handler(event, data)    # answered with the permission notice
        if isinstance(event, CallbackQuery) and (event.data or "").startswith("adm:"):
            return await handler(event, data)        # rejected by the admin-callback guard

        text = self._blocked_text(user.id)
        if text is None:
            try:
                await self.users.touch(user.id)      # last activity
            except Exception:  # noqa: BLE001
                log.warning("touch_failed")
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            try:
                await event.answer(plain_text(text)[:190], show_alert=True)
            except TelegramAPIError:
                pass
        elif isinstance(event, Message):
            try:
                await event.answer(text, parse_mode="HTML")
            except TelegramAPIError:
                pass
        return None

    def _blocked_text(self, user_id: int) -> str | None:
        if self.users.is_banned(user_id):
            return BANNED_TEXT
        if not self.settings.bot_enabled:
            return BOT_OFF_TEXT
        if self.settings.maintenance:
            return self.settings.maintenance_text()
        return None


async def on_error(event: ErrorEvent, bot: Bot, cfg: Config) -> bool:
    """Never show Python exceptions to anyone. Users get a generic notice, admins a short
    safe summary (exception type only); the full traceback goes to the server log."""
    exc = event.exception
    if isinstance(exc, TelegramAPIError) and "not modified" in str(exc).lower():
        return True
    log.error("update_failed", exc_info=exc)
    update = event.update
    target = update.callback_query or update.message
    user = getattr(target, "from_user", None)
    if user is None:
        return True
    is_admin = user.id in cfg.admin_ids
    text = (f"⚠️ Something went wrong ({type(exc).__name__}). Details are in the server log."
            if is_admin else ui.ERROR_TEXT)
    try:
        if update.callback_query is not None:
            await update.callback_query.answer(text[:190], show_alert=True)
        elif update.message is not None:
            await update.message.answer(text)
    except TelegramAPIError:
        pass
    log_event(log, logging.ERROR, "handled_error", error=type(exc).__name__, admin=is_admin)
    return True
