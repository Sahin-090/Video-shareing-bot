"""Admin broadcast: a modern announcement card sent to every active user.

* The admin only writes the message; `render()` wraps it in the bot's announcement design.
* Delivery is rate-limited (well under Telegram's ~30 msg/s limit), honours `RetryAfter`,
  skips users blocked by an admin and users who stopped the bot, and marks users who block
  the bot so the next broadcast does not try them again.
* It runs as a background task; progress is reported through a callback and the admin can stop
  it at any time. State is in memory only - a restart simply ends a running broadcast.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest, TelegramForbiddenError,
                                TelegramRetryAfter)
from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as Kb

import ui
from database import Database
from logging_setup import log_event
from services.user_service import UserService

log = logging.getLogger("broadcast")

MAX_BODY = 3500            # the card adds ~150 characters; Telegram's limit is 4096
SEND_DELAY = 0.05          # 20 messages / second
PROGRESS_EVERY = 25        # users
PROGRESS_SECONDS = 3.0


def render(body: str) -> str:
    """The modern announcement card. `body` is already safe Telegram HTML."""
    return (f"📢 <b>ANNOUNCEMENT</b>\n"
            f"{ui.LINE}\n\n"
            f"{body.strip()}\n\n"
            f"{ui.LINE}\n"
            f"{ui.FOOTER}")


def card_kb() -> Kb:
    return Kb(inline_keyboard=[[B(text="🎬 Open VideoShare", callback_data="menu")]])


@dataclass
class BroadcastResult:
    total: int = 0
    sent: int = 0
    blocked: int = 0      # users who stopped / blocked the bot
    failed: int = 0
    stopped: bool = False
    seconds: float = 0.0
    started: float = field(default_factory=time.monotonic)

    @property
    def done(self) -> int:
        return self.sent + self.blocked + self.failed

    @property
    def percent(self) -> int:
        return int(self.done * 100 / self.total) if self.total else 100


Progress = Callable[[BroadcastResult], Awaitable[None]]


class BroadcastService:
    def __init__(self, bot: Bot, db: Database, users: UserService) -> None:
        self.bot, self.db, self.users = bot, db, users
        self.running = False
        self.last: BroadcastResult | None = None
        self._stop = False

    # ----- audience -----
    async def audience_ids(self) -> list[int]:
        rows = await self.db.pool.fetch(
            "SELECT user_id FROM users WHERE NOT is_banned AND NOT is_blocked ORDER BY user_id")
        return [r["user_id"] for r in rows]

    async def audience_count(self) -> int:
        return await self.db.pool.fetchval(
            "SELECT COUNT(*) FROM users WHERE NOT is_banned AND NOT is_blocked")

    # ----- control -----
    def try_begin(self) -> bool:
        """Claim the single broadcast slot. False = one is already running."""
        if self.running:
            return False
        self.running, self._stop = True, False
        return True

    def stop(self) -> None:
        self._stop = True

    # ----- sending -----
    async def send_one(self, user_id: int, text: str) -> str:
        """Returns 'sent', 'blocked' or 'failed'."""
        for _ in range(3):
            try:
                await self.bot.send_message(user_id, text, parse_mode="HTML",
                                            reply_markup=card_kb())
                return "sent"
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                await self.users.mark_blocked(user_id)
                return "blocked"
            except TelegramBadRequest as e:
                reason = str(e).lower()
                if "chat not found" in reason or "deactivated" in reason:
                    await self.users.mark_blocked(user_id)
                    return "blocked"
                return "failed"
            except TelegramAPIError:
                return "failed"
        return "failed"

    async def run(self, body: str, progress: Progress | None = None) -> BroadcastResult:
        """Call `try_begin()` first. Always releases the slot when done."""
        text = render(body)
        result = BroadcastResult()
        try:
            ids = await self.audience_ids()
            result.total = len(ids)
            last_report = time.monotonic()
            for uid in ids:
                if self._stop:
                    result.stopped = True
                    break
                outcome = await self.send_one(uid, text)
                setattr(result, outcome, getattr(result, outcome) + 1)
                await asyncio.sleep(SEND_DELAY)
                now = time.monotonic()
                if progress and (result.done % PROGRESS_EVERY == 0
                                 or now - last_report >= PROGRESS_SECONDS):
                    last_report = now
                    await progress(result)
        finally:
            result.seconds = time.monotonic() - result.started
            self.last = result
            self.running = False
            log_event(log, logging.INFO, "broadcast_finished", total=result.total,
                      sent=result.sent, blocked=result.blocked, failed=result.failed,
                      stopped=result.stopped)
        return result
