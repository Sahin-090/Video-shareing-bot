"""Restart-safe auto-delete of the video messages the bot sent to users.

Every delivered video is queued in `delivered_messages` with a `delete_at` time. This worker
polls the table and deletes the due Telegram messages from the USER'S chat. Because the queue
lives in the database, a Railway restart only delays deletions - overdue rows are handled on
the very next tick after startup.

It never touches the source channel, the `videos` table or `user_video_history`.
"""
import asyncio
import logging
import time
from collections import defaultdict

from aiogram import Bot
from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest, TelegramForbiddenError,
                                TelegramRetryAfter)

from database import Database
from logging_setup import log_event

log = logging.getLogger("cleanup")

TICK_SECONDS = 2.0       # deletion happens at most ~2 s after delete_at
BATCH = 200              # rows handled per tick
MAX_ATTEMPTS = 10        # network failures before a job is abandoned
PRUNE_AFTER = "3 days"   # finished jobs are forgotten after this long


class CleanupService:
    def __init__(self, bot: Bot, db: Database, events=None) -> None:
        self.bot = bot
        self.db = db
        self.events = events          # EventService (optional): admin Logs panel
        self._pending_deleted = 0     # deletions not yet summarised in the log
        self._last_summary = 0.0

    async def run(self) -> None:
        log_event(log, logging.INFO, "cleanup_worker_started")
        ticks_since_prune = 0
        while True:
            busy = False
            try:
                busy = (await self.tick()) >= BATCH
                ticks_since_prune += 1
                if ticks_since_prune >= 1800:   # roughly hourly
                    ticks_since_prune = 0
                    await self.db.pool.execute(
                        "DELETE FROM delivered_messages "
                        f"WHERE deleted_at < now() - interval '{PRUNE_AFTER}'")
                    if self.events is not None:
                        await self.events.prune()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("cleanup_tick_failed")
            await asyncio.sleep(0.2 if busy else TICK_SECONDS)

    async def tick(self) -> int:
        """Delete everything that is due. Returns how many jobs were picked up."""
        rows = await self.db.pool.fetch(
            "SELECT id, chat_id, telegram_message_id FROM delivered_messages "
            "WHERE deleted_at IS NULL AND delete_at <= now() "
            "ORDER BY delete_at LIMIT $1", BATCH)
        if not rows:
            return 0

        by_chat: dict[int, list[tuple[int, int]]] = defaultdict(list)
        for r in rows:
            by_chat[r["chat_id"]].append((r["id"], r["telegram_message_id"]))

        finished: list[int] = []   # deleted, already gone, or impossible to delete
        retry: list[int] = []      # temporary problem: try again next tick
        for chat_id, items in by_chat.items():
            for i in range(0, len(items), 100):
                chunk = items[i:i + 100]
                done, again = await self._delete_chunk(chat_id, chunk)
                finished += done
                retry += again

        if finished:
            await self.db.pool.execute(
                "UPDATE delivered_messages SET deleted_at = now() WHERE id = ANY($1::bigint[])",
                finished)
            await self._count(len(finished))
        if retry:
            await self.db.pool.execute(
                "UPDATE delivered_messages SET attempts = attempts + 1, "
                "deleted_at = CASE WHEN attempts + 1 >= $2 THEN now() ELSE NULL END "
                "WHERE id = ANY($1::bigint[])", retry, MAX_ATTEMPTS)
        return len(rows)

    async def _count(self, n: int) -> None:
        """Lifetime counter for the statistics page (rows are pruned after a few days) and a
        throttled 'auto-delete completed' log line (at most one per minute)."""
        try:
            await self.db.pool.execute(
                "INSERT INTO meta (key, value) VALUES ('auto_deleted_total', $1::text) "
                "ON CONFLICT (key) DO UPDATE "
                "SET value = (meta.value::bigint + EXCLUDED.value::bigint)::text", str(n))
            self._pending_deleted += n
            now = time.monotonic()
            if self.events is not None and now - self._last_summary >= 60:
                await self.events.record("auto_delete",
                                         f"{self._pending_deleted} delivered video(s) auto-deleted")
                self._pending_deleted, self._last_summary = 0, now
        except Exception:  # noqa: BLE001 - statistics must never stop the cleanup
            log.warning("cleanup_counter_failed")

    async def _delete_chunk(self, chat_id: int,
                            chunk: list[tuple[int, int]]) -> tuple[list[int], list[int]]:
        ids = [c[0] for c in chunk]
        msg_ids = [c[1] for c in chunk]
        try:
            await self.bot.delete_messages(chat_id, msg_ids)   # missing messages are skipped
            return ids, []
        except TelegramRetryAfter as e:
            log_event(log, logging.WARNING, "cleanup_flood_wait", seconds=e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
            return [], ids
        except TelegramForbiddenError:
            return ids, []          # user blocked the bot / chat gone: nothing more to do
        except TelegramBadRequest:
            pass                    # e.g. a message too old to delete: handle one by one
        except TelegramAPIError as e:
            log_event(log, logging.WARNING, "cleanup_api_error", error=str(e)[:150])
            return [], ids

        done: list[int] = []
        again: list[int] = []
        for row_id, mid in chunk:
            try:
                await self.bot.delete_message(chat_id, mid)
                done.append(row_id)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                again.append(row_id)
            except (TelegramBadRequest, TelegramForbiddenError):
                done.append(row_id)  # already deleted by the user, or not deletable any more
            except TelegramAPIError:
                again.append(row_id)
        return done, again
