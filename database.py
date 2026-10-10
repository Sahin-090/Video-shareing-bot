import json
import logging
from pathlib import Path

import asyncpg

from logging_setup import log_event

log = logging.getLogger("database")
SCHEMA_PATH = Path(__file__).parent / "database" / "schema.sql"


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


class Database:
    def __init__(self, dsn: str, min_size: int, max_size: int) -> None:
        self._dsn = dsn
        self._min = min_size
        self._max = max(max_size, min_size)
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min,
            max_size=self._max,
            command_timeout=30,
            init=_init_connection,
        )
        async with self.pool.acquire() as conn:
            await conn.execute("SELECT pg_advisory_lock(7000)")
            try:
                await conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
            finally:
                await conn.execute("SELECT pg_advisory_unlock(7000)")
        log_event(log, logging.INFO, "database_ready")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
