"""Read endpoints: task history, balance with ledger, catalog, health."""

import uuid

import pytest

from tests.integration.conftest import Harness


async def test_task_history_lists_attempts_and_events_in_order(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=10)
    task_id = await harness.start_task(user_id)
    await harness.fail("alpha", (await harness.live_attempt(task_id))["id"], error="moderation")
    await harness.dispatch()

    task = await harness.task(task_id)

    assert [a["number"] for a in task["attempts"]] == [1, 2]
    assert task["current_leg"] == {"attempt": 2, "priority": 2, "provider": "beta", "status": "sent"}
    first = task["attempts"][0]
    assert (first["provider"], first["error"], first["reason"]) == ("alpha", "moderation", "webhook")
    assert first["created_at"] <= first["sent_at"] <= first["finished_at"]
    kinds = [event["kind"] for event in task["events"]]
    assert kinds == [
        "created",
        "charged",
        "attempt_sent",
        "attempt_failed",
        "attempt_created",
        "attempt_sent",
    ]


async def test_unknown_task_is_404(harness: Harness) -> None:
    assert (await harness.client.get(f"/tasks/{uuid.uuid4()}")).status_code == 404
    assert (await harness.client.get("/tasks/not-a-uuid")).status_code == 422


async def test_balance_shows_latest_ledger_rows_first_with_running_balance(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=10)
    await harness.generate(user_id)

    balance = await harness.balance(user_id)

    assert balance["balance"] == 8
    rows = [(row["kind"], row["amount"], row["balance_after"]) for row in balance["ledger"]]
    assert rows == [("charge", -2, 8), ("payment", 10, 10)]


@pytest.mark.parametrize(("limit", "expected"), [(1, 1), (2, 2), (500, None)])
async def test_balance_ledger_limit(harness: Harness, limit: int, expected: int | None) -> None:
    user_id = await harness.new_user(tokens=10)
    await harness.generate(user_id)
    response = await harness.client.get(f"/users/{user_id}/balance", params={"limit": limit})
    if expected is None:
        assert response.status_code == 422
    else:
        assert len(response.json()["ledger"]) == expected


async def test_user_task_list_newest_first(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=10)
    first = (await harness.generate(user_id)).json()["task_id"]
    second = (await harness.generate(user_id)).json()["task_id"]

    response = await harness.client.get(f"/users/{user_id}/tasks")

    assert [task["id"] for task in response.json()] == [second, first]


async def test_models_catalog(harness: Harness) -> None:
    models = (await harness.client.get("/models")).json()
    assert [(m["slug"], m["price"], [leg["provider"] for leg in m["legs"]]) for m in models] == [
        ("image-fast", 2, ["alpha", "beta"]),
        ("video-long", 25, ["beta", "alpha"]),
    ]


async def test_health_reports_database_and_invariants(harness: Harness) -> None:
    assert (await harness.client.get("/health")).json() == {"status": "ok"}
    assert (await harness.client.get("/health/invariants")).json() == {"balance_drift": 0}


async def test_security_headers_are_set(harness: Harness) -> None:
    headers = (await harness.client.get("/health")).headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in headers["content-security-policy"]
