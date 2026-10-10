import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

import ui
from config import Config
from database import Database
from logging_setup import log_event
from models.video import Video
from services.user_service import UserService
from services.video_service import VideoService

log = logging.getLogger("delivery")

_INVALID_MARKERS = ("file identifier", "file_id", "file reference", "wrong remote file")
_GONE_MARKERS = ("message to copy not found", "message_id_invalid", "message can't be copied")


@dataclass
class DeliveryResult:
    delivered: int = 0
    failed: int = 0
    blocked: bool = False


class DeliveryService:
    def __init__(self, bot: Bot, db: Database, videos: VideoService,
                 users: UserService, cfg: Config, settings=None, events=None) -> None:
        self.settings = settings   # SettingsService (optional): caption options
        self.events = events       # EventService (optional): admin Logs panel
        self.bot = bot
        self.db = db
        self.videos = videos
        self.users = users
        self.cfg = cfg
        self._access_warned = False

    async def deliver(self, user_id: int, chat_id: int, category: str, count: int,
                      shuffle: bool = False) -> DeliveryResult:
        """Send up to `count` videos of one category that this user has never received.
        Every sent video is written to user_video_history and queued for auto-deletion."""
        result = DeliveryResult()
        tried_ids: set[int] = set()
        sent_unique: set[str] = set()

        if not shuffle:   # statistics only; must never get in the way of a delivery
            try:
                prevented = await self.videos.count_seen_ahead(user_id, category, count)
                await self.videos.bump_counter("delivery_duplicates_prevented", prevented)
            except Exception:
                log.exception("prevented_counter_failed")

        for _ in range(5):  # extra rounds replace videos that failed
            need = count - result.delivered
            if need <= 0:
                break
            batch = await self.videos.fetch_unseen(user_id, category, need,
                                                   list(tried_ids), shuffle)
            if not batch:
                break
            for video in batch:
                tried_ids.add(video.id)
                if (not self.videos.allow_duplicates
                        and video.telegram_file_unique_id in sent_unique):
                    continue
                outcome, message_id = await self._send_one(chat_id, video, result.delivered + 1)
                if outcome is None:  # user blocked the bot
                    result.blocked = True
                    await self.users.mark_blocked(user_id)
                    await self._record(user_id, category, count, result)
                    return result
                if outcome:
                    sent_unique.add(video.telegram_file_unique_id)
                    result.delivered += 1
                    await self._record_delivery(user_id, chat_id, video, message_id)
                    if result.delivered % max(1, self.cfg.delivery_batch_size) == 0:
                        await asyncio.sleep(1.0)   # short breather between batches (flood-friendly)
                else:
                    result.failed += 1

        await self._record(user_id, category, count, result)
        return result

    async def _record_delivery(self, user_id: int, chat_id: int, video: Video,
                               message_id: int | None) -> None:
        """History row (UNIQUE user+video) + auto-delete job, in one transaction."""
        for attempt in range(3):
            try:
                async with self.db.pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute(
                            "INSERT INTO user_video_history (user_id, video_id, category) "
                            "VALUES ($1, $2, $3) ON CONFLICT (user_id, video_id) DO NOTHING",
                            user_id, video.id, video.category,
                        )
                        if message_id is not None:
                            await conn.execute(
                                "INSERT INTO delivered_messages "
                                "(user_id, chat_id, video_id, telegram_message_id, delete_at) "
                                "VALUES ($1, $2, $3, $4, "
                                "now() + make_interval(secs => $5::double precision))",
                                user_id, chat_id, video.id, message_id,
                                float(self.cfg.auto_delete_seconds),
                            )
                return
            except Exception:
                log.exception("record_delivery_failed")
                await asyncio.sleep(0.5 * (attempt + 1))
        log_event(log, logging.ERROR, "record_delivery_gave_up", user_id=user_id,
                  video_id=video.id)

    async def _record(self, user_id: int, category: str, count: int, r: DeliveryResult) -> None:
        await self.db.pool.execute(
            "INSERT INTO requests (user_id, category, requested, delivered, failed) "
            "VALUES ($1, $2, $3, $4, $5)",
            user_id, category, count, r.delivered, r.failed,
        )
        log_event(log, logging.INFO, "delivery_finished", user_id=user_id, category=category,
                  requested=count, delivered=r.delivered, failed=r.failed, blocked=r.blocked)
        if self.events is not None:
            await self.events.record(
                "delivery", f"{r.delivered}/{count} {category} videos delivered"
                + (f", {r.failed} failed" if r.failed else "")
                + (", user blocked the bot" if r.blocked else ""), user_id)
            if r.failed:
                await self.events.record("error", f"{r.failed} video(s) could not be sent", user_id)

    async def _send_one(self, chat_id: int, video: Video,
                        position: int) -> tuple[bool | None, int | None]:
        """(True, message_id) = sent, (False, None) = failed, (None, None) = user blocked the bot.

        Delivery modes, tried in order: the stored Bot API file_id, then copying the original
        post out of the source channel (no "forwarded from", so the channel is never exposed).
        Videos found by the history scanner have no file_id and always use the copy."""
        # Clean caption of our own. The channel caption is only added if the admin opted in
        # (Bot Settings): channel captions can carry channel names or links.
        keep = self.settings is not None and self.settings.flag("keep_source_caption")
        caption = ui.video_caption(self.cfg, video, position, video.caption if keep else None)
        modes = []
        if video.telegram_file_id:
            modes.append("file")
        if video.channel_id is not None and video.telegram_message_id is not None:
            modes.append("copy")
        invalid_reasons = []
        for mode in modes:
            outcome, reason, message_id = await self._attempt(mode, chat_id, video, caption)
            if outcome == "ok":
                return True, message_id
            if outcome == "blocked":
                return None, None
            if outcome == "error":      # transient / access problem: keep the video active
                return False, None
            invalid_reasons.append(reason)
        if len(invalid_reasons) == len(modes):   # every way of sending it is dead
            await self.videos.mark_invalid(video.id, "; ".join(invalid_reasons) or "no way to send")
            log_event(log, logging.ERROR, "video_deactivated", video_id=video.id)
            if self.events is not None:
                await self.events.record("error", f"Video ID {video.id} can no longer be sent "
                                                  "and was deactivated")
            await self._notify_admins(
                f"⚠️ Video ID {video.id} (#{video.video_number}, {video.category}) "
                "can no longer be sent and was deactivated. Re-scan the channel with /scan "
                "or send the video to the bot again to restore it."
            )
        return False, None

    async def _attempt(self, mode: str, chat_id: int, video: Video,
                       caption: str) -> tuple[str, str, int | None]:
        """One delivery mode. Returns ("ok"|"blocked"|"error"|"invalid", reason, message_id)."""
        for attempt in range(5):
            try:
                if mode == "file":
                    # Original dimensions/duration are passed through untouched so Telegram's own
                    # viewer shows portrait / landscape / square exactly as posted; no
                    # thumbnail, re-encode, crop or padding is ever added.
                    sent = await self.bot.send_video(
                        chat_id, video.telegram_file_id, caption=caption, parse_mode="HTML",
                        width=video.width, height=video.height, duration=video.duration,
                        supports_streaming=True, protect_content=self.cfg.protect_content,
                    )
                else:
                    # copy_message keeps the original media (size, ratio, streaming) as is.
                    sent = await self.bot.copy_message(
                        chat_id, video.channel_id, video.telegram_message_id,
                        caption=caption, parse_mode="HTML",
                        protect_content=self.cfg.protect_content,
                    )
                await asyncio.sleep(self.cfg.send_delay)
                return "ok", "", sent.message_id
            except TelegramRetryAfter as e:
                log_event(log, logging.WARNING, "flood_wait", seconds=e.retry_after)
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError as e:
                if "bot was blocked" in str(e).lower() or "user is deactivated" in str(e).lower():
                    return "blocked", "", None
                if mode == "copy":  # the BOT has no access to the channel: not the user's fault
                    await self._warn_channel_access(str(e))
                    return "error", str(e), None
                return "blocked", "", None
            except TelegramBadRequest as e:
                msg = str(e).lower()
                if mode == "file" and any(m in msg for m in _INVALID_MARKERS):
                    log_event(log, logging.WARNING, "invalid_file_id", video_id=video.id)
                    return "invalid", str(e)[:100], None
                if mode == "copy" and any(m in msg for m in _GONE_MARKERS):
                    log_event(log, logging.WARNING, "source_message_gone", video_id=video.id)
                    return "invalid", str(e)[:100], None
                if mode == "copy" and "chat not found" in msg:
                    await self._warn_channel_access(str(e))
                    return "error", str(e), None
                log_event(log, logging.ERROR, "telegram_bad_request", video_id=video.id, error=str(e))
                return "error", str(e), None
            except TelegramAPIError as e:  # network / server hiccups
                log_event(log, logging.WARNING, "telegram_error", video_id=video.id,
                          attempt=attempt + 1, error=str(e))
                await asyncio.sleep(2 ** attempt)
            except Exception:
                log.exception("unexpected_send_error")
                return "error", "unexpected", None
        return "error", "retries exhausted", None

    async def _warn_channel_access(self, detail: str) -> None:
        if self._access_warned:
            return
        self._access_warned = True
        log_event(log, logging.ERROR, "channel_access_denied", error=detail[:200])
        await self._notify_admins(
            "⚠️ The bot cannot read the source channel, so scanned videos cannot be delivered. "
            "Add the bot to the channel as an administrator."
        )

    async def _notify_admins(self, text: str) -> None:
        for admin_id in self.cfg.admin_ids:
            try:
                await self.bot.send_message(admin_id, text)
            except TelegramAPIError:
                pass
