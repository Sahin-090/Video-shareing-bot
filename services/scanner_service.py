"""Channel history scanner.

Bots cannot read a channel's history, so existing videos are discovered through an MTProto
client (Telethon) logged in as a normal account that is a member of the source channel.
Nothing is downloaded: for every video post only metadata is stored. The user-facing bot
then delivers the video by copying the post (copy_message), which never reveals the channel.
"""
import asyncio
import base64
import logging
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

from config import Config
from database import Database
from logging_setup import log_event
from models.video import ChannelVideo
from services.video_service import VideoService

log = logging.getLogger("scanner")

_UNIQUE_TYPE_DOCUMENT = 2
_MAX_RETRIES = 5


class ScannerNotConfigured(RuntimeError):
    """API_ID / API_HASH / SESSION_STRING missing, invalid, logged out, or a BOT session."""


class ChannelAccessError(RuntimeError):
    """The scanner (user) account cannot see the configured source channel."""


class ScanBusy(RuntimeError):
    pass


class MissingIdentifier(ValueError):
    pass


# ---------- pure helpers (no Telegram connection needed) ----------
def compute_file_unique_id(document_id: int) -> str:
    """Bot API `file_unique_id` of a document/video, derived from its MTProto document id.

    Telethon does not expose it, but the format is stable: type(int32)=2 + id(int64),
    zero-run-length encoded, then URL-safe base64 without padding. This is what makes a
    video found by the scanner and the same video seen by the bot collapse into one row."""
    raw = struct.pack("<iq", _UNIQUE_TYPE_DOCUMENT, document_id)
    out = bytearray()
    zeros = 0
    for b in raw:
        if b == 0:
            zeros += 1
            continue
        if zeros:
            out += bytes((0, zeros))
            zeros = 0
        out.append(b)
    if zeros:
        out += bytes((0, zeros))
    return base64.urlsafe_b64encode(bytes(out)).decode().rstrip("=")


def extract_video(msg, channel_id: int, category_for: Callable[[datetime | None], str]
                  ) -> ChannelVideo | None:
    """Metadata of a channel message if it is a video, else None (photos, text, GIFs,
    round video notes, service messages are skipped). Raises MissingIdentifier when a video
    has no usable id. Duck-typed so it works with any Telethon version."""
    doc = getattr(msg, "document", None)
    if doc is None:
        return None
    attrs = list(getattr(doc, "attributes", None) or [])
    names = {type(a).__name__ for a in attrs}
    mime = getattr(doc, "mime_type", None) or ""
    vattr = next((a for a in attrs if type(a).__name__ == "DocumentAttributeVideo"), None)
    if vattr is None and not mime.startswith("video/"):
        return None
    if "DocumentAttributeAnimated" in names or getattr(vattr, "round_message", False):
        return None
    doc_id = getattr(doc, "id", None)
    if not doc_id:
        raise MissingIdentifier(f"video in message {getattr(msg, 'id', '?')} has no document id")

    date = getattr(msg, "date", None)
    if date is not None and date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    duration = getattr(vattr, "duration", None)
    return ChannelVideo(
        channel_id=channel_id,
        message_id=msg.id,
        file_unique_id=compute_file_unique_id(doc_id),
        file_id=None,  # a Telethon file id is not valid for the bot; delivery copies the post
        message_date=date,
        caption=(getattr(msg, "message", None) or None),
        duration=int(round(duration)) if duration is not None else None,
        file_size=getattr(doc, "size", None),
        mime_type=mime or None,
        width=getattr(vattr, "w", None) or None,
        height=getattr(vattr, "h", None) or None,
        category=category_for(date),
    )


@dataclass
class ScanReport:
    mode: str = "full"
    status: str = "running"   # running | completed | stopped | failed
    scanned: int = 0
    found: int = 0
    added: int = 0
    duplicates: int = 0     # posts already registered
    repeated: int = 0       # stored posts that repeat a video seen elsewhere
    errors: int = 0
    last_id: int = 0
    error: str | None = None
    error_kind: str | None = None   # not_configured | channel_access | other
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


ProgressCb = Callable[[ScanReport], Awaitable[None]]


class ScannerService:
    def __init__(self, cfg: Config, db: Database, videos: VideoService, events=None) -> None:
        self.events = events   # EventService (optional): admin Logs panel
        self.cfg = cfg
        self.db = db
        self.videos = videos
        self._lock = asyncio.Lock()
        self._stop = False
        self._client = None
        self.current: ScanReport | None = None

    @property
    def enabled(self) -> bool:
        return self.cfg.mtproto_enabled

    @property
    def running(self) -> bool:
        return self._lock.locked()

    @property
    def connected(self) -> bool:
        """True while the Telethon user client holds a live connection (shown in the panel)."""
        return self._client is not None and self._client.is_connected()

    def request_stop(self) -> bool:
        if self.running:
            self._stop = True
            return True
        return False

    # ---------- connection ----------
    async def _connect(self):
        if not self.enabled:
            raise ScannerNotConfigured(
                "Set API_ID, API_HASH, SESSION_STRING and SOURCE_CHANNEL_ID to enable scanning."
            )
        if self._client is not None and self._client.is_connected():
            return self._client
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        client = TelegramClient(
            StringSession(self.cfg.session_string), self.cfg.api_id, self.cfg.api_hash,
            flood_sleep_threshold=0,   # we handle FloodWait ourselves so it can be reported
            receive_updates=False,
        )
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise ScannerNotConfigured("SESSION_STRING is not logged in. Generate a new one "
                                       "with generate_session.py.")
        me = await client.get_me()
        if getattr(me, "bot", False):
            # A session created by logging in with the bot token is "authorized" but bots may
            # not read history (BotMethodInvalidError). Only a real user account can.
            await client.disconnect()
            raise ScannerNotConfigured(
                "SESSION_STRING belongs to a BOT account. Generate it with generate_session.py "
                "by logging in with a normal user phone number, not the bot token."
            )
        self._client = client
        return client

    async def validate_session(self) -> bool:
        """Startup check. Logs only safe, fixed messages - never the session or any secret."""
        if not self.enabled:
            log.error("❌ Telethon user session is not configured correctly.")
            return False
        try:
            client = await self._connect()
        except ScannerNotConfigured:
            log.error("❌ Telethon user session is not configured correctly.")
            return False
        except Exception:  # noqa: BLE001 - e.g. network down; /scan retries the connection
            log.error("❌ Telethon user client could not connect at startup (will retry on /scan).")
            return False
        log.info("✅ Telethon user client connected")
        ok = True
        for i, channel in enumerate(self.cfg.source_channel_ids, 1):
            try:
                await self._entity(client, channel)
                log.info("✅ Source channel %d/%d configured", i, len(self.cfg.source_channel_ids))
            except ChannelAccessError:
                ok = False
                log.error("❌ Scanner account cannot access source channel %d/%d.", i,
                          len(self.cfg.source_channel_ids))
            except Exception:  # noqa: BLE001
                ok = False
                log.error("❌ Source channel %d/%d could not be checked at startup.", i,
                          len(self.cfg.source_channel_ids))
        return ok

    async def _entity(self, client, channel: int):
        from telethon.errors import ChannelInvalidError, ChannelPrivateError, RPCError

        denied = "Scanner account cannot access the configured source channel."
        try:
            return await client.get_entity(channel)
        except (ValueError, ChannelInvalidError, ChannelPrivateError):
            # A fresh session has no cached access hash: loading the dialogs fills the cache.
            try:
                async for _ in client.iter_dialogs():
                    pass
                return await client.get_entity(channel)
            except (ValueError, RPCError):
                raise ChannelAccessError(denied)
        except RPCError:
            raise ChannelAccessError(denied)

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    # ---------- state (survives restarts) ----------
    async def reset_stale(self) -> None:
        await self.db.pool.execute(
            "UPDATE scan_state SET status = 'interrupted', updated_at = now() "
            "WHERE status = 'running'"
        )

    async def checkpoint(self, channel_id: int | None = None) -> int:
        """Per-channel checkpoint; without a channel: the highest one (0 = never scanned)."""
        if channel_id is None:
            return await self.db.pool.fetchval(
                "SELECT COALESCE(max(last_message_id), 0) FROM scan_checkpoints") or 0
        return await self.db.pool.fetchval(
            "SELECT last_message_id FROM scan_checkpoints WHERE channel_id = $1", channel_id) or 0

    async def _save_checkpoint(self, channel_id: int, last_id: int) -> None:
        await self.db.pool.execute(
            "INSERT INTO scan_checkpoints (channel_id, last_message_id) VALUES ($1, $2) "
            "ON CONFLICT (channel_id) DO UPDATE SET "
            "last_message_id = GREATEST(scan_checkpoints.last_message_id, EXCLUDED.last_message_id), "
            "updated_at = now()", channel_id, last_id)

    async def status_row(self):
        return await self.db.pool.fetchrow("SELECT * FROM scan_state WHERE id = 1")

    async def _save(self, r: ScanReport, *, start: bool = False, finish: bool = False) -> None:
        await self.db.pool.execute(
            """
            UPDATE scan_state SET
                status = $1, mode = $2,
                started_at  = CASE WHEN $3 THEN now() ELSE started_at END,
                finished_at = CASE WHEN $4 THEN now() WHEN $3 THEN NULL ELSE finished_at END,
                messages_scanned = $5, videos_found = $6, videos_added = $7,
                duplicates = $8, errors = $9,
                last_message_id = GREATEST(last_message_id, $10),
                last_error = $11, updated_at = now()
            WHERE id = 1
            """,
            r.status, r.mode, start, finish, r.scanned, r.found, r.added,
            r.duplicates, r.errors, r.last_id, r.error,
        )

    # ---------- scanning ----------
    async def run_scan(self, *, full: bool, progress: ProgressCb | None = None) -> ScanReport:
        """Scan the source channel oldest -> newest and register every video.

        full=True re-reads the whole history (reconciliation); full=False continues after the
        last checkpoint, so it picks up new posts and finishes an interrupted scan. Both are
        safe to repeat: the database unique indexes make re-registering a no-op."""
        if self._lock.locked():
            raise ScanBusy("A scan is already running.")
        async with self._lock:
            self._stop = False
            report = ScanReport(mode="full" if full else "incremental")
            self.current = report
            try:
                client = await self._connect()
                channels = list(self.cfg.source_channel_ids)
                await self._save(report, start=True)
                await self._event("scan_started",
                                  f"{report.mode} scan started ({len(channels)} channel"
                                  f"{'' if len(channels) == 1 else 's'})")
                failed: list[tuple[str, str]] = []   # (error_kind, message) per broken channel
                for channel in channels:
                    if self._stop:
                        break
                    try:
                        entity = await self._entity(client, channel)
                        cursor = 0 if full else await self.checkpoint(channel)
                        await self._scan_loop(client, entity, channel, cursor, report, progress)
                    except Exception as e:  # noqa: BLE001 - one broken channel must not stop the rest
                        kind, msg = self._classify(e)
                        failed.append((kind, msg))
                        report.errors += 1
                        log_event(log, logging.ERROR, "channel_scan_failed", kind=kind,
                                  error=type(e).__name__, channel_index=channels.index(channel) + 1)
                        if kind == "not_configured":
                            break          # the session itself is broken: no channel can work
                if failed and len(failed) == len(channels):
                    report.status = "failed"
                    report.error_kind, report.error = failed[0]
                else:
                    report.status = "stopped" if self._stop else "completed"
                    if failed:
                        report.error = (f"{len(failed)} of {len(channels)} channels could not be "
                                        "scanned (no access?)")
            except Exception as e:  # noqa: BLE001 - a scan must never take the app down
                report.status = "failed"
                report.error_kind, report.error = self._classify(e)
                log_event(log, logging.ERROR, "scan_failed", kind=report.error_kind,
                          error=type(e).__name__)
            try:
                await self._save(report, finish=True)
                if report.status in ("completed", "stopped"):
                    await self.videos.refresh_categories()
            except Exception:  # noqa: BLE001
                log.exception("scan_state_save_failed")
            await self._finish_events(report)
            log_event(log, logging.INFO, "scan_finished", mode=report.mode, status=report.status,
                      scanned=report.scanned, found=report.found, added=report.added,
                      duplicates=report.duplicates, errors=report.errors)
            return report

    async def _event(self, kind: str, message: str) -> None:
        if self.events is not None:
            await self.events.record(kind, message)

    async def _finish_events(self, r: ScanReport) -> None:
        try:
            await self.videos.bump_counter("scans_total", 1)
            if r.status == "failed":
                await self._event("error", f"{r.mode} scan failed: {r.error_kind or 'error'}")
                return
            await self._event(
                "scan_completed",
                f"{r.mode} scan {r.status}: {r.scanned} messages, {r.found} videos found, "
                f"{r.added} added, {r.errors} errors")
            if r.duplicates:
                await self._event("duplicates", f"{r.duplicates} post(s) were already registered")
            if r.added:
                await self._event("video_added", f"{r.added} video(s) added by the scan")
        except Exception:  # noqa: BLE001
            log.warning("scan_events_failed")

    @staticmethod
    def _classify(e: Exception) -> tuple[str, str]:
        from telethon.errors import (BotMethodInvalidError, ChannelInvalidError,
                                     ChannelPrivateError)
        if isinstance(e, (ScannerNotConfigured, BotMethodInvalidError)):
            return "not_configured", str(e)[:300] if isinstance(e, ScannerNotConfigured) else \
                "The session is a bot account; a user session is required."
        if isinstance(e, (ChannelAccessError, ChannelPrivateError, ChannelInvalidError)):
            return "channel_access", "Scanner account cannot access the configured source channel."
        return "other", f"{type(e).__name__}: {e}"[:300]

    async def _scan_loop(self, client, entity, channel_id: int, cursor: int, r: ScanReport,
                         progress: ProgressCb | None) -> None:
        from telethon.errors import FloodWaitError

        batch: list[ChannelVideo] = []
        r.last_id = cursor            # durable checkpoint of THIS channel: only advanced by flush()
        seen = {"id": cursor}         # last message consumed (may be ahead of the checkpoint)
        category_for = self.videos.category_for
        retries = 0

        async def flush() -> None:
            items = batch[:]
            batch.clear()
            if not items:
                r.last_id = max(r.last_id, seen["id"])
                await self._save_checkpoint(channel_id, r.last_id)
                return
            try:
                res = await self.videos.upsert_channel_videos(items)
                r.added += res.added
                r.duplicates += res.duplicates
                r.repeated += res.repeated
            except Exception:  # noqa: BLE001 - isolate a bad row instead of losing the batch
                log.exception("batch_upsert_failed")
                for item in items:
                    try:
                        res = await self.videos.upsert_channel_videos([item])
                        r.added += res.added
                        r.duplicates += res.duplicates
                        r.repeated += res.repeated
                    except Exception:  # noqa: BLE001
                        r.errors += 1
                        log_event(log, logging.ERROR, "video_upsert_failed", message_id=item.message_id)
            r.last_id = max(r.last_id, seen["id"])   # rows are stored: safe to advance
            await self._save_checkpoint(channel_id, r.last_id)
            await self._save(r)

        async def report_progress() -> None:
            await flush()
            await self._save(r)
            if progress:
                try:
                    await progress(r)
                except Exception:  # noqa: BLE001
                    log.exception("progress_callback_failed")

        while True:
            try:
                async for msg in client.iter_messages(entity, limit=None, reverse=True, min_id=seen["id"]):
                    if self._stop:
                        break
                    r.scanned += 1
                    seen["id"] = max(seen["id"], msg.id)
                    try:
                        video = extract_video(msg, channel_id, category_for)
                    except Exception as e:  # noqa: BLE001 - one bad message never stops the scan
                        r.errors += 1
                        log_event(log, logging.WARNING, "message_skipped",
                                  message_id=getattr(msg, "id", None), reason=str(e)[:200])
                        video = None
                    if video is not None:
                        r.found += 1
                        batch.append(video)
                    if len(batch) >= self.cfg.scan_batch_size:
                        await flush()
                    if r.scanned % self.cfg.scan_progress_every == 0:
                        await report_progress()
                break
            except FloodWaitError as e:
                wait = int(getattr(e, "seconds", 30)) + 1
                log_event(log, logging.WARNING, "flood_wait", seconds=wait)
                await asyncio.sleep(wait)       # then resume right after the last message seen
            except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                retries += 1
                if retries > _MAX_RETRIES:
                    raise
                log_event(log, logging.WARNING, "scan_network_retry", attempt=retries, error=str(e)[:100])
                await asyncio.sleep(min(2 ** retries, 30))
        await flush()
        await self._save(r)
        if progress:
            try:
                await progress(r)
            except Exception:  # noqa: BLE001
                pass

    # ---------- background catch-up ----------
    async def background_loop(self) -> None:
        """Keeps the library current: re-ages New/Old and, once a first scan has happened,
        registers any post the live listener missed (e.g. while the bot was down)."""
        interval = (self.cfg.auto_scan_minutes or 60) * 60
        await asyncio.sleep(20)
        while True:
            try:
                await self.videos.refresh_categories()
                if (self.enabled and self.cfg.auto_scan_minutes > 0
                        and not self.running and await self.checkpoint() > 0):
                    await self.run_scan(full=False)
            except ScanBusy:
                pass
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("background_loop_error")
            await asyncio.sleep(interval)
