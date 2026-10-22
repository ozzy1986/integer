"""The service connects as `integer_app`, a role with only the privileges it needs.

The money triggers are only as strong as the role they guard against: a superuser can switch triggers
off (session_replication_role, ALTER TABLE ... DISABLE TRIGGER). The service role cannot, so even a
SQL injection could not bypass the ledger.
"""

import asyncio
import importlib.util
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import ModuleType

import asyncpg
import pytest

from tests.conftest import ROOT, database_url
from tests.db.test_ledger_invariants import credit, make_task, make_user

SERVICE_ROLE = "integer_app"


@asynccontextmanager
async def as_service(pool: asyncpg.Pool) -> AsyncIterator[asyncpg.Connection]:
    """A connection acting as the service role, inside a transaction that is always rolled back."""
    async with pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            user_id = await make_user(conn)
            await credit(conn, user_id, 10)
            await make_task(conn, user_id)
            await conn.execute(f"SET LOCAL ROLE {SERVICE_ROLE}")
            yield conn
        finally:
            await transaction.rollback()


@pytest.mark.parametrize(
    "statement",
    [
        "SET session_replication_role = replica",  # would silence every trigger
        "ALTER TABLE ledger DISABLE TRIGGER ALL",
        "DROP TRIGGER users_balance_guard ON users",
        "UPDATE ledger SET amount = 1000",
        "DELETE FROM ledger",
        "TRUNCATE ledger CASCADE",
        "UPDATE tasks SET price = 1",
        "UPDATE tasks SET user_id = 1",
        "DELETE FROM tasks",
        "UPDATE payments SET tokens = 1000000",
        "UPDATE models SET price = 1",
        "INSERT INTO legs (model_slug, priority, provider, max_ref_images) VALUES ('image-fast', 9, 'evil', 99)",
        "CREATE TABLE sneaky (id int)",
    ],
)
async def test_the_service_role_cannot_bypass_the_money_rules(pool: asyncpg.Pool, statement: str) -> None:
    async with as_service(pool) as conn:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(statement)


async def test_the_service_role_moves_a_balance_only_through_the_ledger(pool: asyncpg.Pool) -> None:
    # It may update users.balance (the ledger trigger runs with its privileges), but the guard still says no.
    async with as_service(pool) as conn:
        with pytest.raises(asyncpg.RaiseError, match="only through the ledger"):
            await conn.execute("UPDATE users SET balance = balance + 100")


async def test_every_table_is_readable_by_the_service_role(pool: asyncpg.Pool) -> None:
    # A table added by a later migration without its GRANTs would only break in production.
    missing = await pool.fetch(
        """SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v') AND c.relname <> 'alembic_version'
              AND NOT has_table_privilege($1, c.oid, 'SELECT')""",
        SERVICE_ROLE,
    )
    assert [row["relname"] for row in missing] == []


def load_login_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("service_login", ROOT / "migrations" / "service_login.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_login_script_lets_the_role_log_in_with_the_password(pool: asyncpg.Pool) -> None:
    # A throwaway role: the real one may be in use by a running service on the same server.
    role = f"integer_login_test_{secrets.token_hex(4)}"
    await pool.execute(f"CREATE ROLE {role} NOLOGIN")
    try:
        script = load_login_script()
        await asyncio.to_thread(script.grant_login, database_url(), role, "p" * 32)
        assert await pool.fetchval("SELECT rolcanlogin FROM pg_roles WHERE rolname = $1", role)
        with pytest.raises(ValueError, match="at least"):
            script.grant_login(database_url(), role, "short")
    finally:
        await pool.execute(f"DROP ROLE {role}")
