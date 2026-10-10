"""Bot API file_id bridge.

The Telethon scanner only learns MTProto metadata; a Telethon media id is NOT a Bot API
file_id and is never stored as one. To get a genuine Bot API file_id the *bot* forwards the
channel post to an admin's private chat (a supported Bot API workflow), reads the file_id
from the forwarded message, stores it and deletes the forwarded copy. Nothing is downloaded
or re-uploaded.

If the bot cannot do this (not a channel admin, protected content, post deleted) no id is
invented: the video stays without file_id and delivery keeps using copy_message, and the
limitation is reported to the admin.
"""
import asyncio
import logging
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest, TelegramForbiddenError,
                                TelegramRetryAfter)

from config import Config
from logging_setup import log_event
from services.video_service import VideoService

log = logging.getLogger("bridge")


@dataclass
class BridgeReport:
    resolved: int = 0
    duplicates: int = 0
    failed: int = 0
    remaining: int = 0
    aborted: str | None = None
    reasons: dict[str, int] = field(default_factory=dict)


class BridgeService:
    def __init__(self, bot: Bot, videos: VideoService, cfg: Config) -> None:
        self.bot = bot
        self.videos = videos
        self.cfg = cfg
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def run(self, admin_chat: int, batch: int = 100) -> BridgeReport | None:
        """Resolve Bot API file_ids for scanned videos. Returns None if already running."""
        if self._lock.locked():
            return None
        async with self._lock:
            rep = BridgeReport()
            after = 0
            while True:
                items = await self.videos.pending_bridge(batch, after)
                if not items:
                    break
                for v in items:
                    after = v.id
                    outcome = await self._one(admin_chat, v, rep)
                    if outcome == "abort":
                        break
                    await asyncio.sleep(1.0)        # stay far below Telegram's forward limits
                if rep.aborted:
                    break
            rep.remaining = await self.videos.count_without_file_id()
            log_event(log, logging.INFO, "bridge_finished", resolved=rep.resolved,
                      duplicates=rep.duplicates, failed=rep.failed, remaining=rep.remaining)
            return rep

    def _fail(self, rep: BridgeReport, reason: str) -> None:
        rep.failed += 1
        rep.reasons[reason] = rep.reasons.get(reason, 0) + 1

    async def _one(self, admin_chat: int, v, rep: BridgeReport) -> str:
        for _ in range(5):
            try:
                fwd = await self.bot.forward_message(
                    admin_chat, v.channel_id, v.telegram_message_id, disable_notification=True)
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                rep.aborted = "the bot has no access to the source channel (add it as admin)"
                return "abort"
            except TelegramBadRequest as e:
                m = str(e).lower()
                if "chat not found" in m:
                    rep.aborted = "the bot cannot see the source channel (add it as admin)"
                    return "abort"
                if "protected" in m:
                    self._fail(rep, "channel has protected content (forwarding disabled)")
                else:
                    self._fail(rep, "post not forwardable or deleted")
                return "fail"
            except TelegramAPIError:
                await asyncio.sleep(2)
        else:
            self._fail(rep, "retries exhausted")
            return "fail"

        media = fwd.video or (fwd.document if (fwd.document and (fwd.document.mime_type or "")
                                               .startswith("video/")) else None)
        try:
            if media is None:
                self._fail(rep, "post is not a video for the bot")
            else:
                res = await self.videos.set_bot_file_id(v.id, media.file_id, media.file_unique_id)
                if res == "ok":
                    rep.resolved += 1
                else:
                    rep.duplicates += 1
        finally:
            try:
                await self.bot.delete_message(admin_chat, fwd.message_id)
            except TelegramAPIError:
                pass
        return "ok"

    @staticmethod
    def summary(rep: BridgeReport) -> str:
        lines = ["🔗 Bot file_id registration finished.",
                 f"✅ Resolved: {rep.resolved}", f"♻️ Duplicates deactivated: {rep.duplicates}",
                 f"⚠️ Could not resolve: {rep.failed}", f"⏳ Still without file_id: {rep.remaining}"]
        if rep.aborted:
            lines.append(f"🛑 Stopped: {rep.aborted}.")
        for reason, n in rep.reasons.items():
            lines.append(f"• {n} × {reason}")
        if rep.remaining:
            lines.append("These videos are still delivered by copying the channel post "
                         "(the bot must be a channel admin).")
        return "\n".join(lines)
