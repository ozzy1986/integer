"""Background work that runs in every worker process: the dispatcher and the watchdog.

Neither keeps state in memory. Work is claimed in the database with FOR UPDATE SKIP LOCKED, so any
number of processes can run both loops without duplicating work (rule 7).
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

import asyncpg

from app import engine
from app.providers import ProviderGateway, Submission, SubmitFailed

log = logging.getLogger(__name__)

BATCH_SIZE = 20
# Sends one worker keeps in flight at once. Each holds a lease, so this also bounds what a crashed worker
# leaves for the others to resend.
MAX_IN_FLIGHT = 50


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
        self._in_flight: set[asyncio.Task[None]] = set()

    async def run_once(self) -> int:
        """Claim a batch and send it; returns once every send is recorded (tests drive this directly)."""
        submissions = await self._claim(BATCH_SIZE)
        await asyncio.gather(*(self._send(s) for s in submissions))
        return len(submissions)

    async def start_sending(self) -> int:
        """Claim what fits under MAX_IN_FLIGHT and send it in the background. The loop does not wait for
        the sends: a provider that hangs until SEND_TIMEOUT must not hold up anybody else's task."""
        room = MAX_IN_FLIGHT - len(self._in_flight)
        submissions = await self._claim(min(room, BATCH_SIZE)) if room > 0 else []
        for submission in submissions:
            task = asyncio.create_task(self._send(submission), name=f"send-{submission.attempt_id}")
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)
        return len(submissions)

    async def stop(self) -> None:
        """Cancel sends still in flight: their leases expire and another worker resends them."""
        for task in self._in_flight:
            task.cancel()
        await asyncio.gather(*self._in_flight, return_exceptions=True)

    async def _claim(self, limit: int) -> list[Submission]:
        rows = await self._pool.fetch(
            """UPDATE attempts a SET lease_until = now() + make_interval(secs => $2)
                 FROM tasks t
                WHERE t.id = a.task_id
                  AND a.id IN (SELECT id FROM attempts
                                WHERE status = 'pending' AND (lease_until IS NULL OR lease_until < now())
                                ORDER BY created_at LIMIT $1
                                  FOR UPDATE SKIP LOCKED)
            RETURNING a.id, a.provider, t.prompt, t.ref_images""",
            limit, self._lease_seconds,
        )  # fmt: skip
        return [Submission(r["id"], r["provider"], r["prompt"], list(r["ref_images"])) for r in rows]

    async def _send(self, submission: Submission) -> None:
        try:
            try:
                external_id = await self._gateway.submit(submission)
            except SubmitFailed as failure:
                log.warning("attempt %s to %s failed to send: %s", submission.attempt_id, submission.provider, failure)
                async with self._pool.acquire() as conn:
                    await engine.fail_attempt(conn, submission.attempt_id, failure.error, failure.reason)
                return
            async with self._pool.acquire() as conn:
                await engine.mark_sent(conn, submission.attempt_id, external_id, self._provider_timeout)
        except Exception:
            # Recording the outcome failed (e.g. a database blip). Nothing was committed, so the lease
            # expires and the attempt is resent; one bad send must not take the others down with it.
            log.exception("attempt %s: could not record the send, it will be retried", submission.attempt_id)


class Watchdog:
    """Fails attempts whose provider stayed silent past PROVIDER_TIMEOUT (rule 5)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def run_once(self) -> int:
        """Expire overdue attempts, one transaction each, until none is left that nobody else is busy with."""
        expired = 0
        while True:
            async with self._pool.acquire() as conn:
                outcome = await engine.expire_next_overdue(conn)
            if outcome is None:
                return expired
            expired += outcome is not engine.Outcome.IGNORED


class BackgroundLoop:
    """Runs the dispatcher and the watchdog as two independent loops, each every `interval` seconds; the
    dispatcher sooner when woken up. Independent, so a slow send can never delay the watchdog."""

    def __init__(self, dispatcher: Dispatcher, watchdog: Watchdog, interval: float) -> None:
        self._dispatcher = dispatcher
        self._watchdog = watchdog
        self._interval = interval
        self._wakeup = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    def wake(self) -> None:
        """New work was committed in this process: dispatch it now instead of on the next tick."""
        self._wakeup.set()

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._repeat("dispatcher", self._dispatcher.start_sending, self._wakeup)),
            asyncio.create_task(self._repeat("watchdog", self._watchdog.run_once, asyncio.Event())),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._dispatcher.stop()

    async def _repeat(self, name: str, step: Callable[[], Awaitable[int]], wakeup: asyncio.Event) -> None:
        while True:
            wakeup.clear()
            try:
                await step()
            except Exception:
                # The loop must survive a database blip; the next tick retries. Nothing is lost:
                # all state is in the database.
                log.exception("%s iteration failed", name)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wakeup.wait(), timeout=self._interval)
