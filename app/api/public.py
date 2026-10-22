"""User-facing API: create a task, read tasks, balance and the catalog."""

from dataclasses import asdict
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Header, HTTPException, Path, Query, Response

from app import catalog, engine
from app.api.deps import RateLimited, ServicesDep, VerifiedUser, authorize
from app.domain import LIVE_ATTEMPT_STATUSES, TaskStatus
from app.schemas import (
    AttemptOut,
    BalanceOut,
    CurrentLeg,
    EventOut,
    GenerateIn,
    GenerateOut,
    HealthOut,
    IdempotencyKey,
    LedgerRow,
    LegOut,
    ModelOut,
    TaskOut,
    TaskSummary,
)

router = APIRouter()

# The same bounds as in request bodies: anything else could never be a user (and would not fit a bigint).
UserIdInPath = Annotated[int, Path(gt=0, lt=2**63)]

_TASK_SUMMARY = """
    SELECT id, user_id, model_slug AS model, status, price, result_url, error, created_at, finished_at
      FROM tasks WHERE user_id = $1 ORDER BY created_at DESC, id LIMIT $2
"""
_TASK = """
    SELECT id, user_id, model_slug AS model, status, price, result_url, error, created_at, finished_at,
           prompt, ref_images
      FROM tasks WHERE id = $1
"""


@router.post(
    "/generate",
    status_code=202,
    dependencies=[RateLimited],
    responses={
        200: {"description": "Idempotency-Key replay: the task created by the first request"},
        400: {"description": "No leg accepts that many reference images"},
        402: {"description": "Not enough tokens"},
        404: {"description": "Unknown model"},
        409: {"description": "Idempotency-Key reused for a different request"},
        429: {"description": "The user already has the maximum number of active tasks (or RATE_LIMIT_PER_MINUTE)"},
    },
)
async def generate(
    body: GenerateIn,
    response: Response,
    services: ServicesDep,
    verified: VerifiedUser,
    idempotency_key: Annotated[IdempotencyKey | None, Header(alias="Idempotency-Key")] = None,
) -> GenerateOut:
    authorize(verified, body.user_id)
    request = engine.GenerateRequest(
        user_id=body.user_id,
        model_slug=body.model_slug,
        prompt=body.prompt,
        ref_images=tuple(str(url) for url in body.ref_images),
        idempotency_key=idempotency_key,
    )
    async with services.pool.acquire() as conn:
        try:
            created = await engine.create_task(conn, request, services.settings.max_active_tasks)
        except engine.GenerateError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        if not created.created:
            response.status_code = 200
            status = await conn.fetchval("SELECT status FROM tasks WHERE id = $1", created.task_id)
            return GenerateOut(task_id=created.task_id, status=status)
    services.background.wake()
    return GenerateOut(task_id=created.task_id, status=TaskStatus.IN_PROGRESS)


@router.get("/tasks/{task_id}")
async def get_task(task_id: UUID, services: ServicesDep, verified: VerifiedUser) -> TaskOut:
    async with services.pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        task = await conn.fetchrow(_TASK, task_id)
        if task is None or (verified is not None and task["user_id"] != verified):
            raise HTTPException(404, "task not found")
        attempts = await conn.fetch(
            """SELECT id, number, provider, leg_priority, status, error, reason, external_id, result_url,
                      created_at, sent_at, deadline_at, finished_at
                 FROM attempts WHERE task_id = $1 ORDER BY number""",
            task_id,
        )
        events = await conn.fetch(
            "SELECT kind, attempt_id, detail, created_at FROM task_events WHERE task_id = $1 ORDER BY id",
            task_id,
        )
    return TaskOut(
        **dict(task),
        current_leg=_current_leg(attempts),
        attempts=[AttemptOut(**dict(a)) for a in attempts],
        events=[EventOut(**dict(e)) for e in events],
    )


def _current_leg(attempts: list[asyncpg.Record]) -> CurrentLeg | None:
    """The live attempt; for a finished task, the last one tried."""
    if not attempts:
        return None
    live = [a for a in attempts if a["status"] in LIVE_ATTEMPT_STATUSES]
    attempt = live[0] if live else attempts[-1]
    return CurrentLeg(
        attempt=attempt["number"],
        priority=attempt["leg_priority"],
        provider=attempt["provider"],
        status=attempt["status"],
    )


@router.get("/users/{user_id}/balance")
async def get_balance(
    user_id: UserIdInPath,
    services: ServicesDep,
    verified: VerifiedUser,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> BalanceOut:
    authorize(verified, user_id)
    async with services.pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        balance = await conn.fetchval("SELECT balance FROM users WHERE id = $1", user_id)
        rows = await conn.fetch(
            """SELECT id, kind, amount, balance_after, task_id, payment_id, created_at
                 FROM ledger WHERE user_id = $1 ORDER BY id DESC LIMIT $2""",
            user_id, limit or services.settings.ledger_page_size,
        )  # fmt: skip
    # No registration: a user we have never seen simply has nothing yet.
    return BalanceOut(user_id=user_id, balance=balance or 0, ledger=[LedgerRow(**dict(r)) for r in rows])


@router.get("/users/{user_id}/tasks")
async def list_user_tasks(
    user_id: UserIdInPath,
    services: ServicesDep,
    verified: VerifiedUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[TaskSummary]:
    authorize(verified, user_id)
    rows = await services.pool.fetch(
        _TASK_SUMMARY,
        user_id, limit,
    )  # fmt: skip
    return [TaskSummary(**dict(r)) for r in rows]


@router.get("/models")
async def list_models(services: ServicesDep) -> list[ModelOut]:
    async with services.pool.acquire() as conn:
        models = await catalog.list_models(conn)
    return [ModelOut(slug=m.slug, price=m.price, legs=[LegOut(**asdict(leg)) for leg in m.legs]) for m in models]


@router.get("/health")
async def health(services: ServicesDep) -> HealthOut:
    await services.pool.fetchval("SELECT 1")
    return HealthOut(status="ok")


@router.get("/health/invariants")
async def invariants(services: ServicesDep) -> dict[str, int]:
    """Rule 8 as a live check: users whose balance differs from the sum of their ledger (must be 0).
    Public and a full scan of the ledger, so each worker recomputes it at most every few seconds."""
    return {"balance_drift": await services.balance_drift.get()}
