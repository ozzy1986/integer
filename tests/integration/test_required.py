"""The four tests the task statement requires, against the real database.

The same scenarios also run end-to-end against the docker compose stack (4 workers, real fake
providers) in tests/e2e/test_required_e2e.py.
"""

import asyncio
from typing import Any

import pytest

from tests.integration.conftest import Harness


async def test_1_twenty_concurrent_identical_payment_webhooks_credit_once(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=0)

    responses = await asyncio.gather(*(harness.pay(user_id, 50, payment_id="pay-same") for _ in range(20)))

    assert [r.status_code for r in responses] == [200] * 20
    assert sum(r.json()["credited"] for r in responses) == 1
    balance = await harness.balance(user_id)
    assert balance["balance"] == 50
    assert [row["kind"] for row in balance["ledger"]] == ["payment"]


async def test_2_ten_concurrent_failures_on_the_last_leg_refund_once(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=10)
    task_id = await harness.start_task(user_id)  # image-fast: alpha -> beta
    alpha = await harness.live_attempt(task_id)
    assert (await harness.fail("alpha", alpha["id"])).status_code == 200
    await harness.dispatch()
    beta = await harness.live_attempt(task_id)
    assert beta["provider"] == "beta"

    responses = await asyncio.gather(*(harness.fail("beta", beta["id"]) for _ in range(10)))

    assert [r.status_code for r in responses] == [200] * 10
    outcomes = sorted(r.json()["outcome"] for r in responses)
    assert outcomes == ["ignored"] * 9 + ["task_failed"]
    task = await harness.task(task_id)
    assert task["status"] == "failed"
    assert (await harness.balance(user_id))["balance"] == 10
    assert await harness.ledger_kinds(user_id) == ["payment", "charge", "refund"]


async def test_3_first_leg_fails_task_completes_on_second_with_two_attempts(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=10)
    task_id = await harness.start_task(user_id)
    await harness.fail("alpha", (await harness.live_attempt(task_id))["id"], error="technical")
    await harness.dispatch()
    beta = await harness.live_attempt(task_id)

    response = await harness.complete("beta", beta["id"], url="https://cdn.example/beta.png")

    assert response.json()["outcome"] == "task_succeeded"
    task = await harness.task(task_id)
    assert task["status"] == "succeeded"
    assert task["result_url"] == "https://cdn.example/beta.png"
    history = [(a["provider"], a["status"], a["error"]) for a in task["attempts"]]
    assert history == [("alpha", "failed", "technical"), ("beta", "succeeded", None)]
    assert all(a["created_at"] for a in task["attempts"])
    assert (await harness.balance(user_id))["balance"] == 8
    assert harness.stub.providers_called() == ["alpha", "beta"]


class TestRule6LateCompleted:
    """Rule 6: a `completed` arriving after the watchdog gave up on that attempt.

    Decision (see README): if the task is still in progress the result is accepted (the user gets
    what they paid for, the newer attempt is superseded); if the task has already failed and been
    refunded, the result is ignored and only recorded in the history. Never both result and refund.
    """

    @staticmethod
    async def timed_out_alpha(harness: Harness) -> tuple[int, str, dict[str, Any]]:
        user_id = await harness.new_user(tokens=10)
        task_id = await harness.start_task(user_id)
        alpha = await harness.live_attempt(task_id)
        await harness.make_overdue(task_id)
        assert await harness.watchdog() == 1
        await harness.dispatch()
        return user_id, task_id, alpha

    async def test_4a_late_result_is_accepted_while_task_is_still_in_progress(self, harness: Harness) -> None:
        user_id, task_id, alpha = await self.timed_out_alpha(harness)
        beta = await harness.live_attempt(task_id)

        response = await harness.complete("alpha", alpha["id"], url="https://cdn.example/late.png")

        assert response.json()["outcome"] == "late_result_accepted"
        task = await harness.task(task_id)
        assert (task["status"], task["result_url"]) == ("succeeded", "https://cdn.example/late.png")
        statuses = {a["provider"]: (a["status"], a["error"], a["reason"]) for a in task["attempts"]}
        # The reason keeps the story (the watchdog gave up first); an error type on a success would not.
        assert statuses == {"alpha": ("succeeded", None, "watchdog_timeout"), "beta": ("superseded", None, None)}
        # The superseded attempt's own result changes nothing.
        assert (await harness.complete("beta", beta["id"])).json()["outcome"] == "ignored"
        assert await harness.ledger_kinds(user_id) == ["payment", "charge"]
        assert (await harness.balance(user_id))["balance"] == 8

    async def test_4b_late_result_after_refund_is_recorded_but_not_delivered(self, harness: Harness) -> None:
        user_id, task_id, alpha = await self.timed_out_alpha(harness)
        await harness.make_overdue(task_id)
        assert await harness.watchdog() == 1  # beta is silent too -> no legs left -> refund
        assert (await harness.task(task_id))["status"] == "failed"

        response = await harness.complete("alpha", alpha["id"], url="https://cdn.example/late.png")

        assert response.json()["outcome"] == "late_result_ignored"
        task = await harness.task(task_id)
        assert (task["status"], task["result_url"]) == ("failed", None)
        assert "late_result_ignored" in [event["kind"] for event in task["events"]]
        assert await harness.ledger_kinds(user_id) == ["payment", "charge", "refund"]
        assert (await harness.balance(user_id))["balance"] == 10


@pytest.mark.parametrize("copies", [2, 10])
async def test_concurrent_duplicate_completed_webhooks_finish_the_task_once(harness: Harness, copies: int) -> None:
    user_id = await harness.new_user(tokens=10)
    task_id = await harness.start_task(user_id)
    alpha = await harness.live_attempt(task_id)

    responses = await asyncio.gather(*(harness.complete("alpha", alpha["id"]) for _ in range(copies)))

    assert sorted(r.json()["outcome"] for r in responses) == ["ignored"] * (copies - 1) + ["task_succeeded"]
    assert await harness.ledger_kinds(user_id) == ["payment", "charge"]
