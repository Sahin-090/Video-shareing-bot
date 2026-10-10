"""All user-facing texts and keyboards in one place (HTML parse mode)."""
from aiogram.types import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb

from config import Config
from services.settings_service import CUSTOM_ACTIONS, SettingsService

LINE = "━━━━━━━━━━━━━━━━━━"
FOOTER = "🎬 <i>VideoShare • Fast • Unique • Simple</i>"
LABEL = {"new": "New", "old": "Old"}
ICON = {"new": "🆕", "old": "📦"}


def other(category: str) -> str:
    return "old" if category == "new" else "new"


def fmt_duration(seconds: int) -> str:
    if seconds % 60 == 0:
        m = seconds // 60
        return f"{m} minute" + ("" if m == 1 else "s")
    return f"{seconds} seconds"


def videos_word(n: int) -> str:
    return "video" if n == 1 else "videos"


# ---------------------------------------------------------------- texts
def _fill(text: str, cfg: Config) -> str:
    """Placeholders an admin may use in custom texts."""
    return (text.replace("{auto_delete}", fmt_duration(cfg.auto_delete_seconds))
                .replace("{max}", str(cfg.max_videos_per_request)))


def default_welcome(cfg: Config, name: str | None = None) -> str:
    hello = f"Hey <b>{name}</b>, you're in. 🚀\n" if name else ""
    return (
        "🔥 <b>WELCOME TO VIDEOSHARE</b> 🔥\n\n"
        f"{hello}"
        "⚡ <i>Instant delivery. Fresh drops. Zero repeats.</i>\n\n"
        f"{LINE}\n\n"
        "🆕 <b>New</b> – the latest videos\n"
        "📦 <b>Old</b> – the classic collection\n"
        f"⏱️ Auto-removed after {fmt_duration(cfg.auto_delete_seconds)}\n\n"
        f"{LINE}\n\n"
        "👇 <b>Tap below and let's go!</b>"
    )


def welcome_text(cfg: Config, s: SettingsService | None = None, name: str | None = None) -> str:
    """`name` must already be HTML-escaped. Only the built-in welcome greets by name."""
    custom = s.get("welcome_message") if s else ""
    return _fill(custom, cfg) if custom else default_welcome(cfg, name)


def main_menu_text() -> str:
    return "🏠 <b>Main Menu</b>\n\nWhat would you like to do?"


def ask_count_text(cfg: Config, category: str | None = None, fresh: bool = False) -> str:
    what = f"{LABEL[category]} videos" if category else "videos"
    head = "🔄 <b>New request</b>\n\n" if fresh else ""
    return (f"{head}How many {what} would you like?\n\n"
            f"Send a number from <code>1</code> to <code>{cfg.max_videos_per_request}</code>, "
            "for example <code>5</code>, <code>10</code> or <code>20</code>.")


def invalid_text(cfg: Config) -> str:
    return ("⚠️ <b>Invalid request</b>\n\n"
            f"Please enter a number between <code>1</code> and "
            f"<code>{cfg.max_videos_per_request}</code>.")


def choose_collection_text() -> str:
    return ("🎬 <b>Choose your collection</b>\n\n"
            "🆕 <b>New Videos</b>\nLatest unique videos\n\n"
            "📦 <b>Old Videos</b>\nOlder unique videos")


def default_help(cfg: Config) -> str:
    return (
        "ℹ️ <b>HOW VIDEOSHARE WORKS</b>\n\n"
        "1️⃣ Enter the number of videos you want.\n\n"
        "2️⃣ Choose New or Old.\n\n"
        "3️⃣ Videos are delivered directly to you.\n\n"
        "4️⃣ Use More New / More Old to continue.\n\n"
        "5️⃣ Previously delivered videos are not repeated.\n\n"
        f"⏱️ Videos automatically disappear after {fmt_duration(cfg.auto_delete_seconds)}."
    )


def help_text(cfg: Config, s: SettingsService | None = None) -> str:
    custom = s.get("help_message") if s else ""
    return _fill(custom, cfg) if custom else default_help(cfg)


def default_delete_notice(cfg: Config) -> str:
    return ("⏱️ <b>Auto-delete enabled</b>\n\n"
            f"Videos sent by this bot are automatically removed after "
            f"<b>{fmt_duration(cfg.auto_delete_seconds)}</b>.")


def delete_notice_text(cfg: Config, s: SettingsService | None = None) -> str:
    custom = s.get("delete_notice") if s else ""
    return _fill(custom, cfg) if custom else default_delete_notice(cfg)


def developer_text(s: SettingsService, bio: str | None = None) -> str:
    d = s.developer()
    bio = (d["bio"] if bio is None else bio) or "Creator &amp; Developer of VideoShare"
    has_links = any(d[k] for k in ("website", "instagram", "telegram", "github"))
    text = (f"👨‍💻 <b>DEVELOPER</b>\n\n<b>{html_escape(d['name'])}</b>\n\n{bio}\n\n{LINE}")
    if has_links:
        text += "\n\n🔗 Connect with the developer"
    return text


def html_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def video_caption(cfg: Config, video, position: int, source_caption: str | None = None) -> str:
    """Short, clean caption. Only our own label is used unless the admin opted in to keep the
    source caption (channel captions can contain channel names or links)."""
    number = video.video_number or position
    collection = {"new": "🆕 New Collection", "old": "📦 Old Collection"}.get(video.category or "", "")
    text = f"🎬 <b>Video #{number}</b>"
    if collection:
        text += f"\n\n{collection}"
    text += f"\n\n⏱️ Auto-delete: {fmt_duration(cfg.auto_delete_seconds)}"
    if source_caption:
        extra = html_escape(source_caption.strip())[:300]
        if extra:
            text += f"\n\n{extra}"
    return text


def preparing_text() -> str:
    return "⏳ <b>Preparing your videos...</b>"


def preparing_n_text(n: int, category: str, short_of: int | None = None) -> str:
    text = f"⏳ <b>Preparing {n} unique {videos_word(n)}...</b>"
    if short_of is not None:
        text = (f"⚠️ <b>Only {n} {category} {videos_word(n)} are available that you "
                f"haven't received yet.</b>\n\n") + text
    return text


def delivered_text(cfg: Config, n: int, category: str, short_of: int | None = None) -> str:
    text = f"✅ <b>{n} unique {videos_word(n)} delivered.</b>"
    if short_of is not None:
        text += (f"\n⚠️ Only {n} unseen {LABEL[category]} {videos_word(n)} were left "
                 f"(you asked for {short_of}).")
    text += (f"\n\n⏱️ Videos will automatically disappear after "
             f"{fmt_duration(cfg.auto_delete_seconds)}.")
    return text


def exhausted_text(category: str) -> str:
    if category == "new":
        return ("🆕 <b>You've reached the end of the New collection.</b>\n\n"
                "You've already received all available unique New Videos.\n\n"
                "Try <b>📦 More Old Videos</b> or start a <b>🔄 New Request</b>.")
    return ("📦 <b>No more unseen Old Videos</b>\n\n"
            "You've already received every available unique Old Video.\n\n"
            "Try <b>🆕 New Videos</b> or start a <b>🔄 New Request</b>.")


def activity_text(a: dict) -> str:
    return (
        "📊 <b>YOUR VIDEOSHARE ACTIVITY</b>\n\n"
        f"🎬 Videos received: <b>{a['total']:,}</b>\n"
        f"🆕 New videos: {a['new']:,}\n"
        f"📦 Old videos: {a['old']:,}\n\n"
        f"🔄 Requests: <b>{a['requests']:,}</b>\n\n"
        "✨ <b>Unique videos remaining:</b>\n"
        f"New: {a['left_new']:,}\n"
        f"Old: {a['left_old']:,}\n\n"
        f"{FOOTER}"
    )


BUSY_TEXT = "⏳ Your previous request is still being processed."
RATE_TEXT = "⏳ Please wait a moment before requesting another batch."
FAILED_TEXT = "⚠️ I couldn't send the videos right now. Please try again in a moment."
ERROR_TEXT = "⚠️ Something went wrong. Please try again later."
EXPIRED_TEXT = "This request has expired. Send a number again."


# ---------------------------------------------------------------- keyboards
def custom_button(s: SettingsService) -> tuple[Btn, str] | None:
    """The admin-defined public button (if enabled and valid) and where it goes."""
    c = s.custom_button()
    if not c["enabled"] or not s.custom_ready(c):
        return None
    label = f"{c['emoji']} {c['name']}".strip()
    if c["kind"] == "url":
        return Btn(text=label, url=c["target"]), c["position"]
    return Btn(text=label, callback_data=CUSTOM_ACTIONS[c["target"]][1]), c["position"]


def _public_rows(s: SettingsService, *, with_activity: bool) -> list[list[Btn]]:
    on = s.button_on
    rows: list[list[Btn]] = []
    if on("btn_request"):
        rows.append([Btn(text="🎬 Request Videos", callback_data="menu:request")])
    pair = []
    if on("btn_new"):
        pair.append(Btn(text="🆕 New Videos", callback_data="menu:new"))
    if on("btn_old"):
        pair.append(Btn(text="📦 Old Videos", callback_data="menu:old"))
    if pair:
        rows.append(pair)
    if on("btn_more"):
        rows.append([Btn(text="🔥 More Videos", callback_data="menu:more")])
    pair = []
    if on("btn_developer"):
        pair.append(Btn(text="👨‍💻 Developer", callback_data="dev"))
    if on("btn_help"):
        pair.append(Btn(text="ℹ️ Help", callback_data="help"))
    if pair:
        rows.append(pair)
    if with_activity:
        rows.append([Btn(text="📊 My Requests", callback_data="activity")])
    custom = custom_button(s)
    if custom:
        btn, position = custom
        rows.insert(0, [btn]) if position == "top" else rows.append([btn])
    return rows


def _admin_row(is_admin: bool) -> list[list[Btn]]:
    return [[Btn(text="🛠️ Admin Panel", callback_data="adm:home")]] if is_admin else []


def welcome_kb(s: SettingsService, is_admin: bool = False) -> Kb:
    rows = _public_rows(s, with_activity=False) + _admin_row(is_admin)
    return Kb(inline_keyboard=rows or [[Btn(text="🏠 Main Menu", callback_data="menu")]])


def main_menu_kb(s: SettingsService, is_admin: bool = False) -> Kb:
    return Kb(inline_keyboard=_public_rows(s, with_activity=True) + _admin_row(is_admin))


def more_kb() -> Kb:
    return Kb(inline_keyboard=[
        [Btn(text="🔥 More New", callback_data="mmore:new"),
         Btn(text="♻️ More Old", callback_data="mmore:old")],
        [Btn(text="⬅️ Back", callback_data="menu")],
    ])


def help_kb(s: SettingsService) -> Kb:
    rows = []
    if s.button_on("btn_request"):
        rows.append([Btn(text="🎬 Request Videos", callback_data="menu:request")])
    elif s.button_on("btn_new") or s.button_on("btn_old"):
        rows.append([b for b in (
            Btn(text="🆕 New Videos", callback_data="menu:new") if s.button_on("btn_new") else None,
            Btn(text="📦 Old Videos", callback_data="menu:old") if s.button_on("btn_old") else None,
        ) if b])
    if s.button_on("btn_developer"):
        rows.append([Btn(text="👨‍💻 Developer", callback_data="dev")])
    rows.append([Btn(text="⬅️ Back", callback_data="menu")])
    return Kb(inline_keyboard=rows)


def developer_kb(s: SettingsService) -> Kb:
    d = s.developer()
    links = [("🌐 Website", d["website"]), ("📸 Instagram", d["instagram"]),
             ("💬 Telegram", d["telegram"]), ("🐙 GitHub", d["github"])]
    buttons = [Btn(text=label, url=url) for label, url in links if url]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([Btn(text="⬅️ Back", callback_data="menu")])
    return Kb(inline_keyboard=rows)


def collection_kb() -> Kb:
    return Kb(inline_keyboard=[[
        Btn(text="🆕 New Videos", callback_data="type:new"),
        Btn(text="📦 Old Videos", callback_data="type:old"),
    ]])


def after_delivery_kb(category: str, count: int, s: SettingsService | None = None) -> Kb:
    show_more = s is None or s.button_on("btn_more")
    show_dev = s is None or s.button_on("btn_developer")
    rows: list[list[Btn]] = []
    if show_more:
        rows.append([Btn(text="🔥 More New", callback_data=f"more:new:{count}"),
                     Btn(text="♻️ More Old", callback_data=f"more:old:{count}")])
    rows.append([Btn(text="✨ Fresh Batch", callback_data=f"fresh:{category}:{count}")])
    last = [Btn(text="🔄 New Request", callback_data="again")]
    if show_dev:
        last.append(Btn(text="👨‍💻 Developer", callback_data="dev"))
    rows.append(last)
    rows.append([Btn(text="🏠 Main Menu", callback_data="menu")])
    return Kb(inline_keyboard=rows)


def try_other_kb(category: str, count: int) -> Kb:
    o = other(category)
    label = "📦 Try Old Videos" if o == "old" else "🆕 Try New Videos"
    return Kb(inline_keyboard=[
        [Btn(text=label, callback_data=f"more:{o}:{count}"),
         Btn(text="🔄 Request Again", callback_data="again")],
        [Btn(text="🏠 Main Menu", callback_data="menu")],
    ])


def retry_kb(category: str, count: int) -> Kb:
    return Kb(inline_keyboard=[
        [Btn(text="🔄 Try Again", callback_data=f"more:{category}:{count}"),
         Btn(text="🏠 Main Menu", callback_data="menu")],
    ])


def back_kb(s: SettingsService | None = None) -> Kb:
    rows = []
    if s is None or s.button_on("btn_request"):
        rows.append(Btn(text="🎬 Request Videos", callback_data="menu:request"))
    rows.append(Btn(text="🏠 Main Menu", callback_data="menu"))
    return Kb(inline_keyboard=[rows])
