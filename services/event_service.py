"""Human-readable system events for the admin Logs panel.

Only short, safe summaries are stored (never stack traces). Every message is scrubbed of the
configured secrets before it is written, so BOT_TOKEN, API_HASH, SESSION_STRING and the
database URL/password can never end up in the table or on screen.
"""
from __future__ import annotations

import logging
import re

from config import Config
from database import Database

log = logging.getLogger("events")

# kind -> (icon, filter group)
KINDS = {
    "user_request": ("📨", "requests"),
    "delivery": ("🎬", "deliveries"),
    "scan_started": ("🔎", "scans"),
    "scan_completed": ("✅", "scans"),
    "duplicates": ("♻️", "scans"),
    "video_added": ("➕", "videos"),
    "video_deleted": ("🗑️", "videos"),
    "auto_delete": ("⏱️", "cleanup"),
    "admin": ("🛠️", "admin"),
    "error": ("⚠️", "errors"),
}
GROUPS = {
    "all": ("📋 All", None),
    "requests": ("📨 Requests", ("user_request",)),
    "deliveries": ("🎬 Deliveries", ("delivery", "auto_delete")),
    "scans": ("🔎 Scans", ("scan_started", "scan_completed", "duplicates", "video_added")),
    "errors": ("⚠️ Errors", ("error",)),
}


class EventService:
    def __init__(self, db: Database, cfg: Config) -> None:
        self.db = db
        secrets = [cfg.bot_token, cfg.api_hash, cfg.session_string, cfg.database_url]
        # also scrub the pieces of the database URL (user / password / host)
        for part in re.split(r"[:@/?&=]+", cfg.database_url or ""):
            if len(part) >= 6 and part not in ("postgresql", "postgres", "mysql", "sslmode"):
                secrets.append(part)
        self._secrets = sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True)

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    async def record(self, kind: str, message: str, user_id: int | None = None) -> None:
        """Best effort: logging must never break a request."""
        try:
            await self.db.pool.execute(
                "INSERT INTO event_log (kind, message, user_id) VALUES ($1, $2, $3)",
                kind if kind in KINDS else "admin", self.scrub(message)[:300], user_id)
        except Exception:  # noqa: BLE001
            log.warning("event_record_failed")

    async def recent(self, group: str, limit: int, offset: int) -> tuple[list, int]:
        kinds = GROUPS.get(group, GROUPS["all"])[1]
        where, args = ("", []) if kinds is None else ("WHERE kind = ANY($1::text[])", [list(kinds)])
        n = len(args)
        rows = await self.db.pool.fetch(
            f"SELECT ts, kind, message, user_id FROM event_log {where} "
            f"ORDER BY id DESC LIMIT ${n + 1} OFFSET ${n + 2}", *args, limit, offset)
        total = await self.db.pool.fetchval(f"SELECT COUNT(*) FROM event_log {where}", *args)
        return rows, total

    async def prune(self, days: int = 14) -> None:
        await self.db.pool.execute(
            "DELETE FROM event_log WHERE ts < now() - make_interval(days => $1::int)", days)
