"""Alembic environment: online migrations only, through psycopg 3.

psycopg (not asyncpg) because migrations are plain multi-statement SQL scripts, which asyncpg's
prepared statements cannot run. The application itself uses asyncpg.
"""

import os

from alembic import context
from sqlalchemy import create_engine, pool, text

# Serialises concurrent `alembic upgrade` runs (e.g. two containers starting at once).
MIGRATION_LOCK_ID = 7_301_986


def _url() -> str:
    url = context.config.attributes.get("database_url") or os.environ["DATABASE_URL"]
    return str(url).replace("postgresql://", "postgresql+psycopg://", 1)


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None, transactional_ddl=True)
        with context.begin_transaction():
            connection.execute(text(f"SELECT pg_advisory_xact_lock({MIGRATION_LOCK_ID})"))
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline mode is not supported: run `alembic upgrade head` against a database")
run_migrations_online()
