import logging

import asyncio

from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Config
from logging_setup import log_event
from models.video import ChannelVideo, Video
from services.bridge_service import BridgeService
from services.scanner_service import ScanBusy, ScannerService, ScanReport
from services.user_service import UserService
from services.video_service import VideoService

log = logging.getLogger("admin")
router = Router(name="admin")
PAGE = 20
CATS = ("new", "old")


async def is_admin(event, cfg: Config) -> bool:
    user = getattr(event, "from_user", None)
    return bool(user and user.id in cfg.admin_ids)


async def is_ingest_channel(message: Message, cfg: Config) -> bool:
    return (bool(cfg.source_channel_ids)
            and cfg.channel_auto_category is not None
            and message.chat.id in cfg.source_channel_ids)


router.message.filter(F.chat.type == "private", is_admin)
router.callback_query.filter(is_admin)

HELP = (
    "🛠 Admin commands\n\n"
    "/admin – open the Admin Panel (buttons for everything below and much more)\n"
    "/broadcast – send a modern announcement to all users\n"
    "/addvideo – how to add videos\n"
    "/videos – video statistics; /videos new|old|pending|all [page] lists videos\n"
    "/editvideo <id> number <n> | category <new|old> | caption <text>\n"
    "/deletevideo <id> – delete a video\n"
    "/batch new|old|off – auto-save every video you send into one category\n"
    "/scan – full scan of the whole channel history (finds every existing video)\n"
    "/scanstatus – scanner status\n"
    "/bridge – optional: store Bot API file_ids (forwards + deletes videos; off by default)\n"
    "/scanstop – stop a running scan\n"
    "/scanlegacy <new|old> [from_id] [to_id] – old forward-based import (no API keys needed)\n"
    "/stats – library, delivery and scan statistics\n"
    "/resethistory <user_id> – let a user receive videos again (clears their history)\n"
    "/help – this message"
)


def _fmt(v: Video) -> str:
    flag = "✅" if v.is_active else "⚠️"
    cap = (v.caption or "").replace("\n", " ")[:30]
    return f"{flag} ID {v.id} | #{v.video_number or '-'} | {v.category or 'pending'} | {cap}"


async def _reply(message: Message, text: str, **kw) -> None:
    try:
        await message.reply(text, **kw)
    except TelegramAPIError:
        pass


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("addvideo"))
async def cmd_addvideo(message: Message) -> None:
    await message.answer(
        "➕ Send or forward a video to this chat.\n"
        "I'll ask you to choose 🆕 New or 📦 Old, then save it.\n\n"
        "Adding many? Use /batch new or /batch old first."
    )


@router.message(F.video)
async def on_admin_video(message: Message, videos: VideoService, users: UserService) -> None:
    uid = message.from_user.id
    v = message.video
    state, data = await users.get_state(uid)

    video = await videos.add_pending(v.file_id, v.file_unique_id, message.caption, uid)
    if video is None:
        await _reply(message, "⚠️ This video is already in the library.")
        return
    if video.category is not None:
        await _reply(message, f"♻️ Video #{video.video_number} ({video.category}) restored with a fresh file_id.")
        log_event(log, logging.INFO, "admin_video_restored", admin_id=uid, video_id=video.id)
        return

    if state == "admin_batch" and data.get("category") in CATS:
        saved = await videos.assign_category(video.id, data["category"], only_pending=True)
        await users.set_state(uid, "admin_batch", data)  # keep batch mode alive
        if saved:
            log_event(log, logging.INFO, "admin_video_added", admin_id=uid,
                      video_id=saved.id, category=saved.category, number=saved.video_number)
            await _reply(message, f"✅ #{saved.video_number} saved as {saved.category} (ID {saved.id})")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🆕 New", callback_data=f"addcat:{video.id}:new"),
        InlineKeyboardButton(text="📦 Old", callback_data=f"addcat:{video.id}:old"),
    ]])
    await _reply(message, "Select category", reply_markup=kb)


@router.callback_query(F.data.startswith("addcat:"))
async def on_addcat(cb: CallbackQuery, videos: VideoService) -> None:
    try:
        _, vid, cat = cb.data.split(":")
        vid = int(vid)
    except ValueError:
        await cb.answer()
        return
    if cat not in CATS:
        await cb.answer()
        return
    saved = await videos.assign_category(vid, cat, only_pending=True)
    if not saved:
        await cb.answer("Already saved or no longer exists.", show_alert=True)
        return
    log_event(log, logging.INFO, "admin_video_added", admin_id=cb.from_user.id,
              video_id=saved.id, category=cat, number=saved.video_number)
    await cb.answer("Saved")
    try:
        await cb.message.edit_text(f"✅ Saved as {cat} video #{saved.video_number} (ID {saved.id})")
    except TelegramAPIError:
        pass


@router.message(Command("batch"))
async def cmd_batch(message: Message, command: CommandObject, users: UserService) -> None:
    arg = (command.args or "").strip().lower()
    uid = message.from_user.id
    if arg in CATS:
        await users.set_state(uid, "admin_batch", {"category": arg})
        await message.answer(f"📥 Batch mode: every video you send is saved as {arg}. "
                             "Send /batch off when finished (it also expires after idle time).")
    elif arg == "off":
        await users.clear_state(uid)
        await message.answer("Batch mode off.")
    else:
        await message.answer("Usage: /batch new | old | off")


@router.message(Command("videos"))
async def cmd_videos(message: Message, command: CommandObject, videos: VideoService) -> None:
    if not (command.args or "").strip():
        await message.answer(await _library_stats(videos) + "\n\nList them with /videos all, "
                             "/videos new, /videos old or /videos pending.")
        return
    scope, page = "all", 1
    for p in (command.args or "").split():
        if p.lower() in ("new", "old", "pending", "all"):
            scope = p.lower()
        elif p.isdigit():
            page = max(1, int(p))
    rows = await videos.list_videos(scope, PAGE, (page - 1) * PAGE)
    total = await videos.count(scope)
    if not rows:
        await message.answer("No videos found.")
        return
    pages = (total + PAGE - 1) // PAGE
    await message.answer(f"📚 {scope} videos — page {page}/{pages} ({total} total)\n\n"
                         + "\n".join(_fmt(v) for v in rows))


@router.message(Command("deletevideo"))
async def cmd_delete(message: Message, command: CommandObject, videos: VideoService) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Usage: /deletevideo <id>   (IDs are shown by /videos)")
        return
    if await videos.delete(int(arg)):
        log_event(log, logging.INFO, "admin_video_deleted", admin_id=message.from_user.id, video_id=int(arg))
        await message.answer(f"🗑 Video ID {arg} deleted.")
    else:
        await message.answer("Video not found.")


@router.message(Command("editvideo"))
async def cmd_edit(message: Message, command: CommandObject, videos: VideoService) -> None:
    usage = ("Usage:\n/editvideo <id> number <n>\n/editvideo <id> category <new|old>\n"
             "/editvideo <id> caption <text>")
    parts = (command.args or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[0].isdigit():
        await message.answer(usage)
        return
    vid, field, value = int(parts[0]), parts[1].lower(), parts[2].strip()
    try:
        if field == "number" and value.isdigit() and int(value) > 0:
            video = await videos.set_number(vid, int(value))
        elif field == "category" and value.lower() in CATS:
            video = await videos.assign_category(vid, value.lower(), lock=True)
        elif field == "caption":
            video = await videos.set_caption(vid, value[:1000])
        else:
            await message.answer(usage)
            return
    except ValueError as e:
        await message.answer(f"⚠️ {e}")
        return
    if not video:
        await message.answer("Video not found.")
        return
    log_event(log, logging.INFO, "admin_video_edited", admin_id=message.from_user.id,
              video_id=vid, field=field)
    await message.answer("✏️ Updated:\n" + _fmt(video))


def _n(value: int) -> str:
    return f"{value:,}"


async def _library_stats(videos: VideoService) -> str:
    s = await videos.stats()

    def count(cat: str, active: bool = True) -> int:
        return sum(r["n"] for r in s["videos"] if r["cat"] == cat and r["is_active"] == active)

    new, old = count("new"), count("old")
    return (
        "📊 VideoShare Statistics\n\n"
        f"🎬 Total unique videos: {_n(new + old)}\n"
        f"🆕 New: {_n(new)}\n"
        f"📦 Old: {_n(old)}\n"
        f"♻️ Duplicate records prevented: {_n(s['duplicates_prevented'])}"
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message, videos: VideoService, cfg: Config) -> None:
    s = await videos.stats()
    d = await videos.delivery_stats()

    def count(cat: str) -> int:
        return sum(r["n"] for r in s["videos"] if r["cat"] == cat and r["is_active"])

    new, old = count("new"), count("old")
    invalid = sum(r["n"] for r in s["videos"] if not r["is_active"])
    pending = sum(r["n"] for r in s["videos"] if r["cat"] == "pending")
    secs = cfg.auto_delete_seconds
    delete_label = f"{secs // 60} minutes" if secs % 60 == 0 and secs != 60 else (
        "1 minute" if secs == 60 else f"{secs} seconds")
    lines = [
        "📊 VIDEOSHARE ADMIN STATS", "",
        f"🎬 Total unique videos: {_n(new + old)}", "",
        f"🆕 New: {_n(new)}",
        f"📦 Old: {_n(old)}", "",
        f"👥 Users: {_n(s['users'])}", "",
        f"📨 Total deliveries: {_n(d['deliveries'])}", "",
        f"♻️ Duplicate deliveries prevented: {_n(d['prevented'])}", "",
        f"⏱️ Auto-delete enabled: {delete_label}", "",
        "🔎 Last scan:",
    ]
    scan = d["scan"]
    if scan and (scan["videos_found"] or scan["status"] != "idle"):
        lines += [f"Videos found: {_n(scan['videos_found'])}",
                  f"New added: {_n(scan['videos_added'])}",
                  f"Duplicates skipped: {_n(scan['duplicates'])}"]
    else:
        lines.append("No scan has run yet")
    extra = []
    if invalid:
        extra.append(f"⚠️ Invalid (deactivated): {_n(invalid)}")
    if pending:
        extra.append(f"⏳ Waiting for a category: {_n(pending)}")
    extra += [f"📤 Requests: {_n(s['requests'])}",
              f"♻️ Duplicate records prevented while scanning: {_n(s['duplicates_prevented'])}"]
    await message.answer("\n".join(lines + [""] + extra))


@router.message(Command("resethistory"))
async def cmd_resethistory(message: Message, command: CommandObject, videos: VideoService) -> None:
    arg = (command.args or "").strip()
    if not arg.lstrip("-").isdigit():
        await message.answer("Usage: /resethistory <user_id>")
        return
    removed = await videos.reset_user_history(int(arg))
    log_event(log, logging.INFO, "admin_history_reset", admin_id=message.from_user.id,
              target=int(arg), removed=removed)
    await message.answer(f"♻️ History of user {arg} cleared ({_n(removed)} videos). "
                         "They can receive those videos again.")


# Optional: auto-ingest videos posted in the private channel (bot must be channel admin)
@router.channel_post(F.video | F.document, is_ingest_channel)
async def on_channel_video(message: Message, videos: VideoService) -> None:
    """A new video was posted in the source channel: register it right away.
    Duplicates (same video, same post, restarts, re-delivered updates) are ignored by the
    database unique indexes. The periodic incremental scan catches anything missed."""
    v = message.video
    if v is None:
        doc = message.document
        if doc is None or not (doc.mime_type or "").startswith("video/"):
            return
        v = doc
    item = ChannelVideo(
        channel_id=message.chat.id,
        message_id=message.message_id,
        file_unique_id=v.file_unique_id,
        file_id=v.file_id,
        message_date=message.date,
        caption=message.caption,
        duration=getattr(v, "duration", None),
        file_size=v.file_size,
        mime_type=v.mime_type,
        width=getattr(v, "width", None),
        height=getattr(v, "height", None),
        category=videos.category_for(message.date),
    )
    try:
        res = await videos.upsert_channel_videos([item])
    except Exception:
        log.exception("channel_video_ingest_failed")
        return
    log_event(log, logging.INFO, "channel_video_detected", message_id=item.message_id,
              added=res.added, duplicate=res.duplicates)


# ---------- /scan: import videos that already exist in the private channel ----------
_scan = {"running": False, "stop": False}
_tasks: set = set()


@router.message(Command("scanstop"))
async def cmd_scanstop(message: Message, scanner: ScannerService) -> None:
    if scanner.request_stop():
        await message.answer("🛑 Stopping scan... progress so far is saved.")
    elif _scan["running"]:
        _scan["stop"] = True
        await message.answer("🛑 Stopping scan...")
    else:
        await message.answer("No scan is running.")


@router.message(Command("scanlegacy"))
async def cmd_scan_legacy(message: Message, command: CommandObject, bot: Bot,
                          videos: VideoService, cfg: Config) -> None:
    args = (command.args or "").split()
    if cfg.source_channel_id is None:
        await message.answer("⚠️ Set SOURCE_CHANNEL_ID first (bot must be an admin of the channel).")
        return
    if not args or args[0].lower() not in CATS:
        await message.answer("Usage: /scanlegacy <new|old> [from_id] [to_id]\n"
                             "Example: /scanlegacy new   (scans from the first post until the end)\n"
                             "It works on the FIRST source channel only (SOURCE_CHANNEL_IDS).\n"
                             "Tip: /scan scans the whole history much faster once API_ID, API_HASH "
                             "and SESSION_STRING are set.")
        return
    if _scan["running"]:
        await message.answer("A scan is already running. Use /scanstop to cancel it.")
        return
    nums = [int(a) for a in args[1:] if a.isdigit()]
    start = max(1, nums[0]) if nums else 1
    end = nums[1] if len(nums) > 1 else None
    await message.answer(f"🔍 Scanning channel posts from #{start} as {args[0].lower()} videos. "
                         "This takes ~1 second per post; I'll report progress.")
    task = asyncio.create_task(_run_scan(bot, message.chat.id, cfg.source_channel_id,
                                         args[0].lower(), start, end, videos))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _say(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except TelegramAPIError:
        pass


async def _run_scan(bot: Bot, admin_chat: int, channel_id: int, category: str,
                    start: int, end: int | None, videos: VideoService) -> None:
    """Bots can't read channel history, but they can forward a post by id.
    The forwarded copy carries the video's file_id for THIS bot; we save it and delete the copy."""
    _scan.update(running=True, stop=False)
    saved = dup = skipped = misses = 0
    msg_id = start - 1
    aborted: str | None = None
    try:
        while True:
            msg_id += 1
            if _scan["stop"] or (end is not None and msg_id > end):
                break
            if end is None and misses >= 50:  # ran past the last post
                break
            try:
                fwd = await bot.forward_message(admin_chat, channel_id, msg_id,
                                                disable_notification=True)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                msg_id -= 1
                continue
            except TelegramForbiddenError:
                aborted = "The bot has no access to the channel."
                break
            except TelegramBadRequest as e:
                if "chat not found" in str(e).lower():
                    aborted = "Channel not found. Check SOURCE_CHANNEL_ID and make the bot a channel admin."
                    break
                misses += 1  # deleted / non-existent / non-forwardable post
                await asyncio.sleep(0.15)
                continue
            except TelegramAPIError:
                misses += 1
                await asyncio.sleep(1)
                continue

            misses = 0
            v = fwd.video
            if v is None:
                skipped += 1
            else:
                video = await videos.add_pending(v.file_id, v.file_unique_id, None, None)
                if video is None:
                    dup += 1
                else:
                    if video.category is None:
                        await videos.assign_category(video.id, category, only_pending=True)
                    saved += 1
            try:
                await bot.delete_message(admin_chat, fwd.message_id)
            except TelegramAPIError:
                pass
            await asyncio.sleep(1.0)
            if msg_id % 50 == 0:
                await _say(bot, admin_chat, f"… at post #{msg_id}: {saved} saved so far")
    except Exception:
        log.exception("scan_crashed")
        aborted = "Unexpected error (see server logs)."
    finally:
        _scan["running"] = False
    log_event(log, logging.INFO, "channel_scan_finished", saved=saved, duplicates=dup,
              skipped=skipped, last_id=msg_id, category=category)
    head = f"⚠️ Scan stopped: {aborted}\n" if aborted else "✅ Scan finished.\n"
    await _say(bot, admin_chat, f"{head}Saved: {saved}\nAlready in library: {dup}\n"
                                f"Not videos: {skipped}\nLast post checked: #{msg_id}")


# ---------- /scan: full history scan through the MTProto client ----------
def _progress_text(r: ScanReport) -> str:
    return (f"🔎 Scanned: {_n(r.scanned)} messages\n"
            f"🎬 Videos found: {_n(r.found)}\n"
            f"➕ New videos added: {_n(r.added)}\n"
            f"🔁 Re-posted copies stored: {_n(r.repeated)}\n"
            f"♻️ Already registered: {_n(r.duplicates)}")


@router.message(Command("scan"))
async def cmd_scan(message: Message, bot: Bot, scanner: ScannerService, bridge: BridgeService,
                   cfg: Config) -> None:
    if not scanner.enabled:
        await message.answer("❌ Channel scanner is not configured. "
                             "Please configure API_ID, API_HASH and SESSION_STRING.")
        return
    if scanner.running:
        await message.answer("⚠️ A channel scan is already in progress.")
        return
    status = await message.answer("🔎 Starting full channel scan...")
    # Background task: the bot keeps answering /start and video requests meanwhile.
    task = asyncio.create_task(
        _run_full_scan(bot, message.chat.id, status.message_id, scanner, bridge, cfg))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _run_full_scan(bot: Bot, chat_id: int, status_msg_id: int,
                         scanner: ScannerService, bridge: BridgeService, cfg: Config) -> None:
    last_text = ""

    async def progress(r: ScanReport) -> None:
        nonlocal last_text
        text = _progress_text(r)
        if text == last_text:
            return
        last_text = text
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=status_msg_id)
        except TelegramAPIError:
            pass  # progress is cosmetic (flood limits etc.)

    try:
        r = await scanner.run_scan(full=True, progress=progress)
    except ScanBusy:
        await _say(bot, chat_id, "⚠️ A channel scan is already in progress.")
        return

    if r.status == "failed":
        if r.error_kind == "channel_access":
            text = "❌ Scanner account cannot access the configured source channel."
        elif r.error_kind == "not_configured":
            text = ("❌ Channel scanner is not configured. "
                    "Please configure API_ID, API_HASH and SESSION_STRING.\n\n"
                    f"Reason: {r.error}")
        else:
            text = (f"❌ Channel scan failed: {r.error}\n\n"
                    f"Videos found so far are saved ({_n(r.added)} added). "
                    "Run /scan again; it is safe to repeat.")
    else:
        head = "✅ Channel scan completed." if r.status == "completed" else "🛑 Channel scan stopped."
        text = (f"{head}\n\n"
                f"📦 Messages scanned: {_n(r.scanned)}\n"
                f"🎬 Videos found: {_n(r.found)}\n"
                f"➕ New videos added: {_n(r.added)}\n"
                f"🔁 Re-posted copies stored: {_n(r.repeated)}\n"
                f"♻️ Already registered: {_n(r.duplicates)}\n"
                f"⚠️ Errors: {_n(r.errors)}")
    await _say(bot, chat_id, text)

    if r.status == "completed" and cfg.source_channel_ids:
        # The bot must be able to copy the posts to users. This only checks access: nothing is
        # forwarded or deleted. (The old automatic file_id bridge made videos flash in and out
        # of the admin chat after every scan, so it no longer runs by itself; use /bridge.)
        blocked = []
        for i, channel in enumerate(cfg.source_channel_ids, 1):
            try:
                await bot.get_chat(channel)
            except TelegramAPIError:
                blocked.append(i)
        if blocked:
            which = ", ".join(f"#{i}" for i in blocked)
            await _say(bot, chat_id,
                       f"⚠️ The scan worked, but the bot itself cannot access source channel "
                       f"{which} (position in SOURCE_CHANNEL_IDS), so it cannot copy those posts "
                       "to users. Add the bot to the channel as an administrator and run /scan again.")
    if r.status == "completed" and r.error:
        await _say(bot, chat_id, f"⚠️ {r.error}")


@router.message(Command("bridge"))
async def cmd_bridge(message: Message, bot: Bot, bridge: BridgeService, videos: VideoService,
                     cfg: Config) -> None:
    """Optional, manual: only this command forwards+deletes videos (never automatic)."""
    if bridge.running:
        await message.answer("⚠️ The file_id bridge is already running.")
        return
    todo = await videos.count_without_file_id()
    if not todo:
        await message.answer("✅ Every scanned video already has a Bot API file_id.")
        return
    target = cfg.bridge_chat_id or message.chat.id
    await message.answer(f"🔗 Registering file_ids for {_n(todo)} videos. They are forwarded "
                         "one by one to the bridge chat and deleted at once. Stop by restarting.")

    async def job() -> None:
        rep = await bridge.run(target)
        if rep is not None:
            await _say(bot, message.chat.id, BridgeService.summary(rep))

    task = asyncio.create_task(job())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@router.message(Command("scanstatus"))
async def cmd_scanstatus(message: Message, scanner: ScannerService, cfg: Config) -> None:
    row = await scanner.status_row()
    live = scanner.current if scanner.running else None
    if not scanner.enabled:
        await message.answer("🔎 Scanner: not configured (API_ID, API_HASH, SESSION_STRING, "
                             "SOURCE_CHANNEL_ID).")
        return
    state = "running ▶️" if scanner.running else row["status"]
    scanned = live.scanned if live else row["messages_scanned"]
    found = live.found if live else row["videos_found"]
    added = live.added if live else row["videos_added"]
    dupes = live.duplicates if live else row["duplicates"]
    auto = (f"every {cfg.auto_scan_minutes} min" if cfg.auto_scan_minutes else "off")
    started, finished = row["started_at"], row["finished_at"]
    lines = [
        "🔎 Scanner status", "",
        f"State: {state}",
        f"Mode: {row['mode'] or '-'}",
        f"📦 Messages scanned: {_n(scanned)}",
        f"🎬 Videos found: {_n(found)}",
        f"➕ Added: {_n(added)}",
        f"♻️ Duplicates skipped: {_n(dupes)}",
        f"⚠️ Errors: {_n(row['errors'])}",
        f"📡 Source channels: {len(cfg.source_channel_ids)}",
        f"📍 Registered up to channel message #{row['last_message_id']} (highest of all channels)",
        f"🕒 Started: {started:%Y-%m-%d %H:%M UTC}" if started else "🕒 Started: -",
        f"🏁 Finished: {finished:%Y-%m-%d %H:%M UTC}" if finished else "🏁 Finished: -",
        f"🔁 Automatic catch-up: {auto}"
        + ("" if row["last_message_id"] else " (starts after the first /scan)"),
    ]
    if row["last_error"]:
        lines.append(f"❌ Last error: {row['last_error']}")
    await message.answer("\n".join(lines))
