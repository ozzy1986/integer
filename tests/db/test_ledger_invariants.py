"""The money invariants are enforced by the database itself, not only by application code.

Even a buggy query cannot change a balance without a ledger row, credit a payment twice
or refund a task twice: these tests talk to the tables directly to prove it.
"""

import uuid

import asyncpg
import pytest

from tests.conftest import new_user_id


async def make_user(conn: asyncpg.Connection) -> int:
    user_id = new_user_id()
    await conn.execute("INSERT INTO users (id) VALUES ($1)", user_id)
    return user_id


async def credit(conn: asyncpg.Connection, user_id: int, tokens: int) -> str:
    payment_id = f"pay-{uuid.uuid4()}"
    await conn.execute(
        "INSERT INTO payments (id, user_id, tokens, status) VALUES ($1, $2, $3, 'succeeded')",
        payment_id,
        user_id,
        tokens,
    )
    await conn.execute(
        "INSERT INTO ledger (user_id, kind, amount, payment_id) VALUES ($1, 'payment', $2, $3)",
        user_id,
        tokens,
        payment_id,
    )
    return payment_id


async def make_task(conn: asyncpg.Connection, user_id: int, price: int = 2) -> uuid.UUID:
    task_id: uuid.UUID = await conn.fetchval(
        "INSERT INTO tasks (user_id, model_slug, prompt, price) VALUES ($1, 'image-fast', 'p', $2) RETURNING id",
        user_id,
        price,
    )
    return task_id


async def balance(conn: asyncpg.Connection, user_id: int) -> int:
    value: int = await conn.fetchval("SELECT balance FROM users WHERE id = $1", user_id)
    return value


async def test_ledger_row_moves_balance_and_records_balance_after(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        user_id = await make_user(conn)
        await credit(conn, user_id, 10)
        await credit(conn, user_id, 5)
        assert await balance(conn, user_id) == 15
        rows = await conn.fetch("SELECT amount, balance_after FROM ledger WHERE user_id = $1 ORDER BY id", user_id)
        assert [(r["amount"], r["balance_after"]) for r in rows] == [(10, 10), (5, 15)]


async def test_balance_cannot_be_changed_without_a_ledger_row(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        user_id = await make_user(conn)
        with pytest.raises(asyncpg.RaiseError, match="only through the ledger"):
            await conn.execute("UPDATE users SET balance = 100 WHERE id = $1", user_id)
        with pytest.raises(asyncpg.RaiseError, match="only through the ledger"):
            await conn.execute("INSERT INTO users (id, balance) VALUES ($1, 100)", new_user_id())


async def test_ledger_is_append_only(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        user_id = await make_user(conn)
        await credit(conn, user_id, 10)
        with pytest.raises(asyncpg.RaiseError, match="append-only"):
            await conn.execute("UPDATE ledger SET amount = 1000 WHERE user_id = $1", user_id)
        with pytest.raises(asyncpg.RaiseError, match="append-only"):
            await conn.execute("DELETE FROM ledger WHERE user_id = $1", user_id)
        with pytest.raises(asyncpg.RaiseError, match="append-only"):
            await conn.execute("TRUNCATE ledger CASCADE")


async def test_balance_never_goes_negative(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        user_id = await make_user(conn)
        await credit(conn, user_id, 1)
        task_id = await make_task(conn, user_id, price=2)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO ledger (user_id, kind, amount, task_id) VALUES ($1, 'charge', -2, $2)",
                user_id,
                task_id,
            )
        assert await balance(conn, user_id) == 1


async def test_payment_is_credited_at_most_once(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        user_id = await make_user(conn)
        payment_id = await credit(conn, user_id, 7)
        insert = "INSERT INTO ledger (user_id, kind, amount, payment_id) VALUES ($1, 'payment', 7, $2)"
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(insert, user_id, payment_id)
        # ON CONFLICT DO NOTHING must not move the balance either (balance moves in an AFTER trigger).
        await conn.execute(insert + " ON CONFLICT (payment_id) WHERE kind = 'payment' DO NOTHING", user_id, payment_id)
        assert await balance(conn, user_id) == 7


async def test_task_is_charged_once_and_refunded_once_for_the_exact_price(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        user_id = await make_user(conn)
        await credit(conn, user_id, 10)
        task_id = await make_task(conn, user_id, price=2)
        charge = "INSERT INTO ledger (user_id, kind, amount, task_id) VALUES ($1, 'charge', -2, $2)"
        await conn.execute(charge, user_id, task_id)
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(charge, user_id, task_id)

        refund = "INSERT INTO ledger (user_id, kind, amount, task_id) VALUES ($1, 'refund', $2, $3)"
        with pytest.raises(asyncpg.CheckViolationError, match="does not match"):
            await conn.execute(refund, user_id, 5, task_id)
        await conn.execute(refund, user_id, 2, task_id)
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(refund, user_id, 2, task_id)
        assert await balance(conn, user_id) == 10


async def test_a_task_is_charged_to_its_own_user_only(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        owner, stranger = await make_user(conn), await make_user(conn)
        await credit(conn, stranger, 10)
        task_id = await make_task(conn, owner)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO ledger (user_id, kind, amount, task_id) VALUES ($1, 'charge', -2, $2)",
                stranger,
                task_id,
            )
        assert await balance(conn, stranger) == 10


async def test_only_a_failed_attempt_has_an_error_type(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        task_id = await make_task(conn, await make_user(conn))
        attempt_id = await conn.fetchval(
            "INSERT INTO attempts (task_id, number, leg_priority, provider) VALUES ($1, 1, 1, 'alpha') RETURNING id",
            task_id,
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE attempts SET status = 'failed' WHERE id = $1", attempt_id)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE attempts SET status = 'succeeded', error = 'technical', result_url = 'r' WHERE id = $1",
                attempt_id,
            )


async def test_refund_requires_a_charge(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        user_id = await make_user(conn)
        task_id = await make_task(conn, user_id)
        with pytest.raises(asyncpg.CheckViolationError, match="does not match"):
            await conn.execute(
                "INSERT INTO ledger (user_id, kind, amount, task_id) VALUES ($1, 'refund', 2, $2)",
                user_id,
                task_id,
            )


async def test_ledger_row_must_reference_what_it_is_for(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        user_id = await make_user(conn)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("INSERT INTO ledger (user_id, kind, amount) VALUES ($1, 'payment', 5)", user_id)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO ledger (user_id, kind, amount, payment_id) VALUES ($1, 'payment', -5, 'x')",
                user_id,
            )


async def test_multi_row_insert_cannot_corrupt_the_balance(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        user_id = await make_user(conn)
        p1, p2 = f"pay-{uuid.uuid4()}", f"pay-{uuid.uuid4()}"
        await conn.execute(
            "INSERT INTO payments (id, user_id, tokens, status) VALUES ($1, $3, 1, 'succeeded'), ($2, $3, 2, 'succeeded')",
            p1,
            p2,
            user_id,
        )
        with pytest.raises(asyncpg.RaiseError, match="one row per statement"):
            await conn.execute(
                "INSERT INTO ledger (user_id, kind, amount, payment_id) VALUES ($1, 'payment', 1, $2), ($1, 'payment', 2, $3)",
                user_id,
                p1,
                p2,
            )
        assert await balance(conn, user_id) == 0


async def test_balance_equals_ledger_sum_for_every_user(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        assert await conn.fetch("SELECT * FROM balance_drift") == []


async def test_only_one_live_attempt_per_task(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        user_id = await make_user(conn)
        task_id = await make_task(conn, user_id)
        insert = "INSERT INTO attempts (task_id, number, leg_priority, provider) VALUES ($1, $2, $2, 'alpha')"
        await conn.execute(insert, task_id, 1)
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(insert, task_id, 2)


async def test_catalog_is_seeded(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT m.slug, m.price, l.priority, l.provider, l.max_ref_images, l.skip_on_moderation "
            "FROM models m JOIN legs l ON l.model_slug = m.slug ORDER BY m.slug, l.priority"
        )
    assert [tuple(r) for r in rows] == [
        ("image-fast", 2, 1, "alpha", 4, False),
        ("image-fast", 2, 2, "beta", 10, False),
        ("video-long", 25, 1, "beta", 2, False),
        ("video-long", 25, 2, "alpha", 1, True),
    ]
