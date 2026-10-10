import html

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

import ui
from config import Config
from services.settings_service import NO_PERMISSION_TEXT, SettingsService
from services.user_service import UserService
from services.video_service import VideoService

router = Router(name="start")
HTML = "HTML"
DISABLED_TEXT = "This option is currently unavailable."

# Telegram's built-in 🔥 message effect (plays a fire burst; private chats only).
FIRE_EFFECT_ID = "5104841245755180586"


async def _send_welcome(message: Message, cfg: Config, settings: SettingsService,
                        admin: bool) -> None:
    name = html.escape(message.from_user.first_name or "") or None
    text = ui.welcome_text(cfg, settings, name)
    markup = ui.welcome_kb(settings, admin)
    effect = FIRE_EFFECT_ID if settings.flag("welcome_animation") else None
    try:
        await message.answer(text, reply_markup=markup, parse_mode=HTML, message_effect_id=effect)
    except TelegramAPIError:
        if effect is None:
            raise
        # effect refused (old client / API): the welcome must still arrive
        await message.answer(text, reply_markup=markup, parse_mode=HTML)


def prompt_text(cfg: Config) -> str:   # kept for backwards compatibility
    return ui.welcome_text(cfg)


def _is_admin(user, cfg: Config) -> bool:
    return bool(user and user.id in cfg.admin_ids)


async def _show(cb: CallbackQuery, text: str, kb=None) -> None:
    """Edit the menu message in place; fall back to a new message."""
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode=HTML)
    except TelegramAPIError:
        try:
            await cb.message.answer(text, reply_markup=kb, parse_mode=HTML)
        except TelegramAPIError:
            pass


async def _enabled(cb: CallbackQuery, settings: SettingsService, *keys: str) -> bool:
    """False (with a popup) if an admin switched this public option off - the buttons are
    hidden too, but old messages may still carry them, so the server checks as well."""
    if any(settings.button_on(k) for k in keys):
        return True
    await cb.answer(DISABLED_TEXT, show_alert=True)
    return False


@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, users: UserService, cfg: Config,
                    settings: SettingsService) -> None:
    await users.upsert(message.from_user)
    await users.set_state(message.from_user.id, "waiting_for_video_count")
    admin = _is_admin(message.from_user, cfg)
    if not settings.flag("welcome_enabled"):      # admin switched the long welcome off
        await message.answer(ui.main_menu_text(), reply_markup=ui.main_menu_kb(settings, admin),
                             parse_mode=HTML)
        return
    await _send_welcome(message, cfg, settings, admin)


@router.message(Command("menu"), F.chat.type == "private")
async def cmd_menu(message: Message, users: UserService, cfg: Config,
                   settings: SettingsService) -> None:
    await users.upsert(message.from_user)
    await message.answer(ui.main_menu_text(),
                         reply_markup=ui.main_menu_kb(settings, _is_admin(message.from_user, cfg)),
                         parse_mode=HTML)


@router.message(Command("help"), F.chat.type == "private")
async def cmd_help(message: Message, cfg: Config, settings: SettingsService) -> None:
    await message.answer(ui.help_text(cfg, settings), reply_markup=ui.help_kb(settings),
                         parse_mode=HTML)


@router.message(Command("admin"), F.chat.type == "private")
async def cmd_admin_denied(message: Message) -> None:
    """Only reached by non-admins (the admin router answers admins first). The reply is the
    same for everybody who is not an admin and reveals nothing about who is."""
    await message.answer(NO_PERMISSION_TEXT)


@router.callback_query(F.data.startswith("adm:"))
async def on_admin_callback_denied(cb: CallbackQuery) -> None:
    await cb.answer(NO_PERMISSION_TEXT, show_alert=True)


@router.callback_query(F.data == "menu")
async def on_menu(cb: CallbackQuery, cfg: Config, settings: SettingsService) -> None:
    await cb.answer()
    await _show(cb, ui.main_menu_text(),
                ui.main_menu_kb(settings, _is_admin(cb.from_user, cfg)))


@router.callback_query(F.data == "menu:more")
async def on_menu_more_choice(cb: CallbackQuery, settings: SettingsService) -> None:
    if not await _enabled(cb, settings, "btn_more"):
        return
    await cb.answer()
    await _show(cb, "🔥 <b>More Videos</b>\n\nContinue with videos you haven't received before.",
                ui.more_kb())


@router.callback_query(F.data == "help")
async def on_help(cb: CallbackQuery, cfg: Config, settings: SettingsService) -> None:
    if not await _enabled(cb, settings, "btn_help"):
        return
    await cb.answer()
    await _show(cb, ui.help_text(cfg, settings), ui.help_kb(settings))


@router.callback_query(F.data == "dev")
async def on_developer(cb: CallbackQuery, settings: SettingsService) -> None:
    if not await _enabled(cb, settings, "btn_developer"):
        return
    await cb.answer()
    await _show(cb, ui.developer_text(settings), ui.developer_kb(settings))


@router.callback_query(F.data == "activity")
async def on_activity(cb: CallbackQuery, videos: VideoService, users: UserService,
                      settings: SettingsService) -> None:
    await cb.answer()
    await users.upsert(cb.from_user)
    activity = await videos.user_activity(cb.from_user.id)
    await _show(cb, ui.activity_text(activity), ui.back_kb(settings))


async def _ask_count(cb: CallbackQuery, users: UserService, cfg: Config,
                     category: str | None, fresh: bool) -> None:
    data = {"category": category} if category else None
    ok = await users.set_state(cb.from_user.id, "waiting_for_video_count", data)
    if not ok:
        await cb.answer(ui.BUSY_TEXT, show_alert=True)
        return
    await cb.answer()
    await _show(cb, ui.ask_count_text(cfg, category, fresh))


@router.callback_query(F.data == "menu:request")
async def on_menu_request(cb: CallbackQuery, users: UserService, cfg: Config,
                          settings: SettingsService) -> None:
    if not await _enabled(cb, settings, "btn_request", "btn_new", "btn_old"):
        return
    await _ask_count(cb, users, cfg, None, False)


@router.callback_query(F.data.in_({"menu:new", "menu:old"}))
async def on_menu_collection(cb: CallbackQuery, users: UserService, cfg: Config,
                             settings: SettingsService) -> None:
    category = cb.data.split(":", 1)[1]
    if not await _enabled(cb, settings, f"btn_{category}", "btn_request"):
        return
    await _ask_count(cb, users, cfg, category, False)


@router.callback_query(F.data == "again")
async def on_request_again(cb: CallbackQuery, users: UserService, cfg: Config) -> None:
    """🔄 New Request - starts over; the delivery history is NOT touched."""
    await _ask_count(cb, users, cfg, None, True)
