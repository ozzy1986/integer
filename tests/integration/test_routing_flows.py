"""Rules 2-5 end to end through the engine: moderation, payload, sync errors, watchdog, races."""

import asyncio
from typing import Any

import httpx
import pytest

from app import engine
from tests.integration.conftest import Harness, raises, status


async def test_moderation_failure_skips_legs_marked_skip_on_moderation(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=30)
    task_id = await harness.start_task(user_id, model="video-long")  # beta -> alpha(skip_on_moderation)

    response = await harness.fail("beta", (await harness.live_attempt(task_id))["id"], error="moderation")

    assert response.json()["outcome"] == "task_failed"
    task = await harness.task(task_id)
    assert (task["status"], task["error"], len(task["attempts"])) == ("failed", "moderation", 1)
    assert harness.stub.providers_called() == ["beta"]
    assert (await harness.balance(user_id))["balance"] == 30


async def test_technical_failure_on_video_long_falls_back_to_alpha(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=30)
    task_id = await harness.start_task(user_id, model="video-long", refs=1)
    await harness.fail("beta", (await harness.live_attempt(task_id))["id"], error="technical")
    await harness.dispatch()
    assert harness.stub.providers_called() == ["beta", "alpha"]


async def test_moderation_failure_moves_on_when_next_leg_does_not_skip(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=10)
    task_id = await harness.start_task(user_id)  # image-fast: no skip flags
    response = await harness.fail("alpha", (await harness.live_attempt(task_id))["id"], error="moderation")
    assert response.json()["outcome"] == "next_attempt"
    assert (await harness.live_attempt(task_id))["provider"] == "beta"


async def test_payload_failure_fails_the_task_immediately_with_a_refund(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=10)
    task_id = await harness.start_task(user_id)

    response = await harness.fail("alpha", (await harness.live_attempt(task_id))["id"], error="payload")

    assert response.json()["outcome"] == "task_failed"
    task = await harness.task(task_id)
    assert (task["status"], task["error"], len(task["attempts"])) == ("failed", "payload", 1)
    assert await harness.ledger_kinds(user_id) == ["payment", "charge", "refund"]


async def test_fallback_leg_must_accept_the_references(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=30)
    task_id = await harness.start_task(user_id, model="video-long", refs=2)  # alpha takes only 1

    response = await harness.fail("beta", (await harness.live_attempt(task_id))["id"])

    assert response.json()["outcome"] == "task_failed"


async def test_alpha_is_skipped_when_it_cannot_take_the_references(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=10)
    await harness.start_task(user_id, refs=5)
    assert harness.stub.providers_called() == ["beta"]


class TestSynchronousErrors:
    """Rule 3: a 5xx, timeout or connection error on POST /tasks is a technical failure of the leg."""

    async def test_503_moves_to_the_next_leg(self, harness: Harness) -> None:
        harness.stub.script("alpha", status(503))
        user_id = await harness.new_user(tokens=10)
        task_id = await harness.start_task(user_id)
        await harness.dispatch()

        task = await harness.task(task_id)
        assert [(a["provider"], a["status"], a["error"], a["reason"]) for a in task["attempts"]] == [
            ("alpha", "failed", "technical", "http_503"),
            ("beta", "sent", None, None),
        ]

    async def test_timeout_moves_to_the_next_leg(self, harness: Harness) -> None:
        harness.stub.script("alpha", raises(httpx.ReadTimeout))
        user_id = await harness.new_user(tokens=10)
        task_id = await harness.start_task(user_id)
        await harness.dispatch()
        assert [a["reason"] for a in (await harness.task(task_id))["attempts"]] == ["send_timeout", None]

    async def test_connection_error_moves_to_the_next_leg(self, harness: Harness) -> None:
        harness.stub.script("alpha", raises(httpx.ConnectError))
        user_id = await harness.new_user(tokens=10)
        task_id = await harness.start_task(user_id)
        await harness.dispatch()
        assert [a["reason"] for a in (await harness.task(task_id))["attempts"]] == ["connect_error", None]

    async def test_sync_failure_on_every_leg_refunds(self, harness: Harness) -> None:
        harness.stub.script("alpha", status(500))
        harness.stub.script("beta", status(502))
        user_id = await harness.new_user(tokens=10)
        task_id = await harness.start_task(user_id)
        await harness.dispatch()

        assert (await harness.task(task_id))["status"] == "failed"
        assert await harness.ledger_kinds(user_id) == ["payment", "charge", "refund"]

    @pytest.mark.parametrize("code", [400, 413, 415, 422])
    async def test_4xx_about_the_request_itself_means_payload(self, harness: Harness, code: int) -> None:
        harness.stub.script("alpha", status(code))
        user_id = await harness.new_user(tokens=10)
        task_id = await harness.start_task(user_id)

        task = await harness.task(task_id)
        assert (task["status"], task["error"], task["attempts"][0]["reason"]) == (
            "failed",
            "payload",
            f"http_{code}",
        )
        assert harness.stub.providers_called() == ["alpha"]

    @pytest.mark.parametrize("code", [401, 403, 404, 408, 409, 429])
    async def test_other_4xx_is_the_providers_trouble_and_moves_on(self, harness: Harness, code: int) -> None:
        # Rate limiting, a timeout or our misconfigured credentials for one provider say nothing about the
        # request: another leg may well take it.
        harness.stub.script("alpha", status(code))
        user_id = await harness.new_user(tokens=10)
        task_id = await harness.start_task(user_id)
        await harness.dispatch()

        task = await harness.task(task_id)
        assert [(a["provider"], a["status"], a["error"], a["reason"]) for a in task["attempts"]] == [
            ("alpha", "failed", "technical", f"http_{code}"),
            ("beta", "sent", None, None),
        ]

    async def test_202_without_external_id_is_a_technical_failure(self, harness: Harness) -> None:
        harness.stub.script("alpha", lambda request: httpx.Response(202, json={}))
        user_id = await harness.new_user(tokens=10)
        task_id = await harness.start_task(user_id)
        assert (await harness.task(task_id))["attempts"][0]["reason"] == "bad_response"

    async def test_202_with_an_unprintable_external_id_is_a_technical_failure(self, harness: Harness) -> None:
        # A NUL could not even be stored: the attempt would stay pending and be resent forever.
        harness.stub.script("alpha", lambda request: httpx.Response(202, json={"external_id": "x\x00y"}))
        user_id = await harness.new_user(tokens=10)
        task_id = await harness.start_task(user_id)
        await harness.dispatch()
        attempts = (await harness.task(task_id))["attempts"]
        assert [(a["provider"], a["status"], a["reason"]) for a in attempts] == [
            ("alpha", "failed", "bad_response"),
            ("beta", "sent", None),
        ]


class TestWatchdog:
    async def test_silent_provider_is_treated_as_a_technical_failure(self, harness: Harness) -> None:
        user_id = await harness.new_user(tokens=10)
        task_id = await harness.start_task(user_id)
        await harness.make_overdue(task_id)

        assert await harness.watchdog() == 1
        await harness.dispatch()

        attempts = (await harness.task(task_id))["attempts"]
        assert [(a["provider"], a["status"], a["error"], a["reason"]) for a in attempts] == [
            ("alpha", "failed", "technical", "watchdog_timeout"),
            ("beta", "sent", None, None),
        ]

    async def test_attempts_within_the_deadline_are_left_alone(self, harness: Harness) -> None:
        user_id = await harness.new_user(tokens=10)
        await harness.start_task(user_id)
        assert await harness.watchdog() == 0

    async def test_concurrent_watchdogs_expire_an_attempt_once(self, harness: Harness) -> None:
        # Rule 7: every worker runs a watchdog; the work must not be duplicated.
        user_id = await harness.new_user(tokens=10)
        task_id = await harness.start_task(user_id)
        await harness.make_overdue(task_id)

        expired = await asyncio.gather(*(harness.watchdog() for _ in range(8)))

        assert sum(expired) == 1
        assert len((await harness.task(task_id))["attempts"]) == 2

    async def test_failure_webhook_racing_the_watchdog_counts_once(self, harness: Harness) -> None:
        user_id = await harness.new_user(tokens=10)
        task_id = await harness.start_task(user_id)
        alpha = await harness.live_attempt(task_id)
        await harness.make_overdue(task_id)

        await asyncio.gather(harness.watchdog(), harness.fail("alpha", alpha["id"]), harness.watchdog())

        attempts = (await harness.task(task_id))["attempts"]
        assert [(a["provider"], a["status"]) for a in attempts] == [("alpha", "failed"), ("beta", "pending")]


class TestDispatcher:
    async def test_concurrent_dispatchers_send_each_attempt_once(self, harness: Harness) -> None:
        user_id = await harness.new_user(tokens=100)
        for _ in range(3):
            assert (await harness.generate(user_id)).status_code == 202

        sent = await asyncio.gather(*(harness.dispatch() for _ in range(6)))

        assert sum(sent) == 3
        assert len(harness.stub.calls) == 3

    async def test_attempt_of_a_crashed_dispatcher_is_resent_after_the_lease_expires(self, harness: Harness) -> None:
        user_id = await harness.new_user(tokens=10)
        task_id = (await harness.generate(user_id)).json()["task_id"]
        async with harness.services.pool.acquire() as conn:  # claimed, then the process died before sending
            await conn.execute(
                "UPDATE attempts SET lease_until = now() + interval '1 hour' WHERE task_id = $1::uuid",
                task_id,
            )
        assert await harness.dispatch() == 0

        async with harness.services.pool.acquire() as conn:
            await conn.execute(
                "UPDATE attempts SET lease_until = now() - interval '1 second' WHERE task_id = $1::uuid",
                task_id,
            )
        assert await harness.dispatch() == 1
        assert (await harness.live_attempt(task_id))["status"] == "sent"

    async def test_failed_bookkeeping_of_one_send_is_retried_after_the_lease(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The provider took the job, but recording it failed (say, the database blipped): the dispatcher
        # must survive it, and the lease brings the attempt back; the provider deduplicates the resend.
        user_id = await harness.new_user(tokens=10)
        task_id = (await harness.generate(user_id)).json()["task_id"]
        real_mark_sent = engine.mark_sent

        async def broken_once(*args: Any, **kwargs: Any) -> engine.Outcome:
            monkeypatch.setattr(engine, "mark_sent", real_mark_sent)
            raise ConnectionResetError("database went away")

        monkeypatch.setattr(engine, "mark_sent", broken_once)

        await harness.dispatch()  # must not blow up the loop

        assert (await harness.live_attempt(task_id))["status"] == "pending"
        async with harness.services.pool.acquire() as conn:
            await conn.execute(
                "UPDATE attempts SET lease_until = now() - interval '1 second' WHERE task_id = $1::uuid", task_id
            )
        assert await harness.dispatch() == 1
        assert (await harness.live_attempt(task_id))["status"] == "sent"

    async def test_provider_receives_attempt_id_as_idempotency_key(self, harness: Harness) -> None:
        user_id = await harness.new_user(tokens=10)
        task_id = await harness.start_task(user_id, refs=2)
        provider, payload = harness.stub.calls[0]
        attempt = await harness.live_attempt(task_id)
        assert provider == "alpha"
        assert payload == {
            "attempt_id": attempt["id"],
            "prompt": "a cat in a hat",
            "ref_images": ["https://img.example/0.png", "https://img.example/1.png"],
        }
        assert attempt["external_id"]

    async def test_webhook_that_beats_the_202_is_not_lost(self, harness: Harness) -> None:
        user_id = await harness.new_user(tokens=10)
        task_id = (await harness.generate(user_id)).json()["task_id"]
        attempt = await harness.live_attempt(task_id)
        assert attempt["status"] == "pending"

        await harness.complete("alpha", attempt["id"])  # provider answered before we recorded the 202
        await harness.dispatch()  # stale claim must not resurrect the attempt

        task = await harness.task(task_id)
        assert task["status"] == "succeeded"
        assert [a["status"] for a in task["attempts"]] == ["succeeded"]

    async def test_completed_after_sync_timeout_is_accepted_while_task_runs(self, harness: Harness) -> None:
        # A timeout does not mean the provider did not take the job: its result may still come.
        harness.stub.script("alpha", raises(httpx.ReadTimeout))
        user_id = await harness.new_user(tokens=10)
        task_id = await harness.start_task(user_id)
        alpha_id = (await harness.task(task_id))["attempts"][0]["id"]

        response = await harness.complete("alpha", alpha_id)

        assert response.json()["outcome"] == "late_result_accepted"
        assert [a["status"] for a in (await harness.task(task_id))["attempts"]] == ["succeeded", "superseded"]
