"""📢 Force Subscribe Manager - a section of the Admin Panel.

This is a mixin for handlers.admin_panel.Panel, so the existing panel code is barely touched.
It uses the panel's own machinery (callback routing `adm:fsub:...`, the database-backed text
input flow, the admin-only checks), so every action is already restricted to ADMIN_IDS on the
server before any method here runs.

Pages     adm:fsub                  main menu
          adm:fsub:list             📋 Manage Channels
          adm:fsub:pick:edit        ✏️ Edit Channel (choose a channel)
          adm:fsub:pick:rm          🗑️ Remove Channel (choose a channel)
          adm:fsub:edit:<id>        one channel: details + actions
          adm:fsub:rm:<id>          remove confirmation
Actions   adm:fsub:add | ref | tgl:<id> | ctitle:<id> | clink:<id> | rmok:<id>
"""
from __future__ import annotations

import logging
import re

from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup as Kb, Message

import ui
from force_subscribe import bot_channel_status
from services.force_sub_service import MAX_CHANNELS, DuplicateChannel, TooManyChannels

log = logging.getLogger("fsub_panel")
HTML = "HTML"
LINE = ui.LINE
esc = ui.html_escape

_ID = re.compile(r"-100\d{5,}")
_USERNAME = re.compile(r"@?([A-Za-z][A-Za-z0-9_]{3,31})")
_TME_USER = re.compile(r"(?:https?://)?(?:t|telegram)\.me/([A-Za-z][A-Za-z0-9_]{3,31})/?")
_TME_LINK = re.compile(r"https://(?:t|telegram)\.me/(?:\+[A-Za-z0-9_\-]{6,}|joinchat/[A-Za-z0-9_\-]{6,}"
                       r"|[A-Za-z][A-Za-z0-9_]{3,31})/?")


def parse_channel_ref(text: str) -> str | int:
    """@username, t.me/username or a -100... id. Raises ValueError otherwise."""
    t = text.strip()
    if _ID.fullmatch(t):
        return int(t)
    m = _TME_USER.fullmatch(t) or _USERNAME.fullmatch(t)
    if m:
        return "@" + m.group(1)
    raise ValueError("Send the channel as <code>@username</code>, a <code>https://t.me/username</code> "
                     "link, or its numeric ID (<code>-100…</code>).")


def parse_join_link(text: str) -> str:
    t = text.strip()
    if not _TME_LINK.fullmatch(t):
        raise ValueError("Send a Telegram link like <code>https://t.me/YourChannel</code> or "
                         "<code>https://t.me/+AbCdEf…</code>")
    return t


def _kb(*rows: list[B], back: str = "adm:fsub") -> Kb:
    return Kb(inline_keyboard=[*rows, [B(text="⬅️ Back", callback_data=back),
                                       B(text="🏠 Dashboard", callback_data="adm:home")]])


class ForceSubPanelMixin:
    # Set by Panel.__init__ (force_sub=...). health: chat_id -> last known bot status text.
    force_sub = None
    _fsub_health: dict

    # ------------------------------------------------------------------ pages
    async def p_fsub(self, a: list[str]) -> tuple[str, Kb]:
        sub = a[0] if a else ""
        arg = a[1] if len(a) > 1 else ""
        svc = self.force_sub
        if svc is None:
            return "⚠️ Force Subscribe is not available.", _kb(back="adm:home")
        if sub == "list":
            return self._p_fsub_list()
        if sub == "pick" and arg in ("edit", "rm"):
            return self._p_fsub_pick(arg)
        if sub in ("edit", "rm") and arg.isdigit() and svc.get(int(arg)):
            return self._p_fsub_edit(int(arg)) if sub == "edit" else self._p_fsub_rm(int(arg))
        return self._p_fsub_home()

    def _health_icon(self, chat_id: int) -> str:
        return getattr(self, "_fsub_health", {}).get(chat_id, "⚪")

    def _p_fsub_home(self) -> tuple[str, Kb]:
        chans = self.force_sub.all()
        on = [c for c in chans if c.enabled]
        state = (f"🟢 Active — users must join <b>{len(on)}</b> channel{'s' if len(on) != 1 else ''}"
                 if on else "🔴 Off — no enabled channel, everybody can use the bot")
        text = (f"📢 <b>FORCE SUBSCRIBE MANAGER</b>\n\n{LINE}\n\n"
                f"Status: {state}\n\n"
                f"📋 Channels saved: <b>{len(chans)}</b> / {MAX_CHANNELS}\n\n"
                f"Membership is checked live on every message and button press. "
                f"The bot must be an <b>administrator</b> of each channel.\n\n{LINE}")
        rows = [
            [B(text="➕ Add New Channel", callback_data="adm:fsub:add"),
             B(text="📋 Manage Channels", callback_data="adm:fsub:list")],
            [B(text="✏️ Edit Channel", callback_data="adm:fsub:pick:edit"),
             B(text="🗑️ Remove Channel", callback_data="adm:fsub:pick:rm")],
            [B(text="🔄 Refresh Channel List", callback_data="adm:fsub:ref")],
            [B(text="🔙 Back to Admin Panel", callback_data="adm:home")],
        ]
        return text, Kb(inline_keyboard=rows)

    def _p_fsub_list(self) -> tuple[str, Kb]:
        chans = self.force_sub.all()
        if not chans:
            return ("📋 <b>MANAGE CHANNELS</b>\n\nNo channels yet. Press <b>➕ Add New Channel</b>.",
                    _kb([B(text="➕ Add New Channel", callback_data="adm:fsub:add")]))
        lines = []
        for i, c in enumerate(chans, 1):
            lines.append(f"{i}. {'✅' if c.enabled else '⏸'} <b>{esc(c.title)}</b>\n"
                         f"    {self._health_icon(c.chat_id)} <code>{c.chat_id}</code>")
        text = (f"📋 <b>MANAGE CHANNELS</b>\n\n{LINE}\n\n" + "\n\n".join(lines) +
                f"\n\n{LINE}\n✅ enabled  ⏸ paused  🟢 bot is admin  🔴 problem  ⚪ not checked\n"
                "Tap a channel to manage it.")
        rows = [[B(text=f"{'✅' if c.enabled else '⏸'} {c.title}"[:40],
                   callback_data=f"adm:fsub:edit:{c.id}")] for c in chans]
        rows.append([B(text="🔄 Refresh Channel List", callback_data="adm:fsub:ref")])
        return text, _kb(*rows)

    def _p_fsub_pick(self, mode: str) -> tuple[str, Kb]:
        chans = self.force_sub.all()
        title = "✏️ <b>EDIT CHANNEL</b>" if mode == "edit" else "🗑️ <b>REMOVE CHANNEL</b>"
        if not chans:
            return f"{title}\n\nThere are no channels yet.", _kb()
        rows = [[B(text=c.title[:40], callback_data=f"adm:fsub:{mode}:{c.id}")] for c in chans]
        return f"{title}\n\nChoose a channel:", _kb(*rows)

    def _p_fsub_edit(self, cid: int) -> tuple[str, Kb]:
        c = self.force_sub.get(cid)
        text = (f"✏️ <b>{esc(c.title)}</b>\n\n{LINE}\n\n"
                f"Status: {'✅ Enabled' if c.enabled else '⏸ Paused (not required)'}\n"
                f"Chat ID: <code>{c.chat_id}</code>\n"
                f"Username: {('@' + esc(c.username)) if c.username else '—'}\n"
                f"Join link: {esc(c.url)}\n"
                f"Bot access: {self._health_icon(c.chat_id)}\n\n{LINE}")
        rows = [
            [B(text="⏸ Pause" if c.enabled else "▶️ Enable", callback_data=f"adm:fsub:tgl:{cid}")],
            [B(text="✏️ Change Title", callback_data=f"adm:fsub:ctitle:{cid}"),
             B(text="🔗 Change Link", callback_data=f"adm:fsub:clink:{cid}")],
            [B(text="🗑️ Remove", callback_data=f"adm:fsub:rm:{cid}")],
        ]
        return text, _kb(*rows, back="adm:fsub:list")

    def _p_fsub_rm(self, cid: int) -> tuple[str, Kb]:
        c = self.force_sub.get(cid)
        text = (f"⚠️ <b>Are you sure?</b>\n\nRemove <b>{esc(c.title)}</b> from the required "
                f"channels? Users will no longer need to join it.")
        return text, Kb(inline_keyboard=[[
            B(text="✅ Confirm", callback_data=f"adm:fsub:rmok:{cid}"),
            B(text="❌ Cancel", callback_data=f"adm:fsub:edit:{cid}")]])

    # ------------------------------------------------------------------ callbacks
    async def _r_fsub(self, cb: CallbackQuery, a: list[str]) -> None:
        svc = self.force_sub
        sub = a[0] if a else ""
        arg = a[1] if len(a) > 1 else ""
        cid = int(arg) if arg.isdigit() else None
        uid = cb.from_user.id

        if svc is None:
            await cb.answer("Force Subscribe is not available.", show_alert=True)
            return
        if sub == "add":
            if len(svc.all()) >= MAX_CHANNELS:
                await cb.answer(f"Maximum {MAX_CHANNELS} channels reached.", show_alert=True)
                return
            await self._prompt(
                cb, "fsadd", "Add New Channel",
                "1️⃣ Add this bot as an <b>administrator</b> of the channel first.\n"
                "2️⃣ Send the channel as <code>@username</code>, a <code>t.me</code> link, or its "
                "numeric ID (<code>-100…</code>) for private channels.",
                "adm:fsub", arg="")
            return
        if sub == "ref":
            await svc.load()
            await self._fsub_refresh_health()
            await cb.answer("🔄 Refreshed")
            text, markup = self._p_fsub_list()
            await self._show(cb, text, markup)
            return
        if sub == "tgl" and cid is not None and svc.get(cid):
            c = svc.get(cid)
            await svc.update(cid, enabled=not c.enabled)
            await self._log(uid, f"force-sub channel {c.title!r} {'paused' if c.enabled else 'enabled'}")
            await cb.answer("Done")
            text, markup = self._p_fsub_edit(cid)
            await self._show(cb, text, markup)
            return
        if sub in ("ctitle", "clink") and cid is not None and svc.get(cid):
            if sub == "ctitle":
                await self._prompt(cb, "fstitle", "Channel title",
                                   "Send the name users will see (up to 64 characters).",
                                   f"adm:fsub:edit:{cid}", arg=str(cid))
            else:
                await self._prompt(cb, "fslink", "Join link",
                                   "Send the invite link, e.g. <code>https://t.me/YourChannel</code> "
                                   "or <code>https://t.me/+AbCd…</code>",
                                   f"adm:fsub:edit:{cid}", arg=str(cid))
            return
        if sub == "rmok" and cid is not None:
            c = svc.get(cid)
            if c and await svc.remove(cid):
                await self._log(uid, f"force-sub channel {c.title!r} removed")
                await cb.answer("🗑️ Removed")
            else:
                await cb.answer("Channel not found.", show_alert=True)
            text, markup = self._p_fsub_list()
            await self._show(cb, text, markup)
            return
        # plain page views: home, list, pick, edit, rm
        text, markup = await self.p_fsub(a)
        await cb.answer()
        await self._show(cb, text, markup)

    async def _fsub_refresh_health(self) -> None:
        self._fsub_health = {}
        try:
            me = await self.bot.get_me()
        except TelegramAPIError:
            return
        for c in self.force_sub.all():
            status = await bot_channel_status(self.bot, c.chat_id, me.id)
            self._fsub_health[c.chat_id] = "🟢" if status in ("creator", "administrator") else "🔴"

    # ------------------------------------------------------------------ text input steps
    async def _i_fsadd(self, message: Message, data: dict, text: str) -> None:
        uid = message.from_user.id
        try:
            ref = parse_channel_ref(text)
        except ValueError as e:
            await self._retry(message, str(e))
            return
        try:
            chat = await self.bot.get_chat(ref)
        except TelegramAPIError:
            await self._retry(message, "I can't find that channel. Check the name/ID and make sure "
                                       "the bot has been added to it.")
            return
        if chat.type not in ("channel", "supergroup", "group"):
            await self._retry(message, "That is not a channel or group.")
            return
        if self.force_sub.by_chat(chat.id):
            await self.users.clear_state(uid)
            await self._send(message, "ℹ️ That channel is already in the list.",
                             _kb(back="adm:fsub:list"))
            return
        me = await self.bot.get_me()
        status = await bot_channel_status(self.bot, chat.id, me.id)
        if status not in ("creator", "administrator"):
            await self._retry(message, "The bot is not an administrator of that channel. Make it "
                                       "an admin there first (it needs no special rights), then "
                                       "send the channel again.")
            return
        title = chat.title or "Channel"
        username = getattr(chat, "username", None)
        if username:
            await self._fsub_store(message, chat.id, title, username, f"https://t.me/{username}")
            return
        link = getattr(chat, "invite_link", None)
        if not link:
            try:
                link = await self.bot.export_chat_invite_link(chat.id)
            except TelegramAPIError:
                link = None
        if link and _TME_LINK.fullmatch(link):
            await self._fsub_store(message, chat.id, title, None, link)
            return
        # private channel and the bot cannot create a link: ask the admin for one
        ok = await self.users.set_state(uid, "admin_input", {
            "kind": "fsadd2", "back": "adm:fsub", "arg": "",
            "chat_id": chat.id, "title": title})
        if not ok:
            await self._retry(message, "Try again in a moment.")
            return
        await self._send(message, f"🔗 <b>{esc(title)}</b> is private. Send its invite link "
                                  "(<code>https://t.me/+…</code>) so users can join, or press Cancel.",
                         Kb(inline_keyboard=[[B(text="❌ Cancel", callback_data="adm:cancel")]]))

    async def _i_fsadd2(self, message: Message, data: dict, text: str) -> None:
        try:
            link = parse_join_link(text)
        except ValueError as e:
            await self._retry(message, str(e))
            return
        await self._fsub_store(message, int(data["chat_id"]), str(data["title"]), None, link)

    async def _fsub_store(self, message: Message, chat_id: int, title: str,
                          username: str | None, url: str) -> None:
        uid = message.from_user.id
        try:
            c = await self.force_sub.add(chat_id, title, username, url)
        except DuplicateChannel:
            await self._finish(message, "adm:fsub:list", "ℹ️ <b>That channel is already added.</b>\n\n")
            return
        except TooManyChannels:
            await self._finish(message, "adm:fsub:list", f"⚠️ <b>Maximum {MAX_CHANNELS} channels.</b>\n\n")
            return
        self._fsub_health = {**getattr(self, "_fsub_health", {}), c.chat_id: "🟢"}
        await self._log(uid, f"force-sub channel {c.title!r} added")
        await self._finish(message, "adm:fsub:list", f"✅ <b>{esc(c.title)} added.</b>\n\n")

    async def _i_fstitle(self, message: Message, data: dict, text: str) -> None:
        cid = int(data.get("arg") or 0)
        if not text or len(text) > 64:
            await self._retry(message, "The title must be 1–64 characters.")
            return
        if self.force_sub.get(cid) is None:
            await self._finish(message, "adm:fsub:list", "⚠️ Channel not found.\n\n")
            return
        await self.force_sub.update(cid, title=text)
        await self._log(message.from_user.id, "force-sub channel title changed")
        await self._finish(message, f"adm:fsub:edit:{cid}", "✅ <b>Saved.</b>\n\n")

    async def _i_fslink(self, message: Message, data: dict, text: str) -> None:
        cid = int(data.get("arg") or 0)
        try:
            link = parse_join_link(text)
        except ValueError as e:
            await self._retry(message, str(e))
            return
        if self.force_sub.get(cid) is None:
            await self._finish(message, "adm:fsub:list", "⚠️ Channel not found.\n\n")
            return
        await self.force_sub.update(cid, url=link)
        await self._log(message.from_user.id, "force-sub channel link changed")
        await self._finish(message, f"adm:fsub:edit:{cid}", "✅ <b>Saved.</b>\n\n")
