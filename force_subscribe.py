"""Force Subscribe: every non-admin interaction requires CURRENT membership of the channel.

How it works
  * ForceSubscribeGate is an outer middleware on dp.message and dp.callback_query. It runs
    AFTER AccessGate (ban / bot off / maintenance), so those rules keep priority.
  * For every private message and every button press from a non-admin it calls the Bot API
    method getChatMember LIVE. There is no whitelist and no membership cache: a user who
    leaves the channel is blocked on their very next interaction.
  * Only "creator", "administrator", "member" and "restricted" with is_member=True pass.
    "left", "kicked", restricted with is_member=False, Telegram errors, timeouts, network
    failures and malformed answers all DENY access (fail closed).
  * The "Verify Membership" button (callback data fsub:verify) is just a trigger: it re-runs
    the same server-side check using the id of the user who pressed it. The callback data
    carries no user id and no proof of anything.
  * Admins (ADMIN_IDS) bypass the check so maintenance is never locked out.

Channels are managed from the Admin Panel (📢 Force Subscribe Manager) and stored in the
force_sub_channels table. A user must be a CURRENT member of EVERY enabled channel. With no
enabled channel the gate lets everybody through.

Environment variables (all optional)
  FORCE_SUB_ENABLED   true/false master switch (default true)
  FORCE_SUB_CHANNEL / FORCE_SUB_URL / FORCE_SUB_NAME
                      only used ONCE, on the first start, to seed the list
                      (defaults: @TheRoyalZone, https://t.me/TheRoyalZone, The Royal Zone)
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import (CallbackQuery, InlineKeyboardButton as Btn,
                           InlineKeyboardMarkup as Kb, Message, TelegramObject)

import ui
from config import Config
from services.force_sub_service import Channel, ForceSubService
from services.settings_service import SettingsService
from services.user_service import UserService

log = logging.getLogger("forcesub")

VERIFY_DATA = "fsub:verify"
HTML = "HTML"
_OK_STATUSES = {"creator", "administrator", "member"}
_CHECK_TIMEOUT = 10.0       # seconds for one getChatMember call
_VERIFY_COOLDOWN = 1.5      # seconds between two presses of Verify by the same user


@dataclass(frozen=True)
class ForceSubConfig:
    enabled: bool
    channel: str | int
    url: str
    name: str


def load_force_sub_config() -> ForceSubConfig:
    raw = os.getenv("FORCE_SUB_ENABLED", "true").strip().lower()
    channel: str | int = os.getenv("FORCE_SUB_CHANNEL", "").strip() or "@TheRoyalZone"
    if isinstance(channel, str) and channel.lstrip("-").isdigit():
        channel = int(channel)
    elif isinstance(channel, str) and not channel.startswith("@"):
        channel = "@" + channel
    return ForceSubConfig(
        enabled=raw not in {"0", "false", "no", "off"},
        channel=channel,
        url=os.getenv("FORCE_SUB_URL", "").strip() or "https://t.me/TheRoyalZone",
        name=os.getenv("FORCE_SUB_NAME", "").strip() or "The Royal Zone",
    )


# ------------------------------------------------------------------ the real check
async def check_membership(bot: Bot, channel: str | int, user_id: int) -> bool | None:
    """True = currently a member, False = definitely not, None = could not be confirmed.

    Callers must treat None exactly like False for access purposes."""
    for attempt in (1, 2):
        try:
            member = await asyncio.wait_for(bot.get_chat_member(channel, user_id),
                                            timeout=_CHECK_TIMEOUT)
        except TelegramRetryAfter as exc:
            if attempt == 1 and exc.retry_after <= 3:
                await asyncio.sleep(exc.retry_after + 0.2)
                continue
            log.warning("getChatMember rate limited (retry_after=%s)", exc.retry_after)
            return None
        except TelegramAPIError as exc:
            # bot not admin of the channel, channel not found, user id invalid, network error ...
            log.error("getChatMember failed: %s: %s", type(exc).__name__, exc)
            return None
        except asyncio.TimeoutError:
            log.error("getChatMember timed out")
            return None
        except Exception:  # noqa: BLE001 - never let an unexpected error grant access
            log.exception("getChatMember unexpected error")
            return None

        status = getattr(member, "status", None)
        status = getattr(status, "value", status)       # enum or plain string
        if status in _OK_STATUSES:
            return True
        if status == "restricted":
            is_member = getattr(member, "is_member", None)
            return True if is_member is True else False
        if status in {"left", "kicked"}:
            return False
        log.error("getChatMember returned an unknown status: %r", status)
        return None
    return None


async def check_all(bot: Bot, channels: list[Channel], user_id: int
                    ) -> tuple[bool | None, list[tuple[Channel, bool | None]]]:
    """Live check of every channel (in parallel). Overall result:
    True only if the user is a member of ALL of them, False if at least one says "not a member",
    None if nothing says False but at least one could not be confirmed."""
    results = await asyncio.gather(*(check_membership(bot, c.chat_id, user_id) for c in channels))
    pairs = list(zip(channels, results))
    if all(r is True for r in results):
        return True, pairs
    if any(r is False for r in results):
        return False, pairs
    return None, pairs


async def startup_check(bot: Bot, fs: ForceSubConfig, service: ForceSubService) -> None:
    """Log (never raise) whether the bot can read each channel's member list."""
    if not fs.enabled:
        log.info("Force subscribe is disabled (FORCE_SUB_ENABLED=false)")
        return
    channels = service.enabled()
    if not channels:
        log.warning("Force subscribe: no channel configured - everybody passes. "
                    "Add one in Admin Panel -> Force Subscribe Manager.")
        return
    me = await bot.get_me()
    for c in channels:
        status = await bot_channel_status(bot, c.chat_id, me.id)
        if status in {"creator", "administrator"}:
            log.info("✅ Force subscribe active for %s (bot is %s there)", c.title, status)
        else:
            log.error("❌ Force subscribe: the bot is NOT an administrator of %s (status=%s). "
                      "Every non-admin user will be blocked until that is fixed.", c.title, status)


async def bot_channel_status(bot: Bot, chat_id: int, bot_id: int) -> str:
    """The bot's own status in the channel ("administrator", "left", ... or "error")."""
    try:
        member = await asyncio.wait_for(bot.get_chat_member(chat_id, bot_id), timeout=_CHECK_TIMEOUT)
        status = getattr(member, "status", None)
        return str(getattr(status, "value", status))
    except Exception:  # noqa: BLE001
        return "error"


# ------------------------------------------------------------------ UI
def _name(c: Channel) -> str:
    return html.escape(c.title)


def restricted_text(kind: str, pairs: list[tuple[Channel, bool | None]]) -> str:
    multi = len(pairs) > 1
    names = ", ".join(_name(c) for c, _ in pairs)
    listing = ""
    if multi:
        listing = "\n".join(f"{'✅' if r is True else '❌'} {_name(c)}" for c, r in pairs) + "\n\n"
    one = _name(pairs[0][0]) if pairs else "our channel"
    what = "channels" if multi else "channel"
    if kind == "failed":
        return (f"⚠️ <b>MEMBERSHIP NOT VERIFIED</b>\n\n"
                + (f"You have not joined every required channel yet:\n\n{listing}" if multi else
                   f"You are not currently a verified member of <b>{one}</b>.\n\n")
                + f"Please join the {what} and press <b>Verify Membership</b> again.")
    if kind == "left":
        return (f"🔒 <b>ACCESS RESTRICTED</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"👑 <b>MEMBERSHIP NO LONGER ACTIVE</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n\n"
                f"You are no longer a member of {'all required channels' if multi else f'<b>{one}</b>'}, "
                f"so bot access has been paused.\n\n{listing}"
                f"📌 <b>Membership Status:</b> Not Verified\n\n"
                f"👇 Rejoin the {what} and verify your membership to continue.")
    if kind == "error":
        return (f"⚠️ <b>VERIFICATION UNAVAILABLE</b>\n\n"
                f"We could not confirm your membership of <b>{names}</b> right now.\n\n"
                f"Please try again in a moment.")
    title = "JOIN OUR CHANNELS" if multi else f"WELCOME TO {one.upper()}"
    return (f"🔒 <b>ACCESS RESTRICTED</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👑 <b>{title}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"To use this bot, you must join our official Telegram {what} first.\n\n"
            f"{listing}"
            f"✨ Get updates and unlock all bot features.\n\n"
            f"📌 <b>Membership Status:</b> Not Verified\n\n"
            f"👇 Join the {what} and verify your membership to continue.")


def restricted_kb(kind: str, pairs: list[tuple[Channel, bool | None]]) -> Kb:
    missing = [c for c, r in pairs if r is not True] or [c for c, _ in pairs]
    if len(missing) == 1 and kind in {"failed", "error"}:
        return Kb(inline_keyboard=[[Btn(text="📢 Join Channel", url=missing[0].url),
                                    Btn(text="🔄 Verify Again", callback_data=VERIFY_DATA)]])
    rows = [[Btn(text=f"📢 Join {c.title}"[:60], url=c.url)] for c in missing]
    again = kind in {"failed", "error"}
    rows.append([Btn(text="🔄 Verify Again" if again else "✅ Verify Membership",
                     callback_data=VERIFY_DATA)])
    return Kb(inline_keyboard=rows)


# ------------------------------------------------------------------ middleware
class ForceSubscribeGate(BaseMiddleware):
    def __init__(self, cfg: Config, settings: SettingsService, users: UserService,
                 fs: ForceSubConfig, service: ForceSubService) -> None:
        self.cfg = cfg
        self.settings = settings
        self.users = users
        self.fs = fs
        self.service = service
        # user -> chat_ids they passed last time; ONLY used to word the "membership no longer
        # active" screen. It never grants access.
        self._passed: dict[int, frozenset[int]] = {}
        self._last_verify: dict[int, float] = {}

    def _active(self) -> list[Channel]:
        return self.service.enabled() if self.fs.enabled else []

    # -- entry point -------------------------------------------------------
    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        channels = self._active()
        if not channels:
            return await handler(event, data)
        user = getattr(event, "from_user", None)
        bot: Bot = data["bot"]

        # The Verify button: handled here for everybody, always using the presser's real id.
        if isinstance(event, CallbackQuery) and event.data == VERIFY_DATA:
            if user is None or user.is_bot:
                return None
            if user.id in self.cfg.admin_ids:
                await self._safe_answer(event, "✅ Administrators do not need verification.")
                return None
            return await self._verify(event, bot, channels)

        if user is None or user.is_bot or user.id in self.cfg.admin_ids:
            return await handler(event, data)
        if isinstance(event, Message):
            if event.chat.type != "private":
                return await handler(event, data)
            if (event.text or "").lower().split("@")[0].split(" ")[0] == "/admin":
                return await handler(event, data)      # answered with the permission notice
        if isinstance(event, CallbackQuery) and (event.data or "").startswith("adm:"):
            return await handler(event, data)          # rejected by the admin-callback guard

        overall, pairs = await check_all(bot, channels, user.id)
        if overall is True:
            self._remember(user.id, channels)
            return await handler(event, data)

        if overall is False:
            kind = "left" if self._left_something(user.id, pairs) else "new"
            self._passed.pop(user.id, None)            # next time: back to the normal screen
        else:
            kind = "error"
        await self._block(event, kind, pairs)
        return None

    # -- verify button -----------------------------------------------------
    async def _verify(self, cb: CallbackQuery, bot: Bot, channels: list[Channel]) -> None:
        uid = cb.from_user.id
        now = time.monotonic()
        if now - self._last_verify.get(uid, 0.0) < _VERIFY_COOLDOWN:
            await self._safe_answer(cb, "⏳ Please wait a moment…")
            return
        self._last_verify[uid] = now
        if len(self._last_verify) > 5000:              # keep the dict small
            cutoff = now - 60
            self._last_verify = {k: v for k, v in self._last_verify.items() if v > cutoff}

        overall, pairs = await check_all(bot, channels, uid)
        if overall is True:
            self._remember(uid, channels)
            await self._safe_answer(cb, "✅ Membership verified!")
            try:
                await self.users.upsert(cb.from_user)
            except Exception:  # noqa: BLE001
                log.warning("upsert_failed_after_verify")
            await self._show(cb, ui.main_menu_text(), ui.main_menu_kb(self.settings, False))
            return
        if overall is False:
            await self._safe_answer(cb, "❌ You have not joined the channel yet." if len(pairs) == 1
                                    else "❌ You have not joined every channel yet.", alert=True)
            await self._show(cb, restricted_text("failed", pairs), restricted_kb("failed", pairs))
        else:
            await self._safe_answer(cb, "⚠️ Could not verify right now. Please try again.",
                                    alert=True)
            await self._show(cb, restricted_text("error", pairs), restricted_kb("error", pairs))

    # -- helpers -----------------------------------------------------------
    def _remember(self, uid: int, channels: list[Channel]) -> None:
        self._passed[uid] = frozenset(c.chat_id for c in channels)
        if len(self._passed) > 20000:                  # bounded memory
            self._passed.clear()

    def _left_something(self, uid: int, pairs: list[tuple[Channel, bool | None]]) -> bool:
        before = self._passed.get(uid, frozenset())
        return any(r is False and c.chat_id in before for c, r in pairs)

    async def _block(self, event: TelegramObject, kind: str,
                     pairs: list[tuple[Channel, bool | None]]) -> None:
        text, kb = restricted_text(kind, pairs), restricted_kb(kind, pairs)
        if isinstance(event, CallbackQuery):
            await self._safe_answer(event, "🔒 Join the channel and verify to continue.",
                                    alert=True)
            await self._show(event, text, kb)
        elif isinstance(event, Message):
            try:
                await event.answer(text, reply_markup=kb, parse_mode=HTML)
            except TelegramAPIError:
                pass

    @staticmethod
    async def _safe_answer(cb: CallbackQuery, text: str, alert: bool = False) -> None:
        try:
            await cb.answer(text[:190], show_alert=alert)
        except TelegramAPIError:
            pass

    @staticmethod
    async def _show(cb: CallbackQuery, text: str, kb: Kb) -> None:
        msg = cb.message
        if not isinstance(msg, Message):
            return
        try:
            await msg.edit_text(text, reply_markup=kb, parse_mode=HTML)
        except TelegramAPIError as exc:
            if "not modified" in str(exc).lower():
                return
            try:
                await msg.answer(text, reply_markup=kb, parse_mode=HTML)
            except TelegramAPIError:
                pass
