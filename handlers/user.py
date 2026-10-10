import logging
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

import ui
from config import Config
from logging_setup import log_event
from services.delivery_service import DeliveryService
from services.settings_service import SettingsService
from services.user_service import UserService
from services.video_service import VideoService

log = logging.getLogger("user")
router = Router(name="user")
HTML = "HTML"
CATS = ("new", "old")

# Kept under the old names for anything that still imports them.
TYPE_KB = ui.collection_kb()
AGAIN_KB = ui.back_kb()


# ------------------------------------------------------------------ helpers
async def _say(bot: Bot, chat_id: int, text: str, kb=None) -> Message | None:
    try:
        return await bot.send_message(chat_id, text, reply_markup=kb, parse_mode=HTML)
    except TelegramAPIError:
        return None


async def _edit(msg: Message | None, text: str, kb=None) -> None:
    if msg is None:
        return
    try:
        await msg.edit_text(text, reply_markup=kb, parse_mode=HTML)
    except TelegramAPIError:
        pass   # "message is not modified", message gone, ...


async def _alert(bot: Bot, chat_id: int, cb: CallbackQuery | None, text: str) -> None:
    """Tell the user something short: a popup for button taps, a message otherwise."""
    if cb is not None:
        try:
            await cb.answer(text, show_alert=True)
            return
        except TelegramAPIError:
            pass
    await _say(bot, chat_id, text)


# ------------------------------------------------------------------ the one delivery pipeline
async def run_request(bot: Bot, users: UserService, delivery: DeliveryService,
                      videos: VideoService, cfg: Config, *, user_id: int, chat_id: int,
                      category: str, count: int, shuffle: bool = False,
                      cb: CallbackQuery | None = None, locked: bool = False,
                      settings: SettingsService | None = None) -> None:
    """Lock -> rate limit -> unseen check -> deliver -> completion message.

    `locked=True` means the caller already moved the user into the 'delivering' state."""
    if not locked and not await users.acquire_delivery_lock(user_id):
        await _alert(bot, chat_id, cb, ui.BUSY_TEXT)
        return
    status: Message | None = None
    try:
        if not await users.hit_rate_limit(user_id, cfg.rate_limit_requests,
                                          cfg.rate_limit_window):
            log_event(log, logging.INFO, "rate_limited", user_id=user_id)
            await _alert(bot, chat_id, cb, ui.RATE_TEXT)
            return
        if cb is not None:
            try:
                await cb.answer()
            except TelegramAPIError:
                pass

        # One status message: reuse the pressed menu message, or send a fresh one.
        if cb is not None and isinstance(cb.message, Message):
            status = cb.message
            await _edit(status, ui.preparing_text())
        else:
            status = await _say(bot, chat_id, ui.preparing_text())

        unseen = await videos.count_unseen(user_id, category)
        if unseen == 0:
            log_event(log, logging.INFO, "category_exhausted", user_id=user_id, category=category)
            kb = ui.try_other_kb(category, count)
            await _edit(status, ui.exhausted_text(category), kb)
            if status is None:
                await _say(bot, chat_id, ui.exhausted_text(category), kb)
            return

        n = min(count, unseen)
        short_of = count if unseen < count else None
        await _edit(status, ui.preparing_n_text(n, category, short_of))
        if (settings is None or settings.flag("delete_notice_enabled")) \
                and await users.claim_delete_notice(user_id):
            await _say(bot, chat_id, ui.delete_notice_text(cfg, settings))
        if delivery.events is not None:
            await delivery.events.record(
                "user_request", f"{count} {category} video(s) requested"
                + (" (fresh batch)" if shuffle else ""), user_id)

        log_event(log, logging.INFO, "delivery_started", user_id=user_id, category=category,
                  requested=count, available=unseen, shuffle=shuffle)
        result = await delivery.deliver(user_id, chat_id, category, n, shuffle=shuffle)
    except Exception as exc:
        log.exception("delivery_crashed")
        if delivery.events is not None:
            await delivery.events.record("error", f"Delivery crashed: {type(exc).__name__}", user_id)
        await _say(bot, chat_id, ui.ERROR_TEXT, ui.retry_kb(category, count))
        return
    finally:
        await users.clear_state(user_id)

    if result.blocked:
        return
    if status is not None:       # the "preparing" line sits above the videos: remove it
        try:
            await status.delete()
        except TelegramAPIError:
            pass
    if result.delivered == 0:
        await _say(bot, chat_id, ui.FAILED_TEXT, ui.retry_kb(category, count))
        return
    kb = (ui.try_other_kb(category, count) if short_of
          else ui.after_delivery_kb(category, count, settings))
    await _say(bot, chat_id, ui.delivered_text(cfg, result.delivered, category, short_of), kb)


# ------------------------------------------------------------------ typed number
@router.message(F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def on_number(message: Message, bot: Bot, users: UserService, cfg: Config,
                    delivery: DeliveryService, videos: VideoService,
                    settings: SettingsService) -> None:
    await users.upsert(message.from_user)
    text = message.text.strip()
    count = int(text) if re.fullmatch(r"[0-9]{1,6}", text) else 0
    if count < 1 or count > cfg.max_videos_per_request:
        await message.answer(ui.invalid_text(cfg), parse_mode=HTML)
        return

    user_id = message.from_user.id
    state, data = await users.get_state(user_id)
    preset = data.get("category") if state == "waiting_for_video_count" else None
    if preset in CATS:   # came from the "New Videos" / "Old Videos" menu buttons
        await run_request(bot, users, delivery, videos, cfg, user_id=user_id,
                          chat_id=message.chat.id, category=preset, count=count,
                          settings=settings)
        return

    ok = await users.set_state(user_id, "waiting_for_category", {"count": count})
    if not ok:
        await message.answer(ui.BUSY_TEXT)
        return
    log_event(log, logging.INFO, "user_request", user_id=user_id, requested=count)
    await message.answer(ui.choose_collection_text(), reply_markup=ui.collection_kb(),
                         parse_mode=HTML)


# ------------------------------------------------------------------ collection chosen
@router.callback_query(F.data.in_({"type:new", "type:old"}))
async def on_type(cb: CallbackQuery, bot: Bot, users: UserService, delivery: DeliveryService,
                  videos: VideoService, cfg: Config, settings: SettingsService) -> None:
    user_id = cb.from_user.id
    category = cb.data.split(":", 1)[1]
    data = await users.claim_state(user_id, "waiting_for_category", "delivering")
    if data is None:
        await cb.answer(ui.EXPIRED_TEXT, show_alert=True)
        return
    count = int(data.get("count", 0))
    if count < 1:
        await users.clear_state(user_id)
        await cb.answer(ui.EXPIRED_TEXT, show_alert=True)
        return
    log_event(log, logging.INFO, "category_selected", user_id=user_id,
              category=category, requested=count)
    await run_request(bot, users, delivery, videos, cfg, user_id=user_id,
                      chat_id=cb.message.chat.id, category=category, count=count,
                      cb=cb, locked=True, settings=settings)


# ------------------------------------------------------------------ More / Fresh buttons
def _parse(data: str, cfg: Config) -> tuple[str, int] | None:
    parts = data.split(":")
    if len(parts) != 3 or parts[1] not in CATS or not parts[2].isdigit():
        return None
    return parts[1], max(1, min(int(parts[2]), cfg.max_videos_per_request))


@router.callback_query(F.data.startswith("more:") | F.data.startswith("fresh:"))
async def on_more(cb: CallbackQuery, bot: Bot, users: UserService, delivery: DeliveryService,
                  videos: VideoService, cfg: Config, settings: SettingsService) -> None:
    parsed = _parse(cb.data, cfg)
    if parsed is None:
        await cb.answer()
        return
    category, count = parsed
    fresh = cb.data.startswith("fresh:")
    # `more:` is also used by the retry / "try the other collection" buttons, so it stays
    # available; the 🔥 More buttons themselves are simply hidden when an admin turns them off.
    await run_request(bot, users, delivery, videos, cfg, user_id=cb.from_user.id,
                      chat_id=cb.message.chat.id, category=category, count=count,
                      shuffle=fresh, cb=cb, settings=settings)


@router.callback_query(F.data.in_({"mmore:new", "mmore:old"}))
async def on_menu_more(cb: CallbackQuery, bot: Bot, users: UserService,
                       delivery: DeliveryService, videos: VideoService, cfg: Config,
                       settings: SettingsService) -> None:
    """🔥 More New / ♻️ More Old from the main menu: same size as the user's last request."""
    if not settings.button_on("btn_more"):
        await cb.answer("This option is currently unavailable.", show_alert=True)
        return
    category = cb.data.split(":", 1)[1]
    last = await videos.last_request_size(cb.from_user.id)
    count = max(1, min(last or min(10, cfg.max_videos_per_request), cfg.max_videos_per_request))
    await run_request(bot, users, delivery, videos, cfg, user_id=cb.from_user.id,
                      chat_id=cb.message.chat.id, category=category, count=count, cb=cb,
                      settings=settings)
