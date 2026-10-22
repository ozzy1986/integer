"""Shared fixtures: a real PostgreSQL database, migrated from scratch once per test session.

Tests never truncate anything (the ledger is append-only by design); instead every test works with
its own random user ids, so tests are independent and can share one database.
"""

import asyncio
import os
import secrets
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parent.parent


def _load_env_local() -> None:
    """Developer convenience: TEST_DATABASE_URL may live in the git-ignored .env.local."""
    env_file = ROOT / ".env.local"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip())


_load_env_local()


def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.exit("TEST_DATABASE_URL is not set (see README: running tests)", returncode=2)
    return url


def run_migrations(url: str) -> None:
    """upgrade → downgrade → upgrade: every test session also proves the downgrades work."""
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = url
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")


async def reset_schema(url: str) -> None:
    conn = await asyncpg.connect(url)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    finally:
        await conn.close()


@pytest.fixture(scope="session")
async def migrated_db() -> str:
    url = database_url()
    await reset_schema(url)
    # Alembic is synchronous; keep it off the event loop.
    await asyncio.to_thread(run_migrations, url)
    return url


@pytest.fixture(scope="session")
async def pool(migrated_db: str) -> AsyncIterator[asyncpg.Pool]:
    async with asyncpg.create_pool(migrated_db, min_size=2, max_size=30) as created:
        yield created


def new_user_id() -> int:
    # Telegram-like positive ids; random so tests never collide.
    return secrets.randbelow(2**52) + 1
