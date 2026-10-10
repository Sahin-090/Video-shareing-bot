"""Force-subscribe channel list (PostgreSQL table force_sub_channels).

Only the LIST of channels is kept in memory (so the per-message gate needs no database round
trip). Membership of users is never stored or cached anywhere - see force_subscribe.py.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from database import Database

log = logging.getLogger("forcesub.service")
MAX_CHANNELS = 10
_EDITABLE = {"title", "url", "enabled", "chat_id", "username"}


class DuplicateChannel(Exception):
    pass


class TooManyChannels(Exception):
    pass


@dataclass(frozen=True)
class Channel:
    id: int
    chat_id: int
    title: str
    username: str | None
    url: str
    enabled: bool


def _row(r) -> Channel:
    return Channel(r["id"], r["chat_id"], r["title"], r["username"], r["url"], r["enabled"])


class ForceSubService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._channels: list[Channel] = []

    async def load(self) -> None:
        rows = await self.db.pool.fetch(
            "SELECT id, chat_id, title, username, url, enabled FROM force_sub_channels ORDER BY id")
        self._channels = [_row(r) for r in rows]

    # -- reads (memory only) --
    def all(self) -> list[Channel]:
        return list(self._channels)

    def enabled(self) -> list[Channel]:
        return [c for c in self._channels if c.enabled]

    def get(self, channel_id: int) -> Channel | None:
        return next((c for c in self._channels if c.id == channel_id), None)

    def by_chat(self, chat_id: int) -> Channel | None:
        return next((c for c in self._channels if c.chat_id == chat_id), None)

    # -- writes (database first, then refresh the in-memory list) --
    async def add(self, chat_id: int, title: str, username: str | None, url: str) -> Channel:
        if len(self._channels) >= MAX_CHANNELS:
            raise TooManyChannels
        try:
            row = await self.db.pool.fetchrow(
                "INSERT INTO force_sub_channels (chat_id, title, username, url) "
                "VALUES ($1, $2, $3, $4) RETURNING id, chat_id, title, username, url, enabled",
                chat_id, title, username, url)
        except asyncpg.UniqueViolationError as exc:
            raise DuplicateChannel from exc
        await self.load()
        return _row(row)

    async def update(self, channel_id: int, **changes) -> bool:
        changes = {k: v for k, v in changes.items() if k in _EDITABLE}
        if not changes:
            return False
        sets = ", ".join(f"{k} = ${i}" for i, k in enumerate(changes, start=2))
        res = await self.db.pool.execute(
            f"UPDATE force_sub_channels SET {sets} WHERE id = $1", channel_id, *changes.values())
        await self.load()
        return res.endswith("1")

    async def remove(self, channel_id: int) -> bool:
        res = await self.db.pool.execute("DELETE FROM force_sub_channels WHERE id = $1", channel_id)
        await self.load()
        return res.endswith("1")

    async def seed_default(self, bot, channel_ref, url: str, name: str) -> None:
        """First start only: put the originally configured channel (default @TheRoyalZone) in the
        list so behaviour is unchanged after the upgrade. Never re-adds a channel the admin
        removed (a flag row in bot_settings remembers that seeding happened)."""
        flag = await self.db.pool.fetchval("SELECT 1 FROM bot_settings WHERE key = 'fsub_seeded'")
        if flag:
            return
        if self._channels:
            await self._mark_seeded()
            return
        try:
            chat = await bot.get_chat(channel_ref)
        except Exception as exc:  # noqa: BLE001
            log.error("force-sub seed: cannot resolve %s (%s) - add it from the Admin Panel",
                      channel_ref, type(exc).__name__)
            return
        username = getattr(chat, "username", None)
        await self.add(chat.id, getattr(chat, "title", None) or name, username,
                       f"https://t.me/{username}" if username else url)
        await self._mark_seeded()
        log.info("force-sub: seeded default channel %s", channel_ref)

    async def _mark_seeded(self) -> None:
        await self.db.pool.execute(
            "INSERT INTO bot_settings (key, value) VALUES ('fsub_seeded', '1') "
            "ON CONFLICT (key) DO NOTHING")
