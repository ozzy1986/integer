"""Every money movement is one ledger row. The database moves the balance (see migration 0001);
this module only decides which row to write. All functions expect to run inside a transaction."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.db import Connection


class PaymentStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PENDING = "pending"
    FAILED = "failed"
    CANCELED = "canceled"


class PaymentConflict(Exception):
    """The same payment_id arrived with a different user or amount."""


@dataclass(frozen=True, slots=True)
class Payment:
    payment_id: str
    user_id: int
    tokens: int
    status: PaymentStatus


async def ensure_user(conn: Connection, user_id: int) -> None:
    await conn.execute("INSERT INTO users (id) VALUES ($1) ON CONFLICT (id) DO NOTHING", user_id)


async def lock_user_balance(conn: Connection, user_id: int) -> int:
    balance: int = await conn.fetchval("SELECT balance FROM users WHERE id = $1 FOR UPDATE", user_id)
    return balance


async def charge(conn: Connection, user_id: int, task_id: UUID, amount: int) -> None:
    await conn.execute(
        "INSERT INTO ledger (user_id, kind, amount, task_id) VALUES ($1, 'charge', $2, $3)",
        user_id, -amount, task_id,
    )  # fmt: skip


async def refund(conn: Connection, user_id: int, task_id: UUID, amount: int) -> bool:
    """Refund the task's charge. The unique index makes a second refund a no-op, never a double credit."""
    row_id = await conn.fetchval(
        "INSERT INTO ledger (user_id, kind, amount, task_id) VALUES ($1, 'refund', $2, $3) "
        "ON CONFLICT (task_id) WHERE kind = 'refund' DO NOTHING RETURNING id",
        user_id, amount, task_id,
    )  # fmt: skip
    return row_id is not None


async def apply_payment(conn: Connection, payment: Payment) -> bool:
    """Record a payment notification; credit it at most once. Returns True if this call credited it.

    Concurrent duplicates serialise on the payments row lock; the partial unique index on the ledger is
    the second line of defence.
    """
    await ensure_user(conn, payment.user_id)
    # Lock order is always user -> payment. Inserting the payment first would take a KEY SHARE lock on
    # the user (foreign key) and two concurrent payments of one user would deadlock on the upgrade.
    await lock_user_balance(conn, payment.user_id)
    await conn.execute(
        "INSERT INTO payments (id, user_id, tokens, status) VALUES ($1, $2, $3, $4) ON CONFLICT (id) DO NOTHING",
        payment.payment_id, payment.user_id, payment.tokens, payment.status,
    )  # fmt: skip
    stored = await conn.fetchrow(
        "SELECT user_id, tokens, status FROM payments WHERE id = $1 FOR UPDATE", payment.payment_id
    )
    assert stored is not None
    if (stored["user_id"], stored["tokens"]) != (payment.user_id, payment.tokens):
        raise PaymentConflict(payment.payment_id)
    if stored["status"] == PaymentStatus.SUCCEEDED:
        pass  # final: a late "pending" must not downgrade it
    elif stored["status"] != payment.status:
        await conn.execute(
            "UPDATE payments SET status = $2, updated_at = now() WHERE id = $1",
            payment.payment_id,
            payment.status,
        )
    if payment.status != PaymentStatus.SUCCEEDED:
        return False
    row_id = await conn.fetchval(
        "INSERT INTO ledger (user_id, kind, amount, payment_id) VALUES ($1, 'payment', $2, $3) "
        "ON CONFLICT (payment_id) WHERE kind = 'payment' DO NOTHING RETURNING id",
        payment.user_id, payment.tokens, payment.payment_id,
    )  # fmt: skip
    return row_id is not None
