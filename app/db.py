import json

import asyncpg
from asyncpg.pool import PoolConnectionProxy

from app.config import Settings

# A plain connection or one borrowed from the pool: the code accepts both.
type Connection = asyncpg.Connection[asyncpg.Record] | PoolConnectionProxy[asyncpg.Record]


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(settings: Settings) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(
        settings.database_url.get_secret_value(),
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        init=_init_connection,
        command_timeout=30,
    )
    assert pool is not None
    return pool
