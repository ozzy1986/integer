"""Background work that runs in every worker process: the dispatcher and the watchdog.

Neither keeps state in memory. Work is claimed in the database with FOR UPDATE SKIP LOCKED, so any
number of processes can run both loops without duplicating work (rule 7).
"""

import asyncio
import contextlib
import logging
from uuid import UUID

import asyncpg

from app import engine
from app.providers import ProviderGateway, Submission, SubmitFailed

log = logging.getLogger(__name__)

BATCH_SIZE = 20


class Dispatcher:
    """Sends pending attempts to providers.

    An attempt is claimed by setting a lease. If the process dies before recording the outcome, the
    lease expires and another worker resends it; the provider deduplicates by attempt_id. This is
    at-least-once delivery to an idempotent receiver, which is what exactly-once means over a network.
    """

    def __init__(
        self, pool: asyncpg.Pool, gateway: ProviderGateway, lease_seconds: float, provider_timeout: float
    ) -> None:
        self._pool = pool
        self._gateway = gateway
        self._lease_seconds = lease_seconds
        self._provider_timeout = provider_timeout

    async def run_once(self) -> int:
        rows = await self._pool.fetch(
            """UPDATE attempts a SET lease_until = now() + make_interval(secs => $2)
                 FROM tasks t
                WHERE t.id = a.task_id
                  AND a.id IN (SELECT id FROM attempts
                                WHERE status = 'pending' AND (lease_until IS NULL OR lease_until < now())
                                ORDER BY created_at LIMIT $1
                                  FOR UPDATE SKIP LOCKED)
            RETURNING a.id, a.provider, t.prompt, t.ref_images""",
            BATCH_SIZE, self._lease_seconds,
        )  # fmt: skip
        submissions = [Submission(r["id"], r["provider"], r["prompt"], list(r["ref_images"])) for r in rows]
        await asyncio.gather(*(self._send(s) for s in submissions))
        return len(submissions)

    async def _send(self, submission: Submission) -> None:
        try:
            external_id = await self._gateway.submit(submission)
        except SubmitFailed as failure:
            log.warning("attempt %s to %s failed to send: %s", submission.attempt_id, submission.provider, failure)
            async with self._pool.acquire() as conn:
                await engine.fail_attempt(conn, submission.attempt_id, failure.error, failure.reason)
            return
        async with self._pool.acquire() as conn:
            await engine.mark_sent(conn, submission.attempt_id, external_id, self._provider_timeout)


class Watchdog:
    """Fails attempts whose provider stayed silent past PROVIDER_TIMEOUT (rule 5)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def run_once(self) -> int:
        overdue: list[UUID] = [
            row["id"]
            for row in await self._pool.fetch(
                "SELECT id FROM attempts WHERE status = 'sent' AND deadline_at < now() ORDER BY deadline_at LIMIT $1",
                BATCH_SIZE,
            )
        ]
        expired = 0
        for attempt_id in overdue:
            async with self._pool.acquire() as conn:
                if await engine.expire_attempt(conn, attempt_id) is not engine.Outcome.IGNORED:
                    expired += 1
        return expired


class BackgroundLoop:
    """Runs the dispatcher and the watchdog every `interval` seconds, or sooner when woken up."""

    def __init__(self, dispatcher: Dispatcher, watchdog: Watchdog, interval: float) -> None:
        self._dispatcher = dispatcher
        self._watchdog = watchdog
        self._interval = interval
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def wake(self) -> None:
        """New work was committed in this process: dispatch it now instead of on the next tick."""
        self._wakeup.set()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="background-loop")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while True:
            self._wakeup.clear()
            try:
                await self._dispatcher.run_once()
                await self._watchdog.run_once()
            except Exception:
                # The loop must survive a database blip; the next tick retries. Nothing is lost:
                # all state is in the database.
                log.exception("background loop iteration failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), timeout=self._interval)
