from database import Database


class UserService:
    """Users + database-backed session state (no Redis)."""

    def __init__(self, db: Database, ttl_minutes: int) -> None:
        self.db = db
        self.ttl = ttl_minutes
        self.banned_ids: set[int] = set()   # mirror of users.is_banned (single instance)

    async def upsert(self, user) -> None:
        await self.db.pool.execute(
            """
            INSERT INTO users (user_id, username, first_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE
            SET username = EXCLUDED.username, first_name = EXCLUDED.first_name,
                last_seen = now(), is_blocked = FALSE
            """,
            user.id, user.username, user.first_name,
        )

    async def mark_blocked(self, user_id: int) -> None:
        await self.db.pool.execute(
            "UPDATE users SET is_blocked = TRUE WHERE user_id = $1", user_id
        )

    async def set_state(self, user_id: int, state: str, data: dict | None = None) -> bool:
        """Set state unless a delivery is running. Returns False if user is busy."""
        row = await self.db.pool.fetchval(
            """
            INSERT INTO user_states (user_id, state, data) VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE
            SET state = EXCLUDED.state, data = EXCLUDED.data, updated_at = now()
            WHERE user_states.state <> 'delivering'
            RETURNING 1
            """,
            user_id, state, data or {},
        )
        return row is not None

    async def get_state(self, user_id: int) -> tuple[str | None, dict]:
        row = await self.db.pool.fetchrow(
            """
            SELECT state, data FROM user_states
            WHERE user_id = $1
              AND (state = 'delivering'
                   OR updated_at > now() - make_interval(mins => $2::int))
            """,
            user_id, self.ttl,
        )
        return (row["state"], row["data"]) if row else (None, {})

    async def claim_state(self, user_id: int, from_state: str, to_state: str) -> dict | None:
        """Atomically move state; guards against double taps and duplicate sessions."""
        row = await self.db.pool.fetchrow(
            """
            UPDATE user_states SET state = $3, updated_at = now()
            WHERE user_id = $1 AND state = $2
              AND updated_at > now() - make_interval(mins => $4::int)
            RETURNING data
            """,
            user_id, from_state, to_state, self.ttl,
        )
        return row["data"] if row else None

    async def clear_state(self, user_id: int) -> None:
        await self.db.pool.execute("DELETE FROM user_states WHERE user_id = $1", user_id)

    async def reset_delivering(self) -> None:
        """After a restart, no delivery is actually running any more."""
        await self.db.pool.execute("DELETE FROM user_states WHERE state = 'delivering'")

    # ---------- request lock, rate limit, one-time notice (upgrade) ----------
    async def acquire_delivery_lock(self, user_id: int) -> bool:
        """Atomically mark the user as 'delivering'. False = a request is already running.
        A lock older than 10 minutes is treated as dead (it cannot outlive a delivery)."""
        row = await self.db.pool.fetchval(
            """
            INSERT INTO user_states (user_id, state, data) VALUES ($1, 'delivering', '{}'::jsonb)
            ON CONFLICT (user_id) DO UPDATE
            SET state = 'delivering', data = '{}'::jsonb, updated_at = now()
            WHERE user_states.state <> 'delivering'
               OR user_states.updated_at < now() - interval '10 minutes'
            RETURNING 1
            """,
            user_id,
        )
        return row is not None

    async def hit_rate_limit(self, user_id: int, max_hits: int, window_seconds: int) -> bool:
        """Count one request in a fixed window. True = allowed, False = limit exceeded."""
        hits = await self.db.pool.fetchval(
            """
            INSERT INTO user_rate_limits (user_id, window_start, hits) VALUES ($1, now(), 1)
            ON CONFLICT (user_id) DO UPDATE SET
                hits = CASE WHEN user_rate_limits.window_start
                                 <= now() - make_interval(secs => $2::double precision)
                            THEN 1 ELSE user_rate_limits.hits + 1 END,
                window_start = CASE WHEN user_rate_limits.window_start
                                         <= now() - make_interval(secs => $2::double precision)
                                    THEN now() ELSE user_rate_limits.window_start END
            RETURNING hits
            """,
            user_id, float(window_seconds),
        )
        return hits <= max_hits

    async def claim_delete_notice(self, user_id: int) -> bool:
        """True exactly once per user: the first batch carries the auto-delete notice."""
        row = await self.db.pool.fetchval(
            "UPDATE users SET delete_notice_shown = TRUE "
            "WHERE user_id = $1 AND NOT delete_notice_shown RETURNING 1",
            user_id,
        )
        return row is not None

    # ---------- admin block system (users.is_banned, persistent) ----------
    async def load_banned(self) -> None:
        rows = await self.db.pool.fetch("SELECT user_id FROM users WHERE is_banned")
        self.banned_ids = {r["user_id"] for r in rows}

    def is_banned(self, user_id: int) -> bool:
        return user_id in self.banned_ids

    async def set_banned(self, user_id: int, banned: bool) -> None:
        """Works for users the bot has never seen too, so the block is in place when they arrive."""
        await self.db.pool.execute(
            """
            INSERT INTO users (user_id, is_banned, banned_at)
            VALUES ($1, $2, CASE WHEN $2 THEN now() END)
            ON CONFLICT (user_id) DO UPDATE
            SET is_banned = EXCLUDED.is_banned, banned_at = EXCLUDED.banned_at
            """,
            user_id, banned)
        (self.banned_ids.add if banned else self.banned_ids.discard)(user_id)

    async def touch(self, user_id: int) -> None:
        """Record activity (last_seen) for an existing user."""
        await self.db.pool.execute(
            "UPDATE users SET last_seen = now() WHERE user_id = $1", user_id)

    # ---------- admin: safe user information ----------
    _PROFILE_SQL = """
        SELECT u.user_id, u.first_name, u.username, u.first_seen, u.last_seen, u.is_banned,
               u.is_blocked,
               (SELECT COUNT(*) FROM user_video_history h WHERE h.user_id = u.user_id) AS videos,
               (SELECT COUNT(*) FROM requests r WHERE r.user_id = u.user_id) AS requests
        FROM users u
    """

    async def profile(self, user_id: int):
        return await self.db.pool.fetchrow(self._PROFILE_SQL + " WHERE u.user_id = $1", user_id)

    async def find(self, text: str):
        """Look a user up by Telegram ID or @username (only users who wrote to the bot exist)."""
        text = text.strip()
        if text.lstrip("-").isdigit():
            return await self.profile(int(text))
        name = text.lstrip("@")
        if not name:
            return None
        return await self.db.pool.fetchrow(
            self._PROFILE_SQL + " WHERE lower(u.username) = lower($1) "
            "ORDER BY u.last_seen DESC LIMIT 1", name)

    async def recent(self, limit: int, offset: int = 0) -> tuple[list, int]:
        rows = await self.db.pool.fetch(
            self._PROFILE_SQL + " ORDER BY u.last_seen DESC LIMIT $1 OFFSET $2", limit, offset)
        total = await self.db.pool.fetchval("SELECT COUNT(*) FROM users")
        return rows, total

    async def user_numbers(self) -> dict:
        r = await self.db.pool.fetchrow(
            "SELECT COUNT(*) AS total, "
            "COUNT(*) FILTER (WHERE last_seen > now() - interval '5 minutes') AS online, "
            "COUNT(*) FILTER (WHERE last_seen > now() - interval '24 hours') AS d1, "
            "COUNT(*) FILTER (WHERE last_seen > now() - interval '7 days') AS d7, "
            "COUNT(*) FILTER (WHERE first_seen > now() - interval '24 hours') AS new_1d, "
            "COUNT(*) FILTER (WHERE first_seen > now() - interval '7 days') AS new_7d, "
            "COUNT(*) FILTER (WHERE is_banned) AS banned, "
            "COUNT(*) FILTER (WHERE is_blocked) AS left_bot FROM users")
        return dict(r)
