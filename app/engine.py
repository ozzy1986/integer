"""Task state machine: the only place where tasks and attempts change state.

Concurrency model: every change of a task or its attempts happens in one transaction that first locks
the task row (SELECT ... FOR NO KEY UPDATE). Webhooks, the dispatcher and the watchdog of any worker therefore
serialise per task, and each re-reads the attempt *after* taking the lock, so a duplicate sees the
already-applied result and becomes a no-op. Money rows are additionally protected by unique indexes.

Webhook failures, synchronous send failures and the watchdog all end in `_handle_failure`, so rules
2-4 are implemented exactly once.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from app import billing, catalog
from app.db import Connection
from app.domain import (
    LIVE_ATTEMPT_STATUSES,
    AttemptStatus,
    Leg,
    ProviderError,
    TaskStatus,
    first_leg,
    next_leg,
)

log = logging.getLogger(__name__)


class Outcome(StrEnum):
    NEXT_ATTEMPT = "next_attempt"
    TASK_FAILED = "task_failed"
    TASK_SUCCEEDED = "task_succeeded"
    LATE_RESULT_ACCEPTED = "late_result_accepted"
    LATE_RESULT_IGNORED = "late_result_ignored"
    SENT = "sent"
    IGNORED = "ignored"
    UNKNOWN = "unknown"


class GenerateError(Exception):
    status_code = 400

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class UnknownModel(GenerateError):
    status_code = 404


class NoSuitableLeg(GenerateError):
    status_code = 400


class TooManyActiveTasks(GenerateError):
    status_code = 429


class InsufficientFunds(GenerateError):
    status_code = 402


class IdempotencyConflict(GenerateError):
    status_code = 409


@dataclass(frozen=True, slots=True)
class GenerateRequest:
    user_id: int
    model_slug: str
    prompt: str
    ref_images: tuple[str, ...]
    idempotency_key: str | None = None

    def fingerprint(self) -> str:
        payload = [self.model_slug, self.prompt, list(self.ref_images)]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CreatedTask:
    task_id: UUID
    created: bool  # False when an Idempotency-Key replay returned an existing task


@dataclass(frozen=True, slots=True)
class _Locked:
    task_id: UUID
    user_id: int
    model_slug: str
    task_status: TaskStatus
    price: int
    ref_count: int
    attempt_id: UUID
    number: int
    leg_priority: int
    provider: str
    status: AttemptStatus
    deadline_passed: bool


async def create_task(conn: Connection, request: GenerateRequest, max_active: int) -> CreatedTask:
    model = await catalog.get_model(conn, request.model_slug)
    if model is None:
        raise UnknownModel(f"unknown model {request.model_slug!r}")
    leg = first_leg(model.legs, len(request.ref_images))
    if leg is None:
        raise NoSuitableLeg(f"no leg of {model.slug} accepts {len(request.ref_images)} reference images")

    fingerprint = request.fingerprint() if request.idempotency_key else None
    async with conn.transaction():
        await billing.ensure_user(conn, request.user_id)
        # The user row lock serialises everything that reads-then-writes this user's money and limits.
        balance = await billing.lock_user_balance(conn, request.user_id)
        if request.idempotency_key:
            existing = await conn.fetchrow(
                "SELECT id, request_hash FROM tasks WHERE user_id = $1 AND idempotency_key = $2",
                request.user_id, request.idempotency_key,
            )  # fmt: skip
            if existing:
                if existing["request_hash"] != fingerprint:
                    raise IdempotencyConflict("Idempotency-Key was already used for a different request")
                return CreatedTask(existing["id"], created=False)
        active = await conn.fetchval(
            "SELECT count(*) FROM tasks WHERE user_id = $1 AND status = 'in_progress'", request.user_id
        )
        if active >= max_active:
            raise TooManyActiveTasks(f"user already has {active} active tasks")
        if balance < model.price:
            raise InsufficientFunds(f"price is {model.price} tokens, balance is {balance}")

        task_id: UUID = await conn.fetchval(
            """INSERT INTO tasks (user_id, model_slug, prompt, ref_images, price, idempotency_key, request_hash)
               VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
            request.user_id, model.slug, request.prompt, list(request.ref_images), model.price,
            request.idempotency_key, fingerprint,
        )  # fmt: skip
        await _event(conn, task_id, None, "created", model=model.slug, refs=len(request.ref_images))
        await billing.charge(conn, request.user_id, task_id, model.price)
        await _event(conn, task_id, None, "charged", amount=model.price)
        await _new_attempt(conn, task_id, 1, leg, log_event=False)
    return CreatedTask(task_id, created=True)


async def mark_sent(conn: Connection, attempt_id: UUID, external_id: str, watchdog_seconds: float) -> Outcome:
    """The provider accepted the attempt (202): start the watchdog clock."""
    async with conn.transaction():
        locked = await _lock(conn, attempt_id)
        if locked is None:
            return Outcome.UNKNOWN
        if locked.status is not AttemptStatus.PENDING:
            # The webhook beat the 202 (or the attempt was superseded): keep its outcome, note the id.
            await conn.execute(
                "UPDATE attempts SET external_id = coalesce(external_id, $2), lease_until = NULL WHERE id = $1",
                attempt_id, external_id,
            )  # fmt: skip
            return Outcome.IGNORED
        await conn.execute(
            """UPDATE attempts SET status = 'sent', external_id = $2, sent_at = now(), lease_until = NULL,
                      deadline_at = now() + make_interval(secs => $3)
                WHERE id = $1""",
            attempt_id, external_id, watchdog_seconds,
        )  # fmt: skip
        await _event(
            conn,
            locked.task_id,
            attempt_id,
            "attempt_sent",
            provider=locked.provider,
            external_id=external_id,
        )
        return Outcome.SENT


async def fail_attempt(
    conn: Connection,
    attempt_id: UUID,
    error: ProviderError,
    reason: str,
    *,
    provider: str | None = None,
) -> Outcome:
    """A failure reported by the provider's webhook or seen while sending (rules 2-4)."""
    async with conn.transaction():
        locked = await _lock(conn, attempt_id, provider=provider)
        if locked is None:
            return Outcome.UNKNOWN
        if locked.status not in LIVE_ATTEMPT_STATUSES:
            await _event(conn, locked.task_id, attempt_id, "duplicate_ignored", status="failed", error=error)
            return Outcome.IGNORED
        return await _handle_failure(conn, locked, error, reason)


async def expire_next_overdue(conn: Connection) -> Outcome | None:
    """Watchdog (rule 5): fail the most overdue attempt whose task nobody else is busy with; None if there is
    none. SKIP LOCKED lets the watchdogs of all workers share a backlog instead of queueing on the same rows."""
    async with conn.transaction():
        attempt_id = await conn.fetchval(
            """SELECT a.id FROM attempts a JOIN tasks t ON t.id = a.task_id
                WHERE a.status = 'sent' AND a.deadline_at < now()
                ORDER BY a.deadline_at LIMIT 1
                  FOR NO KEY UPDATE OF t SKIP LOCKED"""
        )
        if attempt_id is None:
            return None
        # The lock is ours already; this re-reads the attempt as the previous lock holder left it.
        locked = await _lock(conn, attempt_id)
        if locked is None or locked.status is not AttemptStatus.SENT or not locked.deadline_passed:
            return Outcome.IGNORED
        return await _handle_failure(conn, locked, ProviderError.TECHNICAL, "watchdog_timeout")


async def complete_attempt(
    conn: Connection, attempt_id: UUID, result_url: str, *, provider: str | None = None
) -> Outcome:
    """A `completed` webhook. Rule 6 is decided here, see README."""
    async with conn.transaction():
        locked = await _lock(conn, attempt_id, provider=provider)
        if locked is None:
            return Outcome.UNKNOWN
        task_id = locked.task_id
        if locked.status is AttemptStatus.SUCCEEDED or locked.task_status is TaskStatus.SUCCEEDED:
            await _event(conn, task_id, attempt_id, "duplicate_ignored", status="completed")
            return Outcome.IGNORED
        if locked.task_status is TaskStatus.FAILED:
            # The user already got the refund; delivering the result too would pay out twice.
            await _event(conn, task_id, attempt_id, "late_result_ignored", result_url=result_url)
            return Outcome.LATE_RESULT_IGNORED

        late = locked.status not in LIVE_ATTEMPT_STATUSES
        # A late result clears the error the watchdog set; `reason` still tells that it gave up first.
        await conn.execute(
            "UPDATE attempts SET status = 'succeeded', result_url = $2, error = NULL, finished_at = now(), "
            "lease_until = NULL WHERE id = $1",
            attempt_id, result_url,
        )  # fmt: skip
        superseded = await conn.fetch(
            "UPDATE attempts SET status = 'superseded', finished_at = now(), lease_until = NULL "
            "WHERE task_id = $1 AND id <> $2 AND status IN ('pending', 'sent') RETURNING id",
            task_id, attempt_id,
        )  # fmt: skip
        await conn.execute(
            "UPDATE tasks SET status = 'succeeded', result_url = $2, finished_at = now() WHERE id = $1",
            task_id, result_url,
        )  # fmt: skip
        for row in superseded:
            await _event(conn, task_id, row["id"], "attempt_superseded")
        await _event(conn, task_id, attempt_id, "task_succeeded", result_url=result_url, late=late)
        return Outcome.LATE_RESULT_ACCEPTED if late else Outcome.TASK_SUCCEEDED


async def _handle_failure(conn: Connection, locked: _Locked, error: ProviderError, reason: str) -> Outcome:
    await conn.execute(
        "UPDATE attempts SET status = 'failed', error = $2, reason = $3, finished_at = now(), lease_until = NULL "
        "WHERE id = $1",
        locked.attempt_id, error, reason,
    )  # fmt: skip
    await _event(conn, locked.task_id, locked.attempt_id, "attempt_failed", error=error, reason=reason)

    # The catalog may have changed under a running task; a model left without legs has nowhere to go next.
    model = await catalog.get_model(conn, locked.model_slug)
    leg = next_leg(model.legs if model else (), locked.ref_count, locked.leg_priority, error)
    if leg is not None:
        await _new_attempt(conn, locked.task_id, locked.number + 1, leg)
        return Outcome.NEXT_ATTEMPT

    await conn.execute(
        "UPDATE tasks SET status = 'failed', error = $2, finished_at = now() WHERE id = $1",
        locked.task_id,
        error,
    )
    refunded = await billing.refund(conn, locked.user_id, locked.task_id, locked.price)
    await _event(conn, locked.task_id, None, "task_failed", error=error)
    if refunded:
        await _event(conn, locked.task_id, None, "refunded", amount=locked.price)
    return Outcome.TASK_FAILED


async def _lock(conn: Connection, attempt_id: UUID, *, provider: str | None = None) -> _Locked | None:
    """Lock the attempt's task, then read the attempt. Two statements on purpose: under READ COMMITTED
    the second one sees whatever the previous lock holder committed.

    FOR NO KEY UPDATE, not FOR UPDATE: transitions change columns, never the key, so foreign-key checks of
    rows that merely reference the task (KEY SHARE) need not wait for a transition to finish."""
    task = await conn.fetchrow(
        """SELECT id, user_id, model_slug, status, price, cardinality(ref_images) AS ref_count
             FROM tasks WHERE id = (SELECT task_id FROM attempts WHERE id = $1)
              FOR NO KEY UPDATE""",
        attempt_id,
    )
    if task is None:
        return None
    attempt = await conn.fetchrow(
        "SELECT number, leg_priority, provider, status, coalesce(deadline_at < now(), false) AS deadline_passed "
        "FROM attempts WHERE id = $1",
        attempt_id,
    )
    assert attempt is not None
    if provider is not None and attempt["provider"] != provider:
        return None  # a provider may only report on its own attempts
    return _Locked(
        task_id=task["id"],
        user_id=task["user_id"],
        model_slug=task["model_slug"],
        task_status=TaskStatus(task["status"]),
        price=task["price"],
        ref_count=task["ref_count"],
        attempt_id=attempt_id,
        number=attempt["number"],
        leg_priority=attempt["leg_priority"],
        provider=attempt["provider"],
        status=AttemptStatus(attempt["status"]),
        deadline_passed=attempt["deadline_passed"],
    )


async def _new_attempt(conn: Connection, task_id: UUID, number: int, leg: Leg, *, log_event: bool = True) -> None:
    attempt_id = await conn.fetchval(
        "INSERT INTO attempts (task_id, number, leg_priority, provider) VALUES ($1, $2, $3, $4) RETURNING id",
        task_id, number, leg.priority, leg.provider,
    )  # fmt: skip
    if log_event:
        await _event(conn, task_id, attempt_id, "attempt_created", provider=leg.provider, priority=leg.priority)


async def _event(conn: Connection, task_id: UUID, attempt_id: UUID | None, kind: str, **detail: Any) -> None:
    await conn.execute(
        "INSERT INTO task_events (task_id, attempt_id, kind, detail) VALUES ($1, $2, $3, $4)",
        task_id, attempt_id, kind, detail,
    )  # fmt: skip
    log.info("task %s: %s %s", task_id, kind, json.dumps(detail, ensure_ascii=False))
