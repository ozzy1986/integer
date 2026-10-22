"""POST /generate: validation, error codes, races on balance and on the active-task limit, idempotency."""

import asyncio
from typing import Any

import pytest

from tests.conftest import new_user_id
from tests.integration.conftest import Harness


async def test_generate_charges_the_price_and_returns_a_task(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=10)

    response = await harness.generate(user_id)

    assert response.status_code == 202
    task = await harness.task(response.json()["task_id"])
    assert (task["status"], task["model"], task["price"], task["user_id"]) == (
        "in_progress",
        "image-fast",
        2,
        user_id,
    )
    assert task["current_leg"]["provider"] == "alpha"
    assert (await harness.balance(user_id))["balance"] == 8


async def test_unknown_model_is_404(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=10)
    assert (await harness.generate(user_id, model="no-such-model")).status_code == 404


async def test_no_leg_accepts_that_many_references_is_400_before_charging(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=100)

    response = await harness.generate(user_id, model="image-fast", refs=11)

    assert response.status_code == 400
    assert (await harness.balance(user_id))["balance"] == 100
    assert (await harness.generate(user_id, model="video-long", refs=3)).status_code == 400


async def test_not_enough_tokens_is_402_and_nothing_is_written(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=24)

    response = await harness.generate(user_id, model="video-long")

    assert response.status_code == 402
    balance = await harness.balance(user_id)
    assert (balance["balance"], [r["kind"] for r in balance["ledger"]]) == (24, ["payment"])


async def test_unknown_user_is_a_user_with_zero_balance(harness: Harness) -> None:
    user_id = new_user_id()
    assert (await harness.generate(user_id)).status_code == 402
    assert await harness.balance(user_id) == {"user_id": user_id, "balance": 0, "ledger": []}


async def test_fourth_active_task_is_429(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=100)
    for _ in range(3):
        assert (await harness.generate(user_id)).status_code == 202

    response = await harness.generate(user_id)

    assert response.status_code == 429
    assert (await harness.balance(user_id))["balance"] == 94


async def test_finished_tasks_free_an_active_slot(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=100)
    task_ids = [await harness.start_task(user_id) for _ in range(3)]
    await harness.complete("alpha", (await harness.live_attempt(task_ids[0]))["id"])

    assert (await harness.generate(user_id)).status_code == 202


async def test_concurrent_requests_respect_the_active_task_limit(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=100)

    responses = await asyncio.gather(*(harness.generate(user_id) for _ in range(10)))

    assert sorted(r.status_code for r in responses) == [202] * 3 + [429] * 7
    assert (await harness.balance(user_id))["balance"] == 94


async def test_concurrent_requests_never_overdraw_the_balance(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=5)  # enough for exactly two image-fast tasks

    responses = await asyncio.gather(*(harness.generate(user_id) for _ in range(3)))

    assert sorted(r.status_code for r in responses) == [202, 202, 402]
    assert (await harness.balance(user_id))["balance"] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"prompt": ""},
        {"prompt": "x" * 4001},
        {"prompt": "a\x00b"},  # PostgreSQL text cannot hold NUL: must be 422, not a 500 from the database
        {"model_slug": "Bad Slug!"},
        {"user_id": 0},
        {"user_id": -5},
        {"user_id": 2**63},
        {"user_id": True},  # would otherwise be taken as user 1
        {"ref_images": ["not a url"]},
        {"ref_images": ["javascript:alert(1)"]},
        {"ref_images": ["https://x.example/" + "a" * 2100]},
        {"unexpected": "field"},
    ],
)
async def test_invalid_requests_are_422(harness: Harness, change: dict[str, Any]) -> None:
    user_id = await harness.new_user(tokens=10)
    assert (await harness.generate(user_id, **change)).status_code == 422
    assert (await harness.balance(user_id))["balance"] == 10


class TestIdempotencyKey:
    async def test_retry_with_the_same_key_returns_the_same_task_and_charges_once(self, harness: Harness) -> None:
        user_id = await harness.new_user(tokens=10)
        headers = {"Idempotency-Key": "order-1"}

        first = await harness.generate(user_id, headers=headers)
        second = await harness.generate(user_id, headers=headers)

        assert (first.status_code, second.status_code) == (202, 200)
        assert first.json()["task_id"] == second.json()["task_id"]
        assert (await harness.balance(user_id))["balance"] == 8

    async def test_concurrent_retries_with_the_same_key_create_one_task(self, harness: Harness) -> None:
        user_id = await harness.new_user(tokens=10)

        responses = await asyncio.gather(
            *(harness.generate(user_id, headers={"Idempotency-Key": "k"}) for _ in range(10))
        )

        assert len({r.json()["task_id"] for r in responses}) == 1
        assert (await harness.balance(user_id))["balance"] == 8

    async def test_same_key_with_a_different_request_is_409(self, harness: Harness) -> None:
        user_id = await harness.new_user(tokens=10)
        assert (await harness.generate(user_id, headers={"Idempotency-Key": "k"})).status_code == 202

        response = await harness.generate(user_id, headers={"Idempotency-Key": "k"}, prompt="another")

        assert response.status_code == 409

    async def test_keys_are_scoped_per_user(self, harness: Harness) -> None:
        first, second = await harness.new_user(tokens=10), await harness.new_user(tokens=10)
        a = await harness.generate(first, headers={"Idempotency-Key": "k"})
        b = await harness.generate(second, headers={"Idempotency-Key": "k"})
        assert a.json()["task_id"] != b.json()["task_id"]

    @pytest.mark.parametrize("key", ["", "x" * 129, "bad key with spaces"])
    async def test_malformed_key_is_422(self, harness: Harness, key: str) -> None:
        user_id = await harness.new_user(tokens=10)
        assert (await harness.generate(user_id, headers={"Idempotency-Key": key})).status_code == 422
