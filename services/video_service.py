from datetime import datetime, timedelta, timezone

import asyncpg

from database import Database
from models.video import ChannelVideo, UpsertResult, Video

_NEW_ORDER = "COALESCE(message_date, created_at) DESC, id DESC"
_NEW_ORDERS = {
    "desc": _NEW_ORDER,
    "asc": "COALESCE(message_date, created_at) ASC, id ASC",
    "random": "random()",
}
# Old: from the channel's oldest post up to the latest (asc), by post date - not by video_number,
# because copies registered later (re-posted videos) get a high number although they are old.
_OLD_ORDER = {
    "asc": "COALESCE(message_date, created_at) ASC, id ASC",
    "desc": "COALESCE(message_date, created_at) DESC, id DESC",
    "random": "random()",
}
_PICK_LATEST = "COALESCE(message_date, created_at) DESC, id DESC"
_PICK_OLDEST = "COALESCE(message_date, created_at) ASC, id ASC"
_SCOPES = ("new", "old", "pending", "all")

# Column list shared by the insert and the two "refresh a duplicate" statements.
_UNNEST = """
    unnest($1::bigint[], $2::bigint[], $3::text[], $4::text[], $5::text[], $6::text[],
           $7::int[], $8::bigint[], $9::text[], $10::int[], $11::int[], $12::timestamptz[])
    AS x(ch, msg, uid, fid, cat, cap, dur, size, mime, w, h, dt)
"""

_INSERT_NEW = f"""
    INSERT INTO videos (channel_id, telegram_message_id, telegram_file_unique_id, telegram_file_id,
                        category, caption, duration, file_size, mime_type, width, height, message_date)
    SELECT ch, msg, uid, fid, cat, cap, dur, size, mime, w, h, dt FROM {_UNNEST}
    ON CONFLICT (channel_id, telegram_message_id) DO NOTHING   -- the same POST is one row
    RETURNING id, channel_id, telegram_message_id
"""

# Number the freshly inserted rows oldest -> newest, continuing the global sequence (no gaps).
_NUMBER_NEW = """
    UPDATE videos v SET video_number = $2::int + r.rn
    FROM (SELECT id, row_number() OVER (
                    ORDER BY message_date NULLS LAST, telegram_message_id, id) AS rn
          FROM videos WHERE id = ANY($1::bigint[])) r
    WHERE v.id = r.id
"""

_SET_REFRESH = """
    telegram_file_id = COALESCE(x.fid, v.telegram_file_id),
    caption          = COALESCE(v.caption, x.cap),
    duration         = COALESCE(v.duration, x.dur),
    file_size        = COALESCE(v.file_size, x.size),
    mime_type        = COALESCE(v.mime_type, x.mime),
    width            = COALESCE(v.width, x.w),
    height           = COALESCE(v.height, x.h),
    message_date     = COALESCE(v.message_date, x.dt),
    -- a fresh Bot API file_id revives a video that was deactivated for an invalid one
    is_active        = v.is_active OR x.fid IS NOT NULL,
    last_error       = CASE WHEN x.fid IS NOT NULL THEN NULL ELSE v.last_error END,
    updated_at       = now()
"""

# Duplicate of the same channel post: only write when there is something new to learn,
# so re-running a scan over thousands of known videos performs no writes at all.
_REFRESH_BY_SOURCE = f"""
    UPDATE videos v SET {_SET_REFRESH}
    FROM {_UNNEST}
    WHERE v.channel_id = x.ch AND v.telegram_message_id = x.msg
      AND ((x.fid IS NOT NULL AND v.telegram_file_id IS DISTINCT FROM x.fid)
           OR v.message_date IS NULL
           OR (v.duration IS NULL AND x.dur IS NOT NULL)
           OR (v.caption IS NULL AND x.cap IS NOT NULL))
"""

# Same video was added by hand earlier (no source post known): attach the post to that row.
_ATTACH_SOURCE = f"""
    UPDATE videos v SET channel_id = x.ch, telegram_message_id = x.msg, {_SET_REFRESH}
    FROM {_UNNEST}
    WHERE v.telegram_file_unique_id = x.uid AND v.telegram_message_id IS NULL
      AND NOT EXISTS (SELECT 1 FROM videos o
                      WHERE o.channel_id = x.ch AND o.telegram_message_id = x.msg)
"""


class VideoService:
    def __init__(self, db: Database, old_order: str, new_video_days: int = 30) -> None:
        self.db = db
        self.old_order = old_order
        self.new_order = "desc"          # admin-configurable (Bot Settings)
        self.allow_duplicates = False    # admin-configurable: also deliver re-posted copies
        self.new_video_days = new_video_days

    def order_sql(self, category: str) -> str:
        return _NEW_ORDERS.get(self.new_order, _NEW_ORDER) if category == "new" \
            else _OLD_ORDER[self.old_order]

    # ---------- channel videos (scanner + live detection) ----------
    async def upsert_channel_videos(self, items: list[ChannelVideo]) -> UpsertResult:
        """Register videos from the source channels. Idempotent per POST: the unique index on
        (channel_id, message_id) is the final guard, so scanning the same post any number of
        times leaves exactly one row. The SAME video posted again (other message or other
        channel) is a different post and gets its own row - nothing is dropped any more."""
        if not items:
            return UpsertResult()
        cols = self._columns(items)
        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(7001)")  # numbering lock
                # A hand-added copy of this video (no source post yet) adopts the channel post
                # instead of creating a second row.
                await conn.execute(_ATTACH_SOURCE, *cols)
                inserted = await conn.fetch(_INSERT_NEW, *cols)
                repeated = 0
                if inserted:
                    ids = [r["id"] for r in inserted]
                    base = await conn.fetchval(
                        "SELECT COALESCE(MAX(video_number), 0) FROM videos"
                    )
                    await conn.execute(_NUMBER_NEW, ids, base)
                    # extra copies of a video that already exists elsewhere (informational)
                    repeated = await conn.fetchval(
                        "SELECT COUNT(*) FROM videos v WHERE v.id = ANY($1::bigint[]) AND EXISTS "
                        "(SELECT 1 FROM videos o WHERE o.telegram_file_unique_id = "
                        "v.telegram_file_unique_id AND o.id < v.id)", ids)
                new_keys = {(r["channel_id"], r["telegram_message_id"]) for r in inserted}
                known = [i for i in items if (i.channel_id, i.message_id) not in new_keys]
                if known:
                    await conn.execute(_REFRESH_BY_SOURCE, *self._columns(known))
                    await self._bump(conn, "duplicates_prevented", len(known))
        return UpsertResult(added=len(inserted), duplicates=len(items) - len(inserted),
                            repeated=repeated)

    @staticmethod
    def _columns(items: list[ChannelVideo]) -> list[list]:
        return [
            [i.channel_id for i in items], [i.message_id for i in items],
            [i.file_unique_id for i in items], [i.file_id for i in items],
            [i.category for i in items], [i.caption for i in items],
            [i.duration for i in items], [i.file_size for i in items],
            [i.mime_type for i in items], [i.width for i in items],
            [i.height for i in items], [i.message_date for i in items],
        ]

    @staticmethod
    async def _bump(conn, key: str, amount: int) -> None:
        await conn.execute(
            """
            INSERT INTO meta (key, value) VALUES ($1, $2::text)
            ON CONFLICT (key) DO UPDATE
            SET value = (meta.value::bigint + EXCLUDED.value::bigint)::text
            """,
            key, str(amount),     # asyncpg is strict: the parameter is declared ::text
        )

    async def refresh_categories(self) -> int:
        """Channel videos are 'new' while posted within NEW_VIDEO_DAYS, 'old' afterwards.
        Videos whose category an admin set by hand are left alone."""
        rows = await self.db.pool.fetch(
            """
            UPDATE videos SET category = t.cat, updated_at = now()
            FROM (SELECT id,
                         CASE WHEN message_date >= now() - make_interval(days => $1::int)
                              THEN 'new' ELSE 'old' END AS cat
                  FROM videos
                  WHERE channel_id IS NOT NULL AND message_date IS NOT NULL
                    AND category IS NOT NULL AND NOT category_locked) t
            WHERE videos.id = t.id AND videos.category IS DISTINCT FROM t.cat
            RETURNING videos.id
            """,
            self.new_video_days,
        )
        return len(rows)

    def category_for(self, message_date) -> str:
        if message_date is None:
            return "old"
        if message_date.tzinfo is None:
            message_date = message_date.replace(tzinfo=timezone.utc)
        recent = datetime.now(timezone.utc) - timedelta(days=self.new_video_days)
        return "new" if message_date >= recent else "old"

    # ---------- Bot API file_id bridge ----------
    async def pending_bridge(self, limit: int, after_id: int = 0) -> list[Video]:
        """Scanned videos that still have no Bot API file_id."""
        rows = await self.db.pool.fetch(
            "SELECT * FROM videos WHERE telegram_file_id IS NULL AND channel_id IS NOT NULL "
            "AND telegram_message_id IS NOT NULL AND is_active AND id > $2 ORDER BY id LIMIT $1",
            limit, after_id,
        )
        return [Video.from_record(r) for r in rows]

    async def count_without_file_id(self) -> int:
        return await self.db.pool.fetchval(
            "SELECT COUNT(*) FROM videos WHERE telegram_file_id IS NULL "
            "AND channel_id IS NOT NULL AND is_active"
        )

    async def set_bot_file_id(self, video_id: int, file_id: str, unique_id: str) -> str:
        """Store a REAL Bot API file_id (obtained from the bot itself). If the bot reports a
        different file_unique_id than the one computed by the scanner, adopt the bot's value;
        when that value already belongs to another row this row is a duplicate and is
        deactivated. Returns 'ok' | 'duplicate'."""
        try:
            await self.db.pool.execute(
                "UPDATE videos SET telegram_file_id = $2, telegram_file_unique_id = $3, "
                "last_error = NULL, updated_at = now() WHERE id = $1",
                video_id, file_id, unique_id,
            )
            return "ok"
        except asyncpg.UniqueViolationError:
            await self.db.pool.execute(
                "UPDATE videos SET is_active = FALSE, last_error = 'duplicate of another video', "
                "updated_at = now() WHERE id = $1", video_id,
            )
            return "duplicate"

    # ---------- admin: create / edit / delete ----------
    async def add_pending(self, file_id: str, unique_id: str, caption: str | None,
                          admin_id: int | None) -> Video | None:
        """Insert a hand-added video awaiting a category. Returns None if it's already in the
        library. If the same video exists but was marked invalid, its file_id is refreshed."""
        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(7002)")
                row = await conn.fetchrow(
                    "SELECT id, is_active FROM videos WHERE telegram_file_unique_id = $1 "
                    "ORDER BY is_active DESC, id LIMIT 1", unique_id)
                if row is None:
                    new = await conn.fetchrow(
                        "INSERT INTO videos (telegram_file_id, telegram_file_unique_id, caption, "
                        "added_by) VALUES ($1, $2, $3, $4) RETURNING *",
                        file_id, unique_id, caption, admin_id)
                    return Video.from_record(new)
                if row["is_active"]:
                    return None
                revived = await conn.fetchrow(
                    "UPDATE videos SET telegram_file_id = $2, is_active = TRUE, last_error = NULL, "
                    "updated_at = now() WHERE id = $1 RETURNING *", row["id"], file_id)
                return Video.from_record(revived)

    async def assign_category(self, video_id: int, category: str,
                              only_pending: bool = False, lock: bool = False) -> Video | None:
        """Set the category. A video keeps its number for life; it only gets one (the next
        in the global sequence) the first time it is categorised."""
        guard = "AND category IS NULL" if only_pending else ""
        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(7001)")
                row = await conn.fetchrow(
                    f"""
                    UPDATE videos SET category = $2,
                        category_locked = category_locked OR $3,
                        updated_at = now(),
                        video_number = COALESCE(video_number,
                            (SELECT COALESCE(MAX(video_number), 0) + 1 FROM videos))
                    WHERE id = $1 {guard}
                    RETURNING *
                    """,
                    video_id, category, lock,
                )
        return Video.from_record(row) if row else None

    async def set_number(self, video_id: int, number: int) -> Video | None:
        try:
            row = await self.db.pool.fetchrow(
                "UPDATE videos SET video_number = $2, updated_at = now() "
                "WHERE id = $1 RETURNING *",
                video_id, number,
            )
        except asyncpg.UniqueViolationError:
            raise ValueError("That video number is already used.")
        return Video.from_record(row) if row else None

    async def set_caption(self, video_id: int, caption: str) -> Video | None:
        row = await self.db.pool.fetchrow(
            "UPDATE videos SET caption = $2, updated_at = now() WHERE id = $1 RETURNING *",
            video_id, caption,
        )
        return Video.from_record(row) if row else None

    async def delete(self, video_id: int) -> bool:
        return await self.db.pool.fetchval(
            "DELETE FROM videos WHERE id = $1 RETURNING 1", video_id
        ) is not None

    async def mark_invalid(self, video_id: int, reason: str) -> None:
        await self.db.pool.execute(
            "UPDATE videos SET is_active = FALSE, last_error = $2, updated_at = now() "
            "WHERE id = $1",
            video_id, reason[:300],
        )

    # ---------- queries ----------
    async def get(self, video_id: int) -> Video | None:
        row = await self.db.pool.fetchrow("SELECT * FROM videos WHERE id = $1", video_id)
        return Video.from_record(row) if row else None

    @staticmethod
    def _where(scope: str) -> tuple[str, list]:
        if scope in ("new", "old"):
            return "WHERE category = $1", [scope]
        if scope == "pending":
            return "WHERE category IS NULL", []
        return "", []

    async def list_videos(self, scope: str, limit: int, offset: int) -> list[Video]:
        where, args = self._where(scope if scope in _SCOPES else "all")
        n = len(args)
        rows = await self.db.pool.fetch(
            f"SELECT * FROM videos {where} ORDER BY category NULLS FIRST, video_number, id "
            f"LIMIT ${n + 1} OFFSET ${n + 2}",
            *args, limit, offset,
        )
        return [Video.from_record(r) for r in rows]

    async def count(self, scope: str) -> int:
        where, args = self._where(scope if scope in _SCOPES else "all")
        return await self.db.pool.fetchval(f"SELECT COUNT(*) FROM videos {where}", *args)

    async def count_available(self) -> int:
        """Deliverable videos (either category). With duplicates off, copies count once."""
        if self.allow_duplicates:
            return await self.db.pool.fetchval(
                "SELECT COUNT(*) FROM videos WHERE category IS NOT NULL AND is_active")
        return await self.db.pool.fetchval(
            "SELECT COUNT(DISTINCT telegram_file_unique_id) FROM videos "
            "WHERE category IS NOT NULL AND is_active"
        )

    async def fetch_for_delivery(self, category: str, limit: int,
                                 exclude_ids: list[int]) -> list[Video]:
        """Unique videos for a request: the chosen category first, then — only if that runs
        short — the rest of the library in the matching order, so a request is never refused
        while videos exist."""
        other = "old" if category == "new" else "new"
        found = await self._fetch_category(category, limit, exclude_ids)
        if len(found) < limit:
            skip = list(exclude_ids) + [v.id for v in found]
            found += await self._fetch_category(other, limit - len(found), skip, order_like=category)
        return found

    async def _fetch_category(self, category: str, limit: int, exclude_ids: list[int],
                              order_like: str | None = None) -> list[Video]:
        kind = order_like or category
        order = self.order_sql(kind)
        rows = await self.db.pool.fetch(
            f"""
            SELECT * FROM (
                SELECT DISTINCT ON (telegram_file_unique_id) *
                FROM videos
                WHERE category = $1 AND is_active AND NOT (id = ANY($3::bigint[]))
                ORDER BY telegram_file_unique_id, id
            ) v
            ORDER BY {order}
            LIMIT $2
            """,
            category, limit, exclude_ids,
        )
        return [Video.from_record(r) for r in rows]

    # ---------- per-user delivery (user_video_history) ----------
    # Every channel post is a row. Admin setting "allow duplicates":
    #   OFF (default): a video that was posted several times is delivered ONCE per user - the
    #                  copy is picked by category (New -> latest post, Old -> oldest post) and a
    #                  video the user already got is never sent again through another copy.
    #   ON           : every post is delivered, copies included; only the SAME post is never
    #                  repeated for one user.
    @staticmethod
    def _pick(category: str) -> str:
        return _PICK_LATEST if category == "new" else _PICK_OLDEST

    def _seen_clause(self, user_param: str) -> str:
        if self.allow_duplicates:
            return (f"AND NOT EXISTS (SELECT 1 FROM user_video_history h "
                    f"WHERE h.user_id = {user_param} AND h.video_id = videos.id)")
        return (f"AND NOT EXISTS (SELECT 1 FROM user_video_history h "
                f"JOIN videos hv ON hv.id = h.video_id WHERE h.user_id = {user_param} "
                f"AND hv.telegram_file_unique_id = videos.telegram_file_unique_id)")

    def _library(self, category: str, extra: str = "") -> str:
        """Subquery with the videos of a category that may be delivered ($1 = category)."""
        base = f"FROM videos WHERE category = $1 AND is_active {extra}"
        if self.allow_duplicates:
            return f"SELECT * {base}"
        return (f"SELECT DISTINCT ON (telegram_file_unique_id) * {base} "
                f"ORDER BY telegram_file_unique_id, {self._pick(category)}")

    async def fetch_unseen(self, user_id: int, category: str, limit: int,
                           exclude_ids: list[int], shuffle: bool = False) -> list[Video]:
        """Videos of ONE category that this user has not received yet.
        New = latest post -> oldest post, Old = oldest post -> latest post (or the admin's
        chosen order), shuffle = random. Anything in user_video_history never comes back."""
        order = "random()" if shuffle else self.order_sql(category)
        inner = self._library(category, "AND NOT (id = ANY($3::bigint[])) "
                              + self._seen_clause("$4"))
        rows = await self.db.pool.fetch(
            f"SELECT * FROM ({inner}) v ORDER BY {order} LIMIT $2",
            category, limit, exclude_ids, user_id,
        )
        return [Video.from_record(r) for r in rows]

    async def count_unseen(self, user_id: int, category: str) -> int:
        inner = self._library(category, self._seen_clause("$2"))
        return await self.db.pool.fetchval(
            f"SELECT COUNT(*) FROM ({inner}) v", category, user_id)

    async def count_seen_ahead(self, user_id: int, category: str, limit: int) -> int:
        """How many of the first `limit` videos of the unfiltered list the user already has -
        i.e. how many duplicates the history filter is about to prevent."""
        order = self.order_sql(category)
        inner = self._library(category)
        seen = (("h.video_id = t.id") if self.allow_duplicates else
                ("h.video_id IN (SELECT id FROM videos WHERE telegram_file_unique_id = "
                 "t.telegram_file_unique_id)"))
        return await self.db.pool.fetchval(
            f"""
            SELECT COUNT(*) FROM (
                SELECT * FROM ({inner}) v ORDER BY {order} LIMIT $2
            ) t
            WHERE EXISTS (SELECT 1 FROM user_video_history h
                          WHERE h.user_id = $3 AND {seen})
            """,
            category, limit, user_id,
        )

    async def bump_counter(self, key: str, amount: int) -> None:
        if amount <= 0:
            return
        async with self.db.pool.acquire() as conn:
            await self._bump(conn, key, amount)

    async def last_request_size(self, user_id: int) -> int | None:
        return await self.db.pool.fetchval(
            "SELECT requested FROM requests WHERE user_id = $1 AND delivered > 0 "
            "ORDER BY id DESC LIMIT 1", user_id)

    async def user_activity(self, user_id: int) -> dict:
        pool = self.db.pool
        h = await pool.fetchrow(
            "SELECT COUNT(*) AS total, "
            "COUNT(*) FILTER (WHERE category = 'new') AS new, "
            "COUNT(*) FILTER (WHERE category = 'old') AS old "
            "FROM user_video_history WHERE user_id = $1", user_id)
        reqs = await pool.fetchval(
            "SELECT COUNT(*) FROM requests WHERE user_id = $1 AND delivered > 0", user_id)
        return {"total": h["total"], "new": h["new"], "old": h["old"], "requests": reqs,
                "left_new": await self.count_unseen(user_id, "new"),
                "left_old": await self.count_unseen(user_id, "old")}

    async def reset_user_history(self, user_id: int) -> int:
        """Admin only: let a user receive videos again. Returns the number of rows removed."""
        rows = await self.db.pool.fetch(
            "DELETE FROM user_video_history WHERE user_id = $1 RETURNING 1", user_id)
        return len(rows)

    async def delivery_stats(self) -> dict:
        pool = self.db.pool
        deliveries = await pool.fetchval("SELECT COALESCE(SUM(delivered), 0) FROM requests")
        prevented = await pool.fetchval(
            "SELECT value::bigint FROM meta WHERE key = 'delivery_duplicates_prevented'")
        scan = await pool.fetchrow(
            "SELECT status, videos_found, videos_added, duplicates FROM scan_state WHERE id = 1")
        return {"deliveries": deliveries, "prevented": prevented or 0, "scan": scan}

    async def stats(self) -> dict:
        pool = self.db.pool
        rows = await pool.fetch(
            "SELECT COALESCE(category, 'pending') AS cat, is_active, COUNT(*) AS n "
            "FROM videos GROUP BY 1, 2"
        )
        users = await pool.fetchval("SELECT COUNT(*) FROM users")
        req = await pool.fetchrow(
            "SELECT COUNT(*) AS c, COALESCE(SUM(delivered), 0) AS d FROM requests"
        )
        dupes = await pool.fetchval(
            "SELECT value::bigint FROM meta WHERE key = 'duplicates_prevented'"
        )
        return {"videos": rows, "users": users, "requests": req["c"],
                "delivered": req["d"], "duplicates_prevented": dupes or 0}

    # ---------- admin panel queries ----------
    async def search(self, text: str, limit: int = 10) -> list[Video]:
        """Find videos by database ID, video number or caption text."""
        text = text.strip().lstrip("#")
        if text.isdigit():
            n = int(text)
            rows = await self.db.pool.fetch(
                "SELECT * FROM videos WHERE id = $1 OR video_number = $1 "
                "ORDER BY video_number NULLS LAST, id LIMIT $2", n, limit)
        else:
            like = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            rows = await self.db.pool.fetch(
                "SELECT * FROM videos WHERE caption ILIKE $1 "
                "ORDER BY video_number NULLS LAST, id LIMIT $2", like, limit)
        return [Video.from_record(r) for r in rows]

    async def dashboard(self) -> dict:
        pool = self.db.pool
        row = await pool.fetchrow(
            "SELECT COUNT(*) FILTER (WHERE category IS NOT NULL AND is_active) AS total, "
            "COUNT(*) FILTER (WHERE category = 'new' AND is_active) AS new, "
            "COUNT(*) FILTER (WHERE category = 'old' AND is_active) AS old, "
            "COUNT(*) FILTER (WHERE category IS NULL) AS pending, "
            "COUNT(*) FILTER (WHERE NOT is_active) AS inactive, "
            "COUNT(*) FILTER (WHERE created_at > now() - interval '24 hours') AS added_24h, "
            "COUNT(*) FILTER (WHERE created_at > now() - interval '7 days') AS added_7d "
            "FROM videos")
        users = await pool.fetchrow(
            "SELECT COUNT(*) AS total, "
            "COUNT(*) FILTER (WHERE last_seen > now() - interval '24 hours') AS active_24h, "
            "COUNT(*) FILTER (WHERE last_seen > now() - interval '7 days') AS active_7d, "
            "COUNT(*) FILTER (WHERE is_banned) AS banned FROM users")
        req = await pool.fetchrow(
            "SELECT COALESCE(SUM(delivered), 0) AS delivered, COALESCE(SUM(failed), 0) AS failed, "
            "COUNT(*) AS requests FROM requests")
        meta = {r["key"]: r["value"] for r in await pool.fetch(
            "SELECT key, value FROM meta WHERE key = ANY($1::text[])",
            ["delivery_duplicates_prevented", "auto_deleted_total", "scans_total"])}

        def m(key: str) -> int:
            try:
                return int(meta.get(key, 0))
            except ValueError:
                return 0
        return {**dict(row), "users": users["total"], "active_24h": users["active_24h"],
                "active_7d": users["active_7d"], "banned": users["banned"],
                "deliveries": req["delivered"], "failed": req["failed"],
                "requests": req["requests"], "prevented": m("delivery_duplicates_prevented"),
                "auto_deleted": m("auto_deleted_total"), "scans": m("scans_total")}
