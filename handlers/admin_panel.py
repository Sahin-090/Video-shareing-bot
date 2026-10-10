"""Inline-button Admin Panel (`/admin`).

Separate from the public UI: every update is checked against ADMIN_IDS on the server (router
filters AND a second check inside the panel). Normal users get `⛔ You don't have permission ...`
from handlers/start.py and never see an admin button.

Navigation and "what is the admin typing right now" live in the database (user_states), so
there is no Redis and a restart only drops an unfinished edit. Admin edits never touch the
delivery lock of normal users: they use their own `admin_input` / `admin_preview` states.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup as Kb, Message

import ui
from config import Config
from database import Database
from handlers.fsub_panel import ForceSubPanelMixin
from logging_setup import log_event
from services.bridge_service import BridgeService
from services.broadcast_service import BroadcastResult, BroadcastService, MAX_BODY, card_kb, render
from services.event_service import EventService, GROUPS, KINDS
from services.scanner_service import ScanBusy, ScannerService, ScanReport
from services.settings_service import (CUSTOM_ACTIONS, NO_PERMISSION_TEXT, ORDERS, PUBLIC_BUTTONS,
                                       SettingsService, ValidationError, sanitize_html,
                                       validate_github, validate_instagram, validate_telegram,
                                       validate_url)
from services.user_service import UserService
from services.video_service import VideoService

log = logging.getLogger("admin_panel")
router = Router(name="admin_panel")
HTML = "HTML"
LINE = ui.LINE
AUTO_DELETE_PRESETS = (2, 5, 10, 15, 20)   # minutes offered as one-tap buttons
PAGE_VIDEOS, PAGE_USERS, PAGE_LOGS = 8, 8, 10
esc = ui.html_escape


async def is_admin(event, cfg: Config) -> bool:
    user = getattr(event, "from_user", None)
    return bool(user and user.id in cfg.admin_ids)


router.message.filter(F.chat.type == "private", is_admin)
router.callback_query.filter(is_admin)


# --------------------------------------------------------------------- small helpers
def n(value) -> str:
    return f"{int(value or 0):,}"


IST = timezone(timedelta(hours=5, minutes=30), "IST")   # Asia/Kolkata (no DST)


def _ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:                      # naive timestamps are stored as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def when(dt: datetime | None) -> str:
    """Kolkata time, 12-hour clock: 2026-10-08 07:47 PM IST."""
    return f"{_ist(dt):%Y-%m-%d %I:%M %p} IST" if dt else "—"


ONLINE_WINDOW = timedelta(minutes=5)   # active within this window = "online"


def is_online(dt: datetime | None) -> bool:
    if not dt:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - dt <= ONLINE_WINDOW


def presence(dt: datetime | None) -> str:
    return "🟢 Online" if is_online(dt) else "🔴 Offline"


def ago(dt: datetime | None) -> str:
    if not dt:
        return ""
    now = datetime.now(timezone.utc)
    secs = max(0, int((now - (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))).total_seconds()))
    if secs < 60:
        return "just now"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if secs >= size:
            return f"{secs // size}{unit} ago"
    return ""


def nav(back: str = "adm:home") -> list[B]:
    """Every admin page ends with these two buttons."""
    return [B(text="⬅️ Back", callback_data=back), B(text="🏠 Dashboard", callback_data="adm:home")]


def kb(*rows: list[B], back: str | None = "adm:home") -> Kb:
    rows = list(rows)
    if back is not None:
        rows.append(nav(back))
    return Kb(inline_keyboard=rows)


def pairs(buttons: list[B]) -> list[list[B]]:
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def on_off(value: bool) -> str:
    return "✅ ON" if value else "❌ OFF"


def short(text: str | None, size: int = 28) -> str:
    return esc((text or "").replace("\n", " ").strip()[:size])


# --------------------------------------------------------------------- editable fields
def _plain(label: str, max_len: int, escape: bool = False) -> Callable[[str], str]:
    def parse(raw: str) -> str:
        value = raw.strip()
        if not value:
            raise ValidationError(f"{label} cannot be empty.")
        if len(value) > max_len:
            raise ValidationError(f"{label} is too long ({len(value)}/{max_len} characters).")
        return html.escape(value, quote=False) if escape else value
    return parse


def _emoji(raw: str) -> str:
    value = raw.strip()
    if not value or len(value) > 8 or re.search(r"\s", value) or any(c.isalnum() for c in value):
        raise ValidationError("Send a single emoji (no letters, digits or spaces).")
    return value


def _number(label: str, low: int, high: int) -> Callable[[str], str]:
    def parse(raw: str) -> str:
        if not re.fullmatch(r"[0-9]{1,7}", raw.strip()):
            raise ValidationError(f"Send a whole number between {low} and {high}.")
        value = int(raw)
        if not low <= value <= high:
            raise ValidationError(f"{label} must be between {low} and {high}.")
        return str(value)
    return parse


def _duration(label: str, low_s: int, high_s: int) -> Callable[[str], str]:
    """Minutes by default: `5` = 5 minutes, `2.5` = 2.5 minutes, `30s` = 30 seconds, `1h` = 1 hour.
    The value is stored in seconds (that is what the delivery queue uses)."""
    def parse(raw: str) -> str:
        m = re.fullmatch(r"([0-9]+(?:[.,][0-9]+)?)\s*(s|sec|m|min|h|hr)?", raw.strip().lower())
        if not m:
            raise ValidationError("Send minutes, e.g. 5 (= 5 minutes). You can also use 30s or 1h.")
        amount = float(m.group(1).replace(",", "."))
        unit = (m.group(2) or "m")[0]
        seconds = round(amount * {"s": 1, "m": 60, "h": 3600}[unit])
        if not low_s <= seconds <= high_s:
            raise ValidationError(f"{label} must be between {low_s} seconds and "
                                  f"{high_s // 3600} hours.")
        return str(seconds)
    return parse


@dataclass
class Field:
    label: str
    parse: Callable[[str], str]
    back: str                      # page the admin returns to
    hint: str = ""
    preview: bool = False          # show the rendered result and ask before saving
    optional: bool = False         # "-" clears the value
    enables: str = ""              # setting switched ON together with this one


_FMT = ("Allowed formatting: <code>&lt;b&gt;bold&lt;/b&gt;</code>, "
        "<code>&lt;i&gt;italic&lt;/i&gt;</code>, <code>&lt;u&gt;underline&lt;/u&gt;</code>, "
        "<code>&lt;code&gt;code&lt;/code&gt;</code>. Everything else is shown as plain text.")
_PH = "Placeholders: <code>{auto_delete}</code> and <code>{max}</code>."

FIELDS: dict[str, Field] = {
    "welcome_message": Field("Welcome message", lambda r: sanitize_html(r, 3000), "adm:pub:welcome",
                             f"{_FMT}\n{_PH}\nSend <code>default</code> to restore the built-in text.",
                             preview=True),
    "help_message": Field("Help message", lambda r: sanitize_html(r, 3000), "adm:pub:help",
                          f"{_FMT}\n{_PH}\nSend <code>default</code> to restore the built-in text.",
                          preview=True),
    "delete_notice": Field("Auto-delete notice", lambda r: sanitize_html(r, 600), "adm:pub:notice",
                           f"{_FMT}\n{_PH}\nSend <code>default</code> to restore the built-in text.",
                           preview=True),
    "maintenance_title": Field("Maintenance title", _plain("Title", 40, escape=True), "adm:mnt",
                               "Up to 40 characters.", preview=True, enables="maintenance_custom"),
    "maintenance_message": Field("Maintenance message", lambda r: sanitize_html(r, 800), "adm:mnt",
                                 f"Up to 800 characters.\n{_FMT}", preview=True,
                                 enables="maintenance_custom"),
    "maintenance_emoji": Field("Maintenance emoji", _emoji, "adm:mnt", "Send one emoji.",
                               preview=True, enables="maintenance_custom"),
    "developer_name": Field("Developer name", _plain("Name", 40), "adm:dev", "Up to 40 characters."),
    "developer_bio": Field("Developer bio", lambda r: sanitize_html(r, 300), "adm:dev",
                           f"Up to 300 characters. Send <code>-</code> to clear.\n{_FMT}",
                           preview=True, optional=True),
    "developer_website": Field("Website", validate_url, "adm:dev",
                               "Send a link like https://example.com — <code>-</code> clears it.",
                               optional=True),
    "developer_telegram": Field("Telegram", validate_telegram, "adm:dev",
                                "Send a username (@name) or a t.me link — <code>-</code> clears it.",
                                optional=True),
    "developer_instagram": Field("Instagram", validate_instagram, "adm:dev",
                                 "Send a username or an instagram.com link — <code>-</code> clears it.",
                                 optional=True),
    "developer_github": Field("GitHub", validate_github, "adm:dev",
                              "Send a username or a github.com link — <code>-</code> clears it.",
                              optional=True),
    "max_videos_per_request": Field("Maximum videos per request", _number("Maximum", 1, 200),
                                    "adm:set", "Send a number from 1 to 200."),
    "auto_delete_seconds": Field("Auto-delete time", _duration("Auto-delete time", 5, 86400),
                                 "adm:adel", "Send the time in <b>minutes</b>, e.g. <code>5</code> "
                                            "= 5 minutes, <code>0.5</code> = 30 seconds "
                                            "(also <code>45s</code> or <code>2h</code>). "
                                            "Allowed: 5 seconds to 24 hours."),
    "delivery_batch_size": Field("Delivery batch size", _number("Batch size", 1, 50), "adm:set",
                                 "Videos sent before a short pause (1–50)."),
    "rate_limit_requests": Field("User rate limit (requests per window)", _number("Limit", 1, 100),
                                 "adm:set", "Requests one user may make per rate-limit window "
                                            "(1–100)."),
}

# Switches an admin may flip, and the page code a switch was pressed on (to re-render it).
TOGGLES = {"welcome_enabled", "welcome_animation", "keep_source_caption", "delete_notice_enabled",
           "allow_duplicate_videos",
           "maintenance_custom", *PUBLIC_BUTTONS}
ORIGINS = {"s": "adm:set", "b": "adm:btns", "d": "adm:dev", "m": "adm:mnt",
           "w": "adm:pub:welcome", "n": "adm:pub:notice"}
ORDER_LABELS = {
    "new_video_order": {"desc": "Newest first", "asc": "Oldest first", "random": "Random"},
    "old_video_order": {"asc": "Oldest first", "desc": "Newest first",
                        "random": "Random"},
}
TEXT_PAGES = {  # key -> (title, icon)
    "welcome_message": ("Welcome message", "👋"),
    "help_message": ("Help message", "❓"),
    "delete_notice": ("Auto-delete notice", "⏱️"),
}
PAGE_OF = {"welcome_message": "welcome", "help_message": "help", "delete_notice": "notice"}


# --------------------------------------------------------------------- the panel
class Panel(ForceSubPanelMixin):
    def __init__(self, bot: Bot, cfg: Config, db: Database, users: UserService,
                 videos: VideoService, scanner: ScannerService, bridge: BridgeService,
                 settings: SettingsService, events: EventService, force_sub=None) -> None:
        self.bot, self.cfg, self.db = bot, cfg, db
        self.force_sub = force_sub       # ForceSubService (📢 Force Subscribe Manager)
        self._fsub_health: dict = {}
        self.users, self.videos, self.scanner, self.bridge = users, videos, scanner, bridge
        self.settings, self.events = settings, events
        self.broadcast = BroadcastService(bot, db, users)
        self._scan_msg: tuple[int, int] | None = None   # progress message that may be edited
        self._tasks: set[asyncio.Task] = set()

    # ----- output -----
    async def _show(self, cb: CallbackQuery, text: str, markup: Kb | None = None) -> None:
        try:
            await cb.message.edit_text(text, reply_markup=markup, parse_mode=HTML)
        except TelegramAPIError as e:
            if "not modified" in str(e).lower():
                return
            try:
                await cb.message.answer(text, reply_markup=markup, parse_mode=HTML)
            except TelegramAPIError:
                pass

    async def _send(self, message: Message, text: str, markup: Kb | None = None) -> None:
        try:
            await message.answer(text, reply_markup=markup, parse_mode=HTML)
        except TelegramAPIError:
            pass

    async def _log(self, uid: int, text: str) -> None:
        log_event(log, logging.INFO, "admin_action", admin_id=uid, action=text[:80])
        await self.events.record("admin", f"Admin {uid}: {text}")

    async def _put(self, key: str, value: str) -> None:
        await self.settings.set(key, value)
        self.settings.apply_runtime(self.videos)

    # ----- entry points -----
    async def open_dashboard(self, message: Message) -> None:
        if await self._is_panel_state(message.from_user.id):
            await self.users.clear_state(message.from_user.id)   # drop an unfinished edit
        text, markup = await self.page("adm:home")
        await self._send(message, text, markup)

    async def _is_panel_state(self, uid: int) -> bool:
        state, _ = await self.users.get_state(uid)
        return state in ("admin_input", "admin_preview")

    async def open_broadcast(self, message: Message) -> None:
        if await self._is_panel_state(message.from_user.id):
            await self.users.clear_state(message.from_user.id)
        text, markup = await self.page("adm:bc")
        await self._send(message, text, markup)

    async def handle_callback(self, cb: CallbackQuery) -> None:
        if cb.from_user.id not in self.cfg.admin_ids:    # second, independent server-side check
            await cb.answer(NO_PERMISSION_TEXT, show_alert=True)
            return
        parts = (cb.data or "").split(":")
        route, args = (parts[1] if len(parts) > 1 else "home"), parts[2:]
        if (self._scan_msg and cb.message and cb.message.message_id == self._scan_msg[1]
                and not (route == "scn" and args[:1] == ["stop"])):
            self._scan_msg = None            # the admin moved on: stop editing that message
        handler = getattr(self, f"_r_{route}", None)
        if handler is None:
            await cb.answer()
            return
        await handler(cb, args)

    async def _view(self, cb: CallbackQuery) -> None:
        text, markup = await self.page(cb.data)
        await cb.answer()
        await self._show(cb, text, markup)

    # ----- page dispatcher: path -> (text, keyboard) -----
    async def page(self, path: str) -> tuple[str, Kb]:
        parts = path.split(":")
        route, a = (parts[1] if len(parts) > 1 else "home"), parts[2:]
        sub = a[0] if a else ""
        if route == "fsub":
            return await self.p_fsub(a)
        if route == "stats":
            return await self.p_stats()
        if route == "vid":
            if sub == "add":
                return self.p_vid_add()
            if sub == "list":
                return await self.p_vid_list(a[1] if len(a) > 1 else "all",
                                             int(a[2]) if len(a) > 2 and a[2].isdigit() else 1)
            if sub == "view" and len(a) > 1 and a[1].isdigit():
                return await self.p_vid_view(int(a[1]))
            return await self.p_vid()
        if route == "usr":
            if sub == "recent":
                return await self.p_usr_recent(int(a[1]) if len(a) > 1 and a[1].isdigit() else 1)
            if sub == "stats":
                return await self.p_usr_stats()
            if sub == "view" and len(a) > 1 and re.fullmatch(r"-?\d+", a[1]):
                return await self.p_usr_view(int(a[1]))
            return self.p_usr()
        if route == "scn":
            return await self.p_scn()
        if route == "bc":
            return await self.p_bc()
        if route == "bot":
            return self.p_bot()
        if route == "mnt":
            return self.p_mnt(preview=sub == "prev")
        if route == "pub":
            if sub in PAGE_OF.values():
                key = next(k for k, v in PAGE_OF.items() if v == sub)
                return self.p_text(key, preview=len(a) > 1 and a[1] == "prev")
            return self.p_pub()
        if route == "btns":
            return self.p_btns()
        if route == "cust":
            return self.p_cust_actions() if sub == "act" else self.p_cust()
        if route == "dev":
            return self.p_dev(preview=sub == "prev")
        if route == "set":
            return self.p_set()
        if route == "adel":
            return self.p_adel()
        if route == "log":
            return await self.p_log(a[0] if a and a[0] in GROUPS else "all",
                                    int(a[1]) if len(a) > 1 and a[1].isdigit() else 1)
        if route == "ask":
            return await self.p_ask(a)
        return await self.p_home()

    # ================================================================== PAGES
    async def p_home(self) -> tuple[str, Kb]:
        d = await self.videos.dashboard()
        icon, word = self.settings.status_label().split(" ", 1)
        text = (f"🛠️ <b>VIDEOSHARE ADMIN PANEL</b>\n\n{LINE}\n\n"
                f"{icon} Bot Status: <b>{word}</b>\n\n"
                f"🎬 Videos: <b>{n(d['total'])}</b>\n\n"
                f"👥 Users: <b>{n(d['users'])}</b>\n\n"
                f"📨 Deliveries: <b>{n(d['deliveries'])}</b>\n\n"
                f"🆕 New: <b>{n(d['new'])}</b>\n\n"
                f"📦 Old: <b>{n(d['old'])}</b>\n\n{LINE}")
        rows = pairs([
            B(text="📊 Statistics", callback_data="adm:stats"),
            B(text="🎬 Video Manager", callback_data="adm:vid"),
            B(text="👥 User Manager", callback_data="adm:usr"),
            B(text="📢 Broadcast", callback_data="adm:bc"),
            B(text="📡 Channel Scanner", callback_data="adm:scn"),
            B(text="🤖 Bot Control", callback_data="adm:bot"),
            B(text="🛠️ Maintenance", callback_data="adm:mnt"),
            B(text="🎨 Public Settings", callback_data="adm:pub"),
            B(text="👨‍💻 Developer Settings", callback_data="adm:dev"),
            B(text="⚙️ Bot Settings", callback_data="adm:set"),
            B(text="📋 Logs", callback_data="adm:log"),
        ])
        rows.append([B(text="📢 Force Subscribe Manager", callback_data="adm:fsub")])
        rows.append([B(text="🏠 Main Menu", callback_data="menu")])
        return text, Kb(inline_keyboard=rows)

    async def p_stats(self) -> tuple[str, Kb]:
        d = await self.videos.dashboard()
        lines = [
            "📊 <b>STATISTICS</b>", "", LINE, "",
            f"🎬 Total videos: <b>{n(d['total'])}</b>",
            f"🆕 New videos: <b>{n(d['new'])}</b>",
            f"📦 Old videos: <b>{n(d['old'])}</b>", "",
            f"👥 Total users: <b>{n(d['users'])}</b>",
            f"🟢 Active users (24h): <b>{n(d['active_24h'])}</b>",
            f"🟢 Active users (7 days): <b>{n(d['active_7d'])}</b>", "",
            f"📨 Total deliveries: <b>{n(d['deliveries'])}</b>",
            f"♻️ Duplicate requests prevented: <b>{n(d['prevented'])}</b>",
            f"🗑️ Auto-deleted videos: <b>{n(d['auto_deleted'])}</b>",
            f"⚠️ Failed deliveries: <b>{n(d['failed'])}</b>", "",
            f"🔎 Total scans: <b>{n(d['scans'])}</b>",
            f"➕ Videos added (24h / 7 days): <b>{n(d['added_24h'])} / {n(d['added_7d'])}</b>",
        ]
        extra = []
        if d["pending"]:
            extra.append(f"⏳ Waiting for a category: {n(d['pending'])}")
        if d["inactive"]:
            extra.append(f"⚠️ Deactivated videos: {n(d['inactive'])}")
        if d["banned"]:
            extra.append(f"🚫 Blocked users: {n(d['banned'])}")
        if extra:
            lines += [""] + extra
        return "\n".join(lines), kb([B(text="🔄 Refresh", callback_data="adm:stats")])

    # ----- video manager -----
    async def p_vid(self) -> tuple[str, Kb]:
        d = await self.videos.dashboard()
        text = (f"🎬 <b>VIDEO MANAGER</b>\n\n{LINE}\n\n"
                f"🎬 Total: <b>{n(d['total'])}</b>   🆕 {n(d['new'])}   📦 {n(d['old'])}\n"
                + (f"⏳ Waiting for a category: {n(d['pending'])}\n" if d["pending"] else "")
                + (f"⚠️ Deactivated: {n(d['inactive'])}\n" if d["inactive"] else ""))
        rows = pairs([
            B(text="➕ Add Video", callback_data="adm:vid:add"),
            B(text="🔎 Search Video", callback_data="adm:vid:search"),
            B(text="📋 List Videos", callback_data="adm:vid:list:all:1"),
            B(text="🆕 New Videos", callback_data="adm:vid:list:new:1"),
            B(text="📦 Old Videos", callback_data="adm:vid:list:old:1"),
            B(text="🗑️ Delete Video", callback_data="adm:vid:del"),
            B(text="✏️ Edit Video", callback_data="adm:vid:edit"),
            B(text="🔄 Rescan Channel", callback_data="adm:vid:rescan"),
        ])
        return text, kb(*rows)

    def p_vid_add(self) -> tuple[str, Kb]:
        text = ("➕ <b>ADD VIDEO</b>\n\n"
                "Send or forward a video to this chat. I'll ask you to choose 🆕 New or 📦 Old.\n\n"
                "Adding many? Send <code>/batch new</code> or <code>/batch old</code> first and "
                "<code>/batch off</code> when you're done.\n\n"
                "Videos already in the channel are found by 🔄 Rescan Channel.")
        return text, kb(back="adm:vid")

    def _video_rows(self, rows) -> list[list[B]]:
        out = []
        for v in rows:
            flag = "" if v.is_active else "⚠️ "
            cat = {"new": "🆕", "old": "📦"}.get(v.category or "", "⏳")
            out.append([B(text=f"{flag}{cat} #{v.video_number or '-'} · ID {v.id} · "
                               f"{(v.caption or '').replace(chr(10), ' ')[:18]}".strip(" ·"),
                          callback_data=f"adm:vid:view:{v.id}")])
        return out

    async def p_vid_list(self, scope: str, page: int) -> tuple[str, Kb]:
        scope = scope if scope in ("all", "new", "old", "pending") else "all"
        total = await self.videos.count(scope)
        pages = max(1, (total + PAGE_VIDEOS - 1) // PAGE_VIDEOS)
        page = min(max(1, page), pages)
        rows = await self.videos.list_videos(scope, PAGE_VIDEOS, (page - 1) * PAGE_VIDEOS)
        title = {"all": "📋 ALL VIDEOS", "new": "🆕 NEW VIDEOS", "old": "📦 OLD VIDEOS",
                 "pending": "⏳ PENDING VIDEOS"}[scope]
        text = f"<b>{title}</b>\n\nPage {page}/{pages} · {n(total)} total"
        if not rows:
            text += "\n\nNo videos found."
        pager = []
        if page > 1:
            pager.append(B(text="◀️ Prev", callback_data=f"adm:vid:list:{scope}:{page - 1}"))
        if page < pages:
            pager.append(B(text="Next ▶️", callback_data=f"adm:vid:list:{scope}:{page + 1}"))
        return text, kb(*self._video_rows(rows), *([pager] if pager else []), back="adm:vid")

    def _video_card(self, v) -> str:
        size = f"{v.file_size / 1048576:.1f} MB" if v.file_size else "—"
        dur = f"{v.duration // 60}:{v.duration % 60:02d}" if v.duration is not None else "—"
        dims = f"{v.width}×{v.height}" if v.width and v.height else "—"
        cat = {"new": "🆕 New", "old": "📦 Old"}.get(v.category or "", "⏳ Waiting for a category")
        return (f"🎬 <b>VIDEO #{v.video_number or '-'}</b>\n\n"
                f"🆔 ID: <code>{v.id}</code>\n"
                f"📁 Collection: {cat}\n"
                f"📶 Status: {'✅ Active' if v.is_active else '⚠️ Deactivated'}\n"
                f"⏱️ Duration: {dur}   📐 {dims}   💾 {size}\n"
                f"📅 Posted: {when(v.message_date or v.created_at)}\n"
                f"📝 Caption: {short(v.caption, 120) or '—'}")

    async def p_vid_view(self, video_id: int) -> tuple[str, Kb]:
        v = await self.videos.get(video_id)
        if v is None:
            return "Video not found.", kb(back="adm:vid")
        rows = [[B(text="🆕 Set New", callback_data=f"adm:vid:cat:{v.id}:new"),
                 B(text="📦 Set Old", callback_data=f"adm:vid:cat:{v.id}:old")],
                [B(text="🔢 Number", callback_data=f"adm:in:vnum:{v.id}"),
                 B(text="📝 Caption", callback_data=f"adm:in:vcap:{v.id}")],
                [B(text="🗑️ Delete", callback_data=f"adm:ask:vdel:{v.id}")]]
        return self._video_card(v), kb(*rows, back="adm:vid")

    # ----- user manager -----
    def p_usr(self) -> tuple[str, Kb]:
        text = f"👥 <b>USER MANAGER</b>\n\n{LINE}\n\nChoose an option."
        return text, kb(*pairs([
            B(text="🔎 Search User", callback_data="adm:usr:search"),
            B(text="📋 Recent Users", callback_data="adm:usr:recent:1"),
            B(text="🚫 Block User", callback_data="adm:usr:block"),
            B(text="✅ Unblock User", callback_data="adm:usr:unblock"),
            B(text="📊 User Statistics", callback_data="adm:usr:stats"),
        ]))

    @staticmethod
    def _user_status(u) -> str:
        if u["is_banned"]:
            return "⛔ Blocked by admin"
        return "🚪 Stopped the bot" if u["is_blocked"] else "✅ Active"

    def _user_card(self, u) -> str:
        name = esc(u["first_name"] or "—")
        uname = f"@{esc(u['username'])}" if u["username"] else "—"
        return (f"👤 <b>USER</b>\n\n🆔 ID: <code>{u['user_id']}</code>\n"
                f"👋 Name: {name}\n🔗 Username: {uname}\n"
                f"📅 Joined: {when(u['first_seen'])}\n🕒 Last activity: {when(u['last_seen'])} ({ago(u['last_seen'])})\n"
                f"🎬 Videos received: <b>{n(u['videos'])}</b>\n"
                f"📨 Requests: <b>{n(u['requests'])}</b>\n"
                f"📶 Status: {self._user_status(u)}\n"
                f"🌐 Presence: <b>{presence(u['last_seen'])}</b>")

    async def p_usr_view(self, user_id: int) -> tuple[str, Kb]:
        u = await self.users.profile(user_id)
        if u is None:
            return "User not found.", kb(back="adm:usr")
        ban = (B(text="✅ Unblock", callback_data=f"adm:do:unban:{user_id}") if u["is_banned"]
               else B(text="🚫 Block", callback_data=f"adm:ask:ban:{user_id}"))
        return self._user_card(u), kb([ban, B(text="♻️ Clear history",
                                              callback_data=f"adm:ask:clrhist:{user_id}")],
                                      back="adm:usr")

    async def p_usr_recent(self, page: int) -> tuple[str, Kb]:
        page = max(1, page)
        rows, total = await self.users.recent(PAGE_USERS, (page - 1) * PAGE_USERS)
        pages = max(1, (total + PAGE_USERS - 1) // PAGE_USERS)
        if page > pages:
            page = pages
            rows, total = await self.users.recent(PAGE_USERS, (page - 1) * PAGE_USERS)
        buttons = [[B(text=f"{'🟢' if is_online(r['last_seen']) else '⚪'} "
                           f"{'⛔ ' if r['is_banned'] else ''}{(r['first_name'] or 'User')[:18]}"
                           f" · {r['user_id']}", callback_data=f"adm:usr:view:{r['user_id']}")]
                   for r in rows]
        pager = []
        if page > 1:
            pager.append(B(text="◀️ Prev", callback_data=f"adm:usr:recent:{page - 1}"))
        if page < pages:
            pager.append(B(text="Next ▶️", callback_data=f"adm:usr:recent:{page + 1}"))
        text = f"📋 <b>RECENT USERS</b>\n\nPage {page}/{pages} · {n(total)} users"
        return text, kb(*buttons, *([pager] if pager else []), back="adm:usr")

    async def p_usr_stats(self) -> tuple[str, Kb]:
        u = await self.users.user_numbers()
        text = (f"📊 <b>USER STATISTICS</b>\n\n{LINE}\n\n"
                f"👥 Total users: <b>{n(u['total'])}</b>\n"
                f"🟢 Online now (5 min): <b>{n(u['online'])}</b>\n"
                f"🟢 Active (24h): <b>{n(u['d1'])}</b>\n"
                f"🟢 Active (7 days): <b>{n(u['d7'])}</b>\n"
                f"🆕 Joined (24h / 7 days): <b>{n(u['new_1d'])} / {n(u['new_7d'])}</b>\n"
                f"🚫 Blocked by admin: <b>{n(u['banned'])}</b>\n"
                f"🚪 Stopped the bot: <b>{n(u['left_bot'])}</b>")
        return text, kb(back="adm:usr")

    # ----- broadcast -----
    @staticmethod
    def _bc_summary(r: BroadcastResult) -> str:
        return (f"✅ Delivered: <b>{n(r.sent)}</b>\n"
                f"🚪 Stopped the bot: <b>{n(r.blocked)}</b>\n"
                f"⚠️ Failed: <b>{n(r.failed)}</b>\n"
                f"👥 Audience: <b>{n(r.total)}</b>")

    async def p_bc(self) -> tuple[str, Kb]:
        audience = await self.broadcast.audience_count()
        text = (f"📢 <b>BROADCAST</b>\n\n{LINE}\n\n"
                "Send a modern announcement card to all your users.\n\n"
                f"👥 Audience: <b>{n(audience)}</b> active users\n"
                "<i>Users blocked by an admin or who stopped the bot are skipped.</i>")
        last = self.broadcast.last
        if last and not self.broadcast.running:
            head = "🛑 Last broadcast (stopped)" if last.stopped else "🕘 Last broadcast"
            text += f"\n\n{LINE}\n\n<b>{head}</b>\n{self._bc_summary(last)}"
        if self.broadcast.running:
            text += "\n\n📤 <b>A broadcast is being sent right now.</b>"
            return text, kb([B(text="⏹️ Stop Broadcast", callback_data="adm:bc:stop")])
        return text, kb([B(text="✍️ New Broadcast", callback_data="adm:bc:new")])

    @staticmethod
    def _bc_progress_text(r: BroadcastResult) -> str:
        filled = r.percent // 10
        bar = "▰" * filled + "▱" * (10 - filled)
        return (f"📤 <b>Sending broadcast...</b>\n\n{bar}  <b>{r.percent}%</b>\n\n"
                f"{Panel._bc_summary(r)}")

    async def _r_bc(self, cb: CallbackQuery, a: list[str]) -> None:
        sub = a[0] if a else ""
        uid = cb.from_user.id
        if sub == "new":
            if self.broadcast.running:
                await cb.answer("A broadcast is already being sent.", show_alert=True)
                return
            ok = await self.users.set_state(uid, "admin_input", {"kind": "bcmsg", "back": "adm:bc"})
            if not ok:
                await cb.answer("One of your video requests is still running. Try again in a "
                                "moment.", show_alert=True)
                return
            await cb.answer()
            await self._show(cb, (
                "✍️ <b>NEW BROADCAST</b>\n\n"
                "Send the message now. It is placed inside a modern announcement card.\n\n"
                "💡 Tips\n"
                "• Start with a <b>bold title</b> line, then your text\n"
                "• Bold, italic, links and emoji all work\n"
                f"• Up to {MAX_BODY:,} characters\n\n"
                "You'll see a preview before anything is sent."),
                Kb(inline_keyboard=[[B(text="❌ Cancel", callback_data="adm:cancel")]]))
            return
        if sub == "stop":
            if self.broadcast.running:
                self.broadcast.stop()
                await cb.answer("Stopping after the current message...")
            else:
                await cb.answer("No broadcast is running.")
            return
        if sub in ("test", "go"):
            state, data = await self.users.get_state(uid)
            if state != "admin_preview" or data.get("kind") != "bc":
                await cb.answer("This preview has expired. Write the message again.",
                                show_alert=True)
                return
            if sub == "test":
                try:
                    await self.bot.send_message(uid, render(data["text"]), parse_mode=HTML,
                                                reply_markup=card_kb())
                    await cb.answer("🧪 Test message sent to you.")
                except TelegramAPIError:
                    await cb.answer("Could not send the test message.", show_alert=True)
                return
            claimed = await self.users.claim_state(uid, "admin_preview", "admin_saving")
            if claimed is None or claimed.get("kind") != "bc":
                await self.users.clear_state(uid)
                await cb.answer("This preview has expired. Write the message again.",
                                show_alert=True)
                return
            await self.users.clear_state(uid)
            if not self.broadcast.try_begin():
                await cb.answer("A broadcast is already being sent.", show_alert=True)
                return
            await cb.answer("📤 Broadcast started")
            await self._log(uid, "broadcast started")
            chat_id, msg_id = cb.message.chat.id, cb.message.message_id
            await self._show(cb, "📤 <b>Sending broadcast...</b>\n\n▱▱▱▱▱▱▱▱▱▱  <b>0%</b>",
                             kb([B(text="⏹️ Stop Broadcast", callback_data="adm:bc:stop")],
                                back=None))
            task = asyncio.create_task(self._bc_job(chat_id, msg_id, claimed["text"], uid))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        await self._view(cb)

    async def _bc_job(self, chat_id: int, msg_id: int, body: str, admin_id: int) -> None:
        stop_kb = kb([B(text="⏹️ Stop Broadcast", callback_data="adm:bc:stop")], back=None)

        async def progress(r: BroadcastResult) -> None:
            try:
                await self.bot.edit_message_text(self._bc_progress_text(r), chat_id=chat_id,
                                                 message_id=msg_id, reply_markup=stop_kb,
                                                 parse_mode=HTML)
            except TelegramAPIError:
                pass

        try:
            r = await self.broadcast.run(body, progress)
        except Exception:  # noqa: BLE001
            log.exception("broadcast_crashed")
            text = "⚠️ <b>The broadcast stopped unexpectedly.</b> See the logs."
        else:
            head = "🛑 <b>Broadcast stopped.</b>" if r.stopped else "✅ <b>Broadcast complete.</b>"
            text = f"{head}\n\n{LINE}\n\n{self._bc_summary(r)}\n⏱️ Time: <b>{r.seconds:.0f}s</b>"
            await self._log(admin_id, f"broadcast finished: {r.sent}/{r.total} delivered")
        markup = kb(back="adm:bc")
        try:
            await self.bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                                             reply_markup=markup, parse_mode=HTML)
        except TelegramAPIError:
            try:
                await self.bot.send_message(chat_id, text, reply_markup=markup, parse_mode=HTML)
            except TelegramAPIError:
                pass

    # ----- scanner -----
    def _scan_counts(self, r) -> str:
        return (f"📨 Messages scanned: <b>{n(r.scanned)}</b>\n🎬 Videos found: <b>{n(r.found)}</b>\n"
                f"➕ Added: <b>{n(r.added)}</b>\n🔁 Re-posted copies stored: <b>{n(r.repeated)}</b>\n"
                f"♻️ Already registered: <b>{n(r.duplicates)}</b>\n"
                f"⚠️ Errors: <b>{n(r.errors)}</b>")

    async def p_scn(self) -> tuple[str, Kb]:
        sc = self.scanner
        if not sc.enabled:
            status = "🔴 Not configured"
        else:
            status = "🟢 Connected" if sc.connected else "🟡 Configured (connects when a scan starts)"
        n_ch = len(self.cfg.source_channel_ids)
        channel = ("Not configured" if not n_ch else
                   "Configured Source Channel" if n_ch == 1 else f"{n_ch} source channels")
        lines = ["📡 <b>CHANNEL SCANNER</b>", "", "Channel:", f"<code>{channel}</code>", "",
                 "Status:", status, ""]
        if sc.running and sc.current is not None:
            lines += ["🔎 <b>Scanning channel...</b>", "", self._scan_counts(sc.current)]
        else:
            row = await sc.status_row()
            lines += [f"Last scan: <b>{esc(row['status'])}</b>"
                      + (f" ({esc(row['mode'])})" if row["mode"] else ""),
                      f"🕒 Started: {when(row['started_at'])}", f"🏁 Finished: {when(row['finished_at'])}",
                      f"📨 Scanned: {n(row['messages_scanned'])} · 🎬 Found: {n(row['videos_found'])}"
                      f" · ➕ Added: {n(row['videos_added'])}",
                      f"♻️ Duplicates: {n(row['duplicates'])} · ⚠️ Errors: {n(row['errors'])}"]
            if self.cfg.auto_scan_minutes:
                lines.append(f"🔁 Automatic catch-up every {self.cfg.auto_scan_minutes} min")
        return "\n".join(lines), kb(*pairs([
            B(text="🔎 Full Scan", callback_data="adm:scn:full"),
            B(text="⚡ Quick Scan", callback_data="adm:scn:quick"),
            B(text="📊 Scan Status", callback_data="adm:scn:status"),
            B(text="⏹️ Stop Scan", callback_data="adm:scn:stop"),
        ]), [B(text="🔄 Refresh", callback_data="adm:scn:status")])

    # ----- bot control / maintenance -----
    def p_bot(self) -> tuple[str, Kb]:
        s = self.settings
        text = (f"🤖 <b>BOT CONTROL</b>\n\n{LINE}\n\nStatus: <b>{s.status_label()}</b>\n\n"
                "When the bot is OFF or in maintenance, normal users are told so. Admins keep "
                "full access to this panel.")
        on = (B(text="🟢 Bot ON ✓", callback_data="adm:bot") if s.bot_enabled
              else B(text="🟢 Bot ON", callback_data="adm:do:boton"))
        off = (B(text="🔴 Bot OFF", callback_data="adm:ask:botoff") if s.bot_enabled
               else B(text="🔴 Bot OFF ✓", callback_data="adm:bot"))
        maint = (B(text="🟢 End Maintenance", callback_data="adm:do:mntoff") if s.maintenance
                 else B(text="🟡 Maintenance Mode", callback_data="adm:ask:mnton"))
        return text, kb([on, off], [maint])

    def _maint_preview_text(self, **override: str) -> str:
        s = self.settings
        title = override.get("title", s.get("maintenance_title"))
        msg = override.get("message", s.get("maintenance_message"))
        emoji = override.get("emoji", s.get("maintenance_emoji"))
        return f"{emoji} <b>{title}</b>\n\n{msg}"

    def p_mnt(self, preview: bool = False) -> tuple[str, Kb]:
        s = self.settings
        if preview:
            return (f"👁 <b>Preview</b> — what users see:\n\n{LINE}\n\n"
                    f"{self._maint_preview_text()}\n\n{LINE}"), kb(back="adm:mnt")
        text = (f"🛠️ <b>MAINTENANCE MODE</b>\n\n{LINE}\n\n"
                f"Maintenance: <b>{on_off(s.maintenance)}</b>\n"
                f"Custom message: <b>{on_off(s.flag('maintenance_custom'))}</b>\n\n"
                f"Emoji: {s.get('maintenance_emoji')}\nTitle: <b>{s.get('maintenance_title')}</b>\n"
                f"Message:\n{s.get('maintenance_message')}")
        toggle = (B(text="🟢 End Maintenance", callback_data="adm:do:mntoff") if s.maintenance
                  else B(text="🟡 Enable Maintenance", callback_data="adm:ask:mnton"))
        return text, kb(
            [toggle],
            [B(text="✏️ Title", callback_data="adm:edit:maintenance_title"),
             B(text="💬 Message", callback_data="adm:edit:maintenance_message"),
             B(text="😀 Emoji", callback_data="adm:edit:maintenance_emoji")],
            [B(text=f"🎛 Custom message: {on_off(s.flag('maintenance_custom'))}",
               callback_data="adm:tgl:maintenance_custom:m")],
            [B(text="👁 Preview", callback_data="adm:mnt:prev"),
             B(text="♻️ Reset text", callback_data="adm:rst:mnt")],
            back="adm:bot")

    # ----- public settings -----
    def p_pub(self) -> tuple[str, Kb]:
        s = self.settings
        text = (f"🎨 <b>PUBLIC SETTINGS</b>\n\n{LINE}\n\n"
                f"Welcome message: <b>{'custom' if s.get('welcome_message') else 'default'}</b>\n"
                f"Help message: <b>{'custom' if s.get('help_message') else 'default'}</b>\n"
                f"Developer button: <b>{on_off(s.button_on('btn_developer'))}</b>\n"
                f"Custom button: <b>{on_off(s.custom_button()['enabled'])}</b>\n"
                f"Auto-delete notice: <b>{on_off(s.flag('delete_notice_enabled'))}</b>")
        return text, kb(*pairs([
            B(text="👋 Welcome Message", callback_data="adm:pub:welcome"),
            B(text="❓ Help Message", callback_data="adm:pub:help"),
            B(text="🔘 Public Buttons", callback_data="adm:btns"),
            B(text="👨‍💻 Developer Button", callback_data="adm:dev"),
            B(text="⭐ Custom Button", callback_data="adm:cust"),
            B(text="🛠️ Maintenance Message", callback_data="adm:mnt"),
            B(text="⏱️ Auto-delete Notice", callback_data="adm:pub:notice"),
        ]))

    def _rendered(self, key: str, value: str | None = None) -> str:
        """The text exactly as users see it (optionally with a not-yet-saved `value`)."""
        s, c = self.settings, self.cfg
        if key == "welcome_message":
            return ui._fill(value, c) if value else ui.default_welcome(c)
        if key == "help_message":
            return ui._fill(value, c) if value else ui.default_help(c)
        return ui._fill(value, c) if value else ui.default_delete_notice(c)

    def p_text(self, key: str, preview: bool = False) -> tuple[str, Kb]:
        title, icon = TEXT_PAGES[key]
        s, page = self.settings, f"adm:pub:{PAGE_OF[key]}"
        if preview:
            return (f"👁 <b>Preview</b> — what users see:\n\n{LINE}\n\n"
                    f"{self._rendered(key, s.get(key))}\n\n{LINE}"), kb(back=page)
        rows = [[B(text="✏️ Edit", callback_data=f"adm:edit:{key}"),
                 B(text="👁 Preview", callback_data=f"{page}:prev")],
                [B(text="♻️ Reset to default", callback_data=f"adm:rst:{PAGE_OF[key]}")]]
        if key == "delete_notice":
            rows.insert(1, [B(text=f"Show notice: {on_off(s.flag('delete_notice_enabled'))}",
                              callback_data="adm:tgl:delete_notice_enabled:n")])
        if key == "welcome_message":
            rows.insert(1, [B(text=f"Show welcome: {on_off(s.flag('welcome_enabled'))}",
                              callback_data="adm:tgl:welcome_enabled:w")])
            rows.insert(2, [B(text=f"🔥 Fire effect: {on_off(s.flag('welcome_animation'))}",
                              callback_data="adm:tgl:welcome_animation:w")])
        text = (f"{icon} <b>{title.upper()}</b>\n\nCurrently using the "
                f"<b>{'custom' if s.get(key) else 'default'}</b> text.\n\n{LINE}\n\n"
                f"{self._rendered(key, s.get(key))}\n\n{LINE}")
        return text, kb(*rows, back="adm:pub")

    def p_btns(self) -> tuple[str, Kb]:
        s = self.settings
        rows = [[B(text=f"{label}  {on_off(s.button_on(key))}", callback_data=f"adm:tgl:{key}:b")]
                for key, label in PUBLIC_BUTTONS.items()]
        rows.append([B(text="⭐ Custom Button", callback_data="adm:cust")])
        text = (f"🔘 <b>PUBLIC BUTTONS</b>\n\n{LINE}\n\nTap a button to switch it on or off for "
                "users. At least one way to request videos always stays enabled; Back and Main "
                "Menu buttons cannot be removed.")
        return text, kb(*rows, back="adm:pub")

    def _cust_line(self, c: dict) -> str:
        label = f"{c['emoji']} {c['name']}".strip() or "—"
        where = (c["target"] if c["kind"] == "url" else
                 CUSTOM_ACTIONS.get(c["target"], ("—",))[0]) or "—"
        return (f"Label: <b>{esc(label)}</b>\nOpens: {'link' if c['kind'] == 'url' else 'action'} "
                f"→ {esc(where)}\nPosition: <b>{'top' if c['position'] == 'top' else 'bottom'}</b>\n"
                f"Enabled: <b>{on_off(c['enabled'])}</b>")

    def p_cust(self) -> tuple[str, Kb]:
        c = self.settings.custom_button()
        text = f"⭐ <b>CUSTOM BUTTON</b>\n\n{LINE}\n\n{self._cust_line(c)}"
        if c["enabled"] and not self.settings.custom_ready(c):
            text += "\n\n⚠️ Incomplete: set a name and a link or action."
        return text, kb(
            [B(text="✏️ Name", callback_data="adm:in:cname:0"),
             B(text="😀 Emoji", callback_data="adm:in:cemoji:0")],
            [B(text="🔗 Set Link", callback_data="adm:in:curl:0"),
             B(text="⚡ Set Action", callback_data="adm:cust:act")],
            [B(text=f"Enabled: {on_off(c['enabled'])}", callback_data="adm:cust:tgl"),
             B(text=f"📍 {'Top' if c['position'] == 'top' else 'Bottom'}",
               callback_data="adm:cust:pos")],
            [B(text="🗑️ Remove button", callback_data="adm:cust:del")],
            back="adm:pub")

    def p_cust_actions(self) -> tuple[str, Kb]:
        rows = [[B(text=label, callback_data=f"adm:cust:set:{key}")]
                for key, (label, _) in CUSTOM_ACTIONS.items()]
        return ("⚡ <b>CHOOSE AN ACTION</b>\n\nThe button will open one of these built-in screens.",
                kb(*rows, back="adm:cust"))

    # ----- developer / bot settings -----
    def p_dev(self, preview: bool = False) -> tuple[str, Kb]:
        s = self.settings
        if preview:
            links = [("🌐 Website", "developer_website"), ("📸 Instagram", "developer_instagram"),
                     ("💬 Telegram", "developer_telegram"), ("🐙 GitHub", "developer_github")]
            btns = [B(text=lbl, url=s.get(k)) for lbl, k in links if s.get(k)]
            return (f"👁 <b>Preview</b> — what users see:\n\n{LINE}\n\n{ui.developer_text(s)}",
                    kb(*[btns[i:i + 2] for i in range(0, len(btns), 2)], back="adm:dev"))
        d = s.developer()

        def val(key: str) -> str:
            return esc(d[key]) if d[key] else "—"
        text = (f"👨‍💻 <b>DEVELOPER SETTINGS</b>\n\n{LINE}\n\n"
                f"Name: <b>{esc(d['name'])}</b>\nBio: {d['bio'] or '—'}\n"
                f"🌐 Website: {val('website')}\n💬 Telegram: {val('telegram')}\n"
                f"📸 Instagram: {val('instagram')}\n🐙 GitHub: {val('github')}\n"
                f"Developer button: <b>{on_off(d['enabled'])}</b>")
        return text, kb(
            [B(text="✏️ Name", callback_data="adm:edit:developer_name"),
             B(text="📝 Bio", callback_data="adm:edit:developer_bio")],
            [B(text="🌐 Website", callback_data="adm:edit:developer_website"),
             B(text="💬 Telegram", callback_data="adm:edit:developer_telegram")],
            [B(text="📸 Instagram", callback_data="adm:edit:developer_instagram"),
             B(text="🐙 GitHub", callback_data="adm:edit:developer_github")],
            [B(text=f"Button: {on_off(d['enabled'])}", callback_data="adm:tgl:btn_developer:d"),
             B(text="👁 Preview", callback_data="adm:dev:prev")],
            back="adm:pub")

    def p_adel(self) -> tuple[str, Kb]:
        """Auto-delete time: one-tap presets (minutes) + a custom value."""
        s = self.settings
        cur = self.cfg.auto_delete_seconds
        preset_secs = {m * 60: m for m in AUTO_DELETE_PRESETS}
        source = "set from this panel" if s.is_custom("auto_delete_seconds") else "default (environment)"
        text = (f"⏱️ <b>AUTO-DELETE TIME</b>\n\n{LINE}\n\n"
                f"Videos disappear from the user's chat after: <b>{ui.fmt_duration(cur)}</b>\n"
                f"Source: {source}\n\n"
                "Pick a time below, or choose Custom to type your own (minutes).\n"
                "The change applies to videos sent from now on.")
        buttons = [B(text=("✅ " if cur == m * 60 else "") + f"{m} min",
                     callback_data=f"adm:adel:set:{m}") for m in AUTO_DELETE_PRESETS]
        custom_label = "✏️ Custom"
        if cur not in preset_secs:
            custom_label = f"✅ Custom ({ui.fmt_duration(cur)})"
        rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
        rows.append([B(text=custom_label, callback_data="adm:edit:auto_delete_seconds")])
        rows.append([B(text="♻️ Reset to default", callback_data="adm:adel:rst")])
        return text, kb(*rows, back="adm:set")

    def p_set(self) -> tuple[str, Kb]:
        s, c = self.settings, self.cfg
        nlabel = ORDER_LABELS["new_video_order"][c.new_video_order]
        olabel = ORDER_LABELS["old_video_order"][c.old_video_order]
        text = (f"⚙️ <b>BOT SETTINGS</b>\n\n{LINE}\n\n"
                f"🔢 Max videos per request: <b>{c.max_videos_per_request}</b>\n"
                f"⏱️ Auto-delete: <b>{ui.fmt_duration(c.auto_delete_seconds)}</b>\n"
                f"🆕 New ordering: <b>{nlabel}</b>\n📦 Old ordering: <b>{olabel}</b>\n"
                f"📤 Delivery batch size: <b>{c.delivery_batch_size}</b>\n"
                f"🚦 Rate limit: <b>{c.rate_limit_requests} requests / {c.rate_limit_window}s</b>\n"
                f"👋 Welcome message: <b>{on_off(s.flag('welcome_enabled'))}</b>\n"
                f"👨‍💻 Developer button: <b>{on_off(s.button_on('btn_developer'))}</b>\n"
                f"💬 Keep source caption: <b>{on_off(s.flag('keep_source_caption'))}</b>\n"
                f"♻️ Send duplicate videos: <b>{on_off(s.flag('allow_duplicate_videos'))}</b>\n"
                f"🛠️ Maintenance: <b>{on_off(s.maintenance)}</b>")
        maint = (B(text="🛠️ Maintenance: ✅ ON → end", callback_data="adm:do:mntoff") if s.maintenance
                 else B(text="🛠️ Maintenance: ❌ OFF → enable", callback_data="adm:ask:mnton"))
        return text, kb(
            [B(text="🔢 Max videos", callback_data="adm:edit:max_videos_per_request"),
             B(text="⏱️ Auto-delete", callback_data="adm:adel")],
            [B(text=f"🆕 New: {nlabel}", callback_data="adm:cyc:new_video_order"),
             B(text=f"📦 Old: {olabel}", callback_data="adm:cyc:old_video_order")],
            [B(text="📤 Batch size", callback_data="adm:edit:delivery_batch_size"),
             B(text="🚦 Rate limit", callback_data="adm:edit:rate_limit_requests")],
            [B(text=f"👋 Welcome: {on_off(s.flag('welcome_enabled'))}",
               callback_data="adm:tgl:welcome_enabled:s"),
             B(text=f"👨‍💻 Developer: {on_off(s.button_on('btn_developer'))}",
               callback_data="adm:tgl:btn_developer:s")],
            [B(text=f"💬 Source caption: {on_off(s.flag('keep_source_caption'))}",
               callback_data="adm:tgl:keep_source_caption:s")],
            [B(text=f"♻️ Send duplicates: {on_off(s.flag('allow_duplicate_videos'))}",
               callback_data="adm:tgl:allow_duplicate_videos:s")],
            [maint], back="adm:home")

    # ----- logs -----
    async def p_log(self, group: str, page: int) -> tuple[str, Kb]:
        rows, total = await self.events.recent(group, PAGE_LOGS, (max(1, page) - 1) * PAGE_LOGS)
        pages = max(1, (total + PAGE_LOGS - 1) // PAGE_LOGS)
        page = min(max(1, page), pages)
        lines = [f"📋 <b>LOGS</b> — {GROUPS[group][0]}", f"Page {page}/{pages} · {n(total)} events", ""]
        for r in rows:
            icon = KINDS.get(r["kind"], ("•",))[0]
            who = f" · <code>{r['user_id']}</code>" if r["user_id"] else ""
            lines.append(f"<code>{r['ts']:%d.%m %H:%M}</code> {icon} {esc(r['message'])}{who}")
        if not rows:
            lines.append("No events yet.")
        filt = [B(text=("• " if g == group else "") + label.split(" ", 1)[0] ,
                  callback_data=f"adm:log:{g}:1") for g, (label, _) in GROUPS.items()]
        pager = []
        if page > 1:
            pager.append(B(text="◀️ Prev", callback_data=f"adm:log:{group}:{page - 1}"))
        pager.append(B(text="🔄", callback_data=f"adm:log:{group}:{page}"))
        if page < pages:
            pager.append(B(text="Next ▶️", callback_data=f"adm:log:{group}:{page + 1}"))
        return "\n".join(lines), kb(filt, pager)

    # ----- confirmations -----
    ASKS = {
        "vdel": ("Delete video ID {id} from the library? Its delivery history is removed too. "
                 "Messages already sent to users and the source channel are not touched.",
                 "adm:vid:view:{id}"),
        "ban": ("Block user {id}? They will no longer be able to use VideoShare.",
                "adm:usr:view:{id}"),
        "clrhist": ("Clear the delivery history of user {id}? They can receive those videos again.",
                    "adm:usr:view:{id}"),
        "botoff": ("Turn the bot OFF? Normal users will see that VideoShare is unavailable. "
                   "Admins keep full access.", "adm:bot"),
        "mnton": ("Enable maintenance mode? Normal users will see the maintenance message. "
                  "Admins keep full access.", "adm:bot"),
    }

    async def p_ask(self, a: list[str]) -> tuple[str, Kb]:
        action, arg = (a[0] if a else ""), (a[1] if len(a) > 1 else "")
        if action not in self.ASKS or (action in ("vdel", "ban", "clrhist")
                                       and not re.fullmatch(r"-?\d+", arg)):
            return await self.p_home()
        desc, back = self.ASKS[action]
        text = f"⚠️ <b>Are you sure?</b>\n\n{desc.format(id=arg)}"
        return text, Kb(inline_keyboard=[[
            B(text="✅ Confirm", callback_data=f"adm:do:{action}:{arg}"),
            B(text="❌ Cancel", callback_data=back.format(id=arg))]])

    # ================================================================== ROUTES (callbacks)
    # ----- plain views -----
    async def _r_home(self, cb: CallbackQuery, a: list[str]) -> None:
        await self._view(cb)

    _r_stats = _r_btns = _r_set = _r_log = _r_pub = _r_dev = _r_mnt = _r_bot = _r_ask = _r_home

    async def _r_adel(self, cb: CallbackQuery, a: list[str]) -> None:
        sub = a[0] if a else ""
        if sub == "set" and len(a) > 1 and a[1].isdigit() and int(a[1]) in AUTO_DELETE_PRESETS:
            minutes = int(a[1])
            await self._put("auto_delete_seconds", str(minutes * 60))
            await self._log(cb.from_user.id, f"auto_delete_seconds → {minutes * 60}")
            await cb.answer(f"✅ {minutes} minutes")
            text, markup = self.p_adel()
            await self._show(cb, text, markup)
            return
        if sub == "rst":
            await self.settings.reset("auto_delete_seconds")
            self.settings.apply_runtime(self.videos)
            await self._log(cb.from_user.id, "auto_delete_seconds reset to default")
            await cb.answer("Restored the default")
            text, markup = self.p_adel()
            await self._show(cb, text, markup)
            return
        await self._view(cb)

    async def _r_usr(self, cb: CallbackQuery, a: list[str]) -> None:
        sub = a[0] if a else ""
        prompts = {
            "search": ("usearch", "Search user", "Send a Telegram user ID or @username.\n"
                       "Only people who have written to the bot can be found."),
            "block": ("ublock", "Block user", "Send the Telegram user ID to block."),
            "unblock": ("uunblock", "Unblock user", "Send the Telegram user ID to unblock."),
        }
        if sub in prompts:
            kind, title, hint = prompts[sub]
            await self._prompt(cb, kind, title, hint, "adm:usr")
            return
        await self._view(cb)

    async def _r_vid(self, cb: CallbackQuery, a: list[str]) -> None:
        sub = a[0] if a else ""
        uid = cb.from_user.id
        prompts = {
            "search": ("vsearch", "Search video", "Send a video number, database ID or a word "
                       "from the caption."),
            "del": ("vdel", "Delete video", "Send the database <b>ID</b> of the video to delete "
                    "(shown on every video card)."),
            "edit": ("vedit", "Edit video", "Send the video number or database ID."),
        }
        if sub in prompts:
            kind, title, hint = prompts[sub]
            await self._prompt(cb, kind, title, hint, "adm:vid")
            return
        if sub == "rescan":
            await self._start_scan(cb, full=True)
            return
        if sub == "cat" and len(a) == 3 and a[1].isdigit() and a[2] in ("new", "old"):
            video = await self.videos.assign_category(int(a[1]), a[2], lock=True)
            if video is None:
                await cb.answer("Video not found.", show_alert=True)
                return
            await self._log(uid, f"video ID {video.id} set to {a[2]}")
            await cb.answer("Saved")
            text, markup = await self.p_vid_view(video.id)
            await self._show(cb, text, markup)
            return
        await self._view(cb)

    async def _r_scn(self, cb: CallbackQuery, a: list[str]) -> None:
        sub = a[0] if a else ""
        if sub in ("full", "quick"):
            await self._start_scan(cb, full=sub == "full")
        elif sub == "stop":
            if self.scanner.request_stop():
                await cb.answer("🛑 Stopping scan... progress so far is saved.", show_alert=True)
                await self._log(cb.from_user.id, "scan stop requested")
            else:
                await cb.answer("No scan is running.", show_alert=True)
        else:
            await self._view(cb)

    # ----- changing state -----
    async def _r_do(self, cb: CallbackQuery, a: list[str]) -> None:
        action, arg = (a[0] if a else ""), (a[1] if len(a) > 1 else "")
        uid = cb.from_user.id
        target_id = int(arg) if re.fullmatch(r"-?\d+", arg) else None

        if action in ("boton", "botoff", "mnton", "mntoff"):
            key, value = {"boton": ("bot_enabled", "1"), "botoff": ("bot_enabled", "0"),
                          "mnton": ("maintenance_mode", "1"), "mntoff": ("maintenance_mode", "0")}[action]
            await self._put(key, value)
            await self._log(uid, {"boton": "bot turned ON", "botoff": "bot turned OFF",
                                  "mnton": "maintenance enabled", "mntoff": "maintenance disabled"}[action])
            await cb.answer("Done")
            text, markup = self.p_bot()
            await self._show(cb, text, markup)
            return
        if action == "vdel" and target_id is not None:
            if await self.videos.delete(target_id):
                await self._log(uid, f"video ID {target_id} deleted")
                await self.events.record("video_deleted", f"Video ID {target_id} deleted")
                await cb.answer("🗑️ Deleted")
            else:
                await cb.answer("Video not found.", show_alert=True)
            text, markup = await self.p_vid()
            await self._show(cb, text, markup)
            return
        if action in ("ban", "unban", "clrhist") and target_id is not None:
            if action == "ban":
                if target_id in self.cfg.admin_ids:
                    await cb.answer("Admins can't be blocked.", show_alert=True)
                    return
                await self.users.set_banned(target_id, True)
                await self._log(uid, f"user {target_id} blocked")
                await cb.answer("🚫 User blocked")
            elif action == "unban":
                await self.users.set_banned(target_id, False)
                await self._log(uid, f"user {target_id} unblocked")
                await cb.answer("✅ User unblocked")
            else:
                removed = await self.videos.reset_user_history(target_id)
                await self._log(uid, f"history of user {target_id} cleared ({removed} videos)")
                await cb.answer(f"♻️ History cleared ({removed} videos)", show_alert=True)
            text, markup = await self.p_usr_view(target_id)
            await self._show(cb, text, markup)
            return
        await cb.answer()

    async def _r_tgl(self, cb: CallbackQuery, a: list[str]) -> None:
        key = a[0] if a else ""
        if key not in TOGGLES:
            await cb.answer()
            return
        s = self.settings
        new = not s.flag(key)
        if key in PUBLIC_BUTTONS and not new and not s.can_disable(key):
            await cb.answer("At least one way to request videos must stay enabled.", show_alert=True)
            return
        await self._put(key, "1" if new else "0")
        await self._log(cb.from_user.id, f"{key} → {'on' if new else 'off'}")
        await cb.answer("Saved")
        text, markup = await self.page(ORIGINS.get(a[1] if len(a) > 1 else "s", "adm:set"))
        await self._show(cb, text, markup)

    async def _r_cyc(self, cb: CallbackQuery, a: list[str]) -> None:
        key = a[0] if a else ""
        if key not in ORDER_LABELS:
            await cb.answer()
            return
        current = self.settings.get(key)
        nxt = ORDERS[(ORDERS.index(current) + 1) % len(ORDERS)] if current in ORDERS else ORDERS[0]
        await self._put(key, nxt)
        await self._log(cb.from_user.id, f"{key} → {nxt}")
        await cb.answer("Saved")
        text, markup = self.p_set()
        await self._show(cb, text, markup)

    async def _r_rst(self, cb: CallbackQuery, a: list[str]) -> None:
        what = a[0] if a else ""
        s = self.settings
        if what in PAGE_OF.values():
            key = next(k for k, v in PAGE_OF.items() if v == what)
            await s.reset(key)
            path = f"adm:pub:{what}"
        elif what == "mnt":
            for key in ("maintenance_title", "maintenance_message", "maintenance_emoji",
                        "maintenance_custom"):
                await s.reset(key)
            path = "adm:mnt"
        else:
            await cb.answer()
            return
        s.apply_runtime(self.videos)
        await self._log(cb.from_user.id, f"{what} text reset to default")
        await cb.answer("Restored the default")
        text, markup = await self.page(path)
        await self._show(cb, text, markup)

    async def _r_cust(self, cb: CallbackQuery, a: list[str]) -> None:
        sub = a[0] if a else ""
        s = self.settings
        c = s.custom_button()
        if sub == "tgl":
            if not c["enabled"] and not s.custom_ready(c):
                await cb.answer("Set a name and a link or action first.", show_alert=True)
                return
            c["enabled"] = not c["enabled"]
        elif sub == "pos":
            c["position"] = "top" if c["position"] == "bottom" else "bottom"
        elif sub == "del":
            await s.reset("custom_button")
            await self._log(cb.from_user.id, "custom button removed")
            await cb.answer("Removed")
            text, markup = self.p_cust()
            await self._show(cb, text, markup)
            return
        elif sub == "set" and len(a) > 1 and a[1] in CUSTOM_ACTIONS:
            c.update(kind="action", target=a[1])
            if not c["name"]:
                c["name"] = CUSTOM_ACTIONS[a[1]][0].split(" ", 1)[1]
        else:
            await self._view(cb)
            return
        await s.save_custom_button(c)
        await self._log(cb.from_user.id, f"custom button {sub}")
        await cb.answer("Saved")
        text, markup = self.p_cust()
        await self._show(cb, text, markup)

    # ----- text input flow (edit → validate → preview → save) -----
    async def _prompt(self, cb: CallbackQuery, kind: str, title: str, hint: str, back: str,
                      **extra) -> None:
        ok = await self.users.set_state(cb.from_user.id, "admin_input",
                                        {"kind": kind, "back": back, **extra})
        if not ok:
            await cb.answer("One of your video requests is still running. Try again in a moment.",
                            show_alert=True)
            return
        await cb.answer()
        await self._show(cb, f"✏️ <b>{esc(title)}</b>\n\n{hint}\n\nSend the new value now, or "
                             "press Cancel.",
                         Kb(inline_keyboard=[[B(text="❌ Cancel", callback_data="adm:cancel")]]))

    async def _r_edit(self, cb: CallbackQuery, a: list[str]) -> None:
        key = a[0] if a else ""
        field = FIELDS.get(key)
        if field is None:
            await cb.answer()
            return
        s = self.settings
        current = s.get(key)
        hint = field.hint
        if key == "auto_delete_seconds" and current.isdigit():
            hint = f"Current: <b>{ui.fmt_duration(int(current))}</b>\n\n{hint}"
        elif current and len(current) <= 120 and key != "welcome_message":
            hint = f"Current: <code>{esc(html.unescape(plain_preview(current)))}</code>\n\n{hint}"
        await self._prompt(cb, "field", field.label, hint, field.back, key=key)

    async def _r_in(self, cb: CallbackQuery, a: list[str]) -> None:
        kind, arg = (a[0] if a else ""), (a[1] if len(a) > 1 else "0")
        specs = {
            "vnum": ("Video number", "Send the new number (a whole number, must be unused).",
                     f"adm:vid:view:{arg}"),
            "vcap": ("Video caption", "Send the new caption (up to 1000 characters) — "
                     "<code>-</code> clears it.", f"adm:vid:view:{arg}"),
            "cname": ("Button name", "Send the button text, up to 24 characters.", "adm:cust"),
            "cemoji": ("Button emoji", "Send one emoji.", "adm:cust"),
            "curl": ("Button link", "Send a link starting with https://", "adm:cust"),
        }
        if kind not in specs:
            await cb.answer()
            return
        title, hint, back = specs[kind]
        await self._prompt(cb, kind, title, hint, back, arg=arg)

    async def _r_cancel(self, cb: CallbackQuery, a: list[str]) -> None:
        state, data = await self.users.get_state(cb.from_user.id)
        back = "adm:home"
        if state in ("admin_input", "admin_preview"):
            back = data.get("back", "adm:home")
            await self.users.clear_state(cb.from_user.id)
        await cb.answer("Cancelled")
        text, markup = await self.page(back)
        await self._show(cb, text, markup)

    async def _r_save(self, cb: CallbackQuery, a: list[str]) -> None:
        _, peek = await self.users.get_state(cb.from_user.id)
        if peek.get("kind") == "bc":
            await cb.answer("This preview has expired. Start the edit again.", show_alert=True)
            return
        data = await self.users.claim_state(cb.from_user.id, "admin_preview", "admin_saving")
        if data is None:
            await cb.answer("This preview has expired. Start the edit again.", show_alert=True)
            return
        try:
            await self._save_field(cb.from_user.id, data["key"], data["value"])
        finally:
            await self.users.clear_state(cb.from_user.id)
        await cb.answer("✅ Saved")
        text, markup = await self.page(data.get("back", "adm:home"))
        await self._show(cb, "✅ <b>Saved.</b>\n\n" + text, markup)

    async def _save_field(self, uid: int, key: str, value: str) -> None:
        field = FIELDS[key]
        await self._put(key, value)
        if field.enables:
            await self._put(field.enables, "1")
        await self._log(uid, f"{key} updated")

    def _preview_text(self, key: str, value: str) -> str:
        if key in TEXT_PAGES:
            return self._rendered(key, value)
        if key == "maintenance_title":
            return self._maint_preview_text(title=value)
        if key == "maintenance_message":
            return self._maint_preview_text(message=value)
        if key == "maintenance_emoji":
            return self._maint_preview_text(emoji=value)
        if key == "developer_bio":
            return ui.developer_text(self.settings, bio=value)
        return esc(value)

    async def on_input(self, message: Message) -> None:
        uid = message.from_user.id
        state, data = await self.users.get_state(uid)
        if state != "admin_input":
            return
        handler = getattr(self, f"_i_{data.get('kind')}", None)
        if handler is None:
            await self.users.clear_state(uid)
            return
        await handler(message, data, (message.text or "").strip())

    async def _retry(self, message: Message, problem: str) -> None:
        await self._send(message, f"⚠️ {esc(problem)}\n\nTry again, or press Cancel.",
                         Kb(inline_keyboard=[[B(text="❌ Cancel", callback_data="adm:cancel")]]))

    async def _finish(self, message: Message, path: str, prefix: str = "") -> None:
        await self.users.clear_state(message.from_user.id)
        text, markup = await self.page(path)
        await self._send(message, prefix + text, markup)

    async def _i_field(self, message: Message, data: dict, text: str) -> None:
        uid, key = message.from_user.id, data.get("key", "")
        field = FIELDS.get(key)
        if field is None:
            await self.users.clear_state(uid)
            return
        if text.lower() == "default":
            await self.settings.reset(key)
            self.settings.apply_runtime(self.videos)
            await self._log(uid, f"{key} reset to default")
            await self._finish(message, field.back, "♻️ <b>Restored the default.</b>\n\n")
            return
        if text == "-" and field.optional:
            value = ""
        else:
            try:
                value = field.parse(text)
            except ValidationError as e:
                await self._retry(message, str(e))
                return
        if field.preview and value:
            await self.users.set_state(uid, "admin_preview",
                                       {"key": key, "value": value, "back": field.back})
            body = self._preview_text(key, value)
            await self._send(
                message,
                f"👁 <b>Preview</b> — {esc(field.label)}\n\n{LINE}\n\n{body}\n\n{LINE}\n\n"
                "Save this change?",
                Kb(inline_keyboard=[[B(text="✅ Save", callback_data="adm:save"),
                                     B(text="❌ Cancel", callback_data="adm:cancel")]]))
            return
        await self._save_field(uid, key, value)
        await self._finish(message, field.back, "✅ <b>Saved.</b>\n\n")

    # --- video inputs ---
    async def _i_vsearch(self, message: Message, data: dict, text: str) -> None:
        if not text:
            await self._retry(message, "Send a number or a word to search for.")
            return
        found = await self.videos.search(text, 10)
        await self.users.clear_state(message.from_user.id)
        if not found:
            await self._send(message, "🔎 No videos found.", kb(back="adm:vid"))
            return
        await self._send(message, f"🔎 <b>SEARCH RESULTS</b>\n\n{len(found)} found.",
                         kb(*self._video_rows(found), back="adm:vid"))

    async def _i_vedit(self, message: Message, data: dict, text: str) -> None:
        found = await self.videos.search(text, 10) if text else []
        await self.users.clear_state(message.from_user.id)
        if not found:
            await self._send(message, "Video not found.", kb(back="adm:vid"))
        elif len(found) == 1:
            t, markup = await self.p_vid_view(found[0].id)
            await self._send(message, t, markup)
        else:
            await self._send(message, f"✏️ <b>Which video?</b>\n\n{len(found)} matches.",
                             kb(*self._video_rows(found), back="adm:vid"))

    async def _i_vdel(self, message: Message, data: dict, text: str) -> None:
        if not text.isdigit():
            await self._retry(message, "Send the numeric database ID of the video.")
            return
        video = await self.videos.get(int(text))
        await self.users.clear_state(message.from_user.id)
        if video is None:
            await self._send(message, "Video not found.", kb(back="adm:vid"))
            return
        ask, markup = await self.p_ask(["vdel", str(video.id)])
        await self._send(message, f"{self._video_card(video)}\n\n{ask}", markup)

    async def _i_vnum(self, message: Message, data: dict, text: str) -> None:
        vid = int(data.get("arg", 0) or 0)
        if not text.isdigit() or int(text) < 1 or int(text) > 2_000_000_000:
            await self._retry(message, "Send a whole number of 1 or more.")
            return
        try:
            video = await self.videos.set_number(vid, int(text))
        except ValueError as e:
            await self._retry(message, str(e))
            return
        if video is None:
            await self.users.clear_state(message.from_user.id)
            await self._send(message, "Video not found.", kb(back="adm:vid"))
            return
        await self._log(message.from_user.id, f"video ID {vid} renumbered to {text}")
        await self._finish(message, f"adm:vid:view:{vid}", "✅ <b>Saved.</b>\n\n")

    async def _i_vcap(self, message: Message, data: dict, text: str) -> None:
        vid = int(data.get("arg", 0) or 0)
        if len(text) > 1000:
            await self._retry(message, f"Too long ({len(text)}/1000 characters).")
            return
        video = await self.videos.set_caption(vid, "" if text == "-" else text)
        if video is None:
            await self.users.clear_state(message.from_user.id)
            await self._send(message, "Video not found.", kb(back="adm:vid"))
            return
        await self._log(message.from_user.id, f"video ID {vid} caption edited")
        await self._finish(message, f"adm:vid:view:{vid}", "✅ <b>Saved.</b>\n\n")

    # --- broadcast input ---
    async def _i_bcmsg(self, message: Message, data: dict, text: str) -> None:
        uid = message.from_user.id
        try:
            if message.entities:           # formatting made with Telegram's own toolbar
                body = (message.html_text or "").strip()
                if not body:
                    raise ValidationError("The text cannot be empty.")
                if len(body) > MAX_BODY:
                    raise ValidationError(f"Too long ({len(body)}/{MAX_BODY} characters).")
            else:                          # plain text, or typed <b> <i> <u> <s> <code> tags
                body = sanitize_html(text, MAX_BODY)
        except ValidationError as e:
            await self._retry(message, str(e))
            return
        if not await self.users.set_state(uid, "admin_preview",
                                          {"kind": "bc", "text": body, "back": "adm:bc"}):
            await self._send(message, "One of your video requests is still running. Try again "
                                      "in a moment.")
            return
        audience = await self.broadcast.audience_count()
        await self._send(message, "👁 <b>Preview</b> — this is exactly what users will receive:")
        try:
            await message.answer(render(body), parse_mode=HTML, reply_markup=card_kb())
        except TelegramAPIError:
            await self.users.clear_state(uid)
            await self._retry(message, "Telegram rejected this formatting. Simplify it and "
                                       "send it again.")
            return
        await self._send(
            message, f"📢 Send to <b>{n(audience)}</b> users?",
            Kb(inline_keyboard=[
                [B(text="🚀 Send to everyone", callback_data="adm:bc:go")],
                [B(text="🧪 Send test to me", callback_data="adm:bc:test"),
                 B(text="✏️ Edit", callback_data="adm:bc:new")],
                [B(text="❌ Cancel", callback_data="adm:cancel")]]))

    # --- user inputs ---
    async def _i_usearch(self, message: Message, data: dict, text: str) -> None:
        user = await self.users.find(text) if text else None
        await self.users.clear_state(message.from_user.id)
        if user is None:
            await self._send(message, "User not found. Only people who have written to the bot "
                                      "can be searched.", kb(back="adm:usr"))
            return
        t, markup = await self.p_usr_view(user["user_id"])
        await self._send(message, t, markup)

    @staticmethod
    def _parse_id(text: str) -> int | None:
        return int(text) if re.fullmatch(r"[0-9]{1,15}", text) else None

    async def _i_ublock(self, message: Message, data: dict, text: str) -> None:
        target = self._parse_id(text)
        if target is None:
            await self._retry(message, "Send a numeric Telegram user ID.")
            return
        await self.users.clear_state(message.from_user.id)
        if target in self.cfg.admin_ids:
            await self._send(message, "Admins can't be blocked.", kb(back="adm:usr"))
            return
        ask, markup = await self.p_ask(["ban", str(target)])
        await self._send(message, ask, markup)

    async def _i_uunblock(self, message: Message, data: dict, text: str) -> None:
        target = self._parse_id(text)
        if target is None:
            await self._retry(message, "Send a numeric Telegram user ID.")
            return
        await self.users.set_banned(target, False)
        await self._log(message.from_user.id, f"user {target} unblocked")
        await self._finish(message, f"adm:usr:view:{target}", "✅ <b>User unblocked.</b>\n\n")

    # --- custom button inputs ---
    async def _custom_update(self, message: Message, **changes) -> None:
        c = self.settings.custom_button()
        c.update(changes)
        await self.settings.save_custom_button(c)
        await self._log(message.from_user.id, "custom button updated")
        await self._finish(message, "adm:cust", "✅ <b>Saved.</b>\n\n")

    async def _i_cname(self, message: Message, data: dict, text: str) -> None:
        try:
            name = _plain("Name", 24)(text)
        except ValidationError as e:
            await self._retry(message, str(e))
            return
        await self._custom_update(message, name=name)

    async def _i_cemoji(self, message: Message, data: dict, text: str) -> None:
        try:
            emoji = _emoji(text)
        except ValidationError as e:
            await self._retry(message, str(e))
            return
        await self._custom_update(message, emoji=emoji)

    async def _i_curl(self, message: Message, data: dict, text: str) -> None:
        try:
            url = validate_url(text)
        except ValidationError as e:
            await self._retry(message, str(e))
            return
        await self._custom_update(message, kind="url", target=url)

    # ----- scanner control -----
    @staticmethod
    def _scan_kb() -> Kb:
        return Kb(inline_keyboard=[[B(text="⏹️ Stop Scan", callback_data="adm:scn:stop"),
                                    B(text="🏠 Dashboard", callback_data="adm:home")]])

    async def _start_scan(self, cb: CallbackQuery, full: bool) -> None:
        if not self.scanner.enabled:
            await cb.answer("❌ Channel scanner is not configured. Please configure API_ID, "
                            "API_HASH and SESSION_STRING.", show_alert=True)
            return
        if self.scanner.running:
            await cb.answer("⚠️ A channel scan is already in progress.", show_alert=True)
            return
        await cb.answer("🔎 Scanning channel...")
        chat_id = cb.message.chat.id
        try:
            msg = await self.bot.send_message(
                chat_id, f"🔎 <b>Scanning channel...</b>\n\n{'Full' if full else 'Quick'} scan "
                "started. Progress appears here.", reply_markup=self._scan_kb(), parse_mode=HTML)
        except TelegramAPIError:
            return
        self._scan_msg = (chat_id, msg.message_id)
        await self._log(cb.from_user.id, f"{'full' if full else 'quick'} scan requested")
        task = asyncio.create_task(self._scan_job(chat_id, msg.message_id, full))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _scan_job(self, chat_id: int, msg_id: int, full: bool) -> None:
        async def progress(r: ScanReport) -> None:
            if self._scan_msg != (chat_id, msg_id):
                return
            try:
                await self.bot.edit_message_text(
                    f"🔎 <b>Scanning channel...</b>\n\n{self._scan_counts(r)}", chat_id=chat_id,
                    message_id=msg_id, reply_markup=self._scan_kb(), parse_mode=HTML)
            except TelegramAPIError:
                pass

        try:
            r = await self.scanner.run_scan(full=full, progress=progress)
        except ScanBusy:
            await self._scan_report(chat_id, msg_id, "⚠️ A channel scan is already in progress.")
            return
        except Exception:  # noqa: BLE001
            log.exception("panel_scan_crashed")
            await self._scan_report(chat_id, msg_id, "⚠️ The scan stopped unexpectedly. See the logs.")
            return
        if r.status == "failed":
            reason = {"channel_access": "Scanner account cannot access the configured source channel.",
                      "not_configured": "Channel scanner is not configured correctly "
                                        "(API_ID, API_HASH, SESSION_STRING)."}.get(
                r.error_kind or "", "The scan failed. Videos found so far are saved; it is safe "
                                    "to run it again.")
            text = f"❌ <b>Channel scan failed.</b>\n\n{reason}\n\n{self._scan_counts(r)}"
        else:
            head = "✅ <b>Channel scan completed.</b>" if r.status == "completed" \
                else "🛑 <b>Channel scan stopped.</b>"
            text = f"{head}\n\n{self._scan_counts(r)}"
        await self._scan_report(chat_id, msg_id, text)

    async def _scan_report(self, chat_id: int, msg_id: int, text: str) -> None:
        markup = kb(back="adm:scn")
        if self._scan_msg == (chat_id, msg_id):
            self._scan_msg = None
            try:
                await self.bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                                                 reply_markup=markup, parse_mode=HTML)
                return
            except TelegramAPIError:
                pass
        try:
            await self.bot.send_message(chat_id, text, reply_markup=markup, parse_mode=HTML)
        except TelegramAPIError:
            pass


def plain_preview(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value)


# --------------------------------------------------------------------- aiogram wiring
async def awaiting_input(message: Message, users: UserService) -> bool:
    state, _ = await users.get_state(message.from_user.id)
    return state == "admin_input"


@router.message(Command("admin"))
async def cmd_admin(message: Message, panel: Panel) -> None:
    await panel.open_dashboard(message)


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, panel: Panel) -> None:
    await panel.open_broadcast(message)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, users: UserService) -> None:
    state, _ = await users.get_state(message.from_user.id)
    if state in ("admin_input", "admin_preview"):
        await users.clear_state(message.from_user.id)
        await message.answer("Cancelled.")


@router.callback_query(F.data.startswith("adm:"))
async def on_panel_callback(cb: CallbackQuery, panel: Panel) -> None:
    await panel.handle_callback(cb)


@router.message(F.text, ~F.text.startswith("/"), awaiting_input)
async def on_panel_input(message: Message, panel: Panel) -> None:
    await panel.on_input(message)
