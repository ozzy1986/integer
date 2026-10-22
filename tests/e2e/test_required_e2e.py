"""The four required scenarios against the real stack: 4 uvicorn workers, real fake providers, real network.

Here concurrency really crosses process boundaries, and the watchdog runs with the real PROVIDER_TIMEOUT.
"""

import asyncio
from typing import Any

import pytest

from tests.e2e.conftest import BASE_URL, Api, new_user_id, provider_secret, provider_timeout

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not BASE_URL, reason="E2E_BASE_URL is not set: no stack to test")]


def live(task: dict[str, Any], provider: str) -> bool:
    return any(a["provider"] == provider and a["status"] == "sent" for a in task["attempts"])


async def test_1_twenty_concurrent_payment_webhooks_credit_once(api: Api) -> None:
    user_id = new_user_id()
    payment_id = f"e2e-same-{user_id}"

    responses = await asyncio.gather(*(api.pay(user_id, 50, payment_id=payment_id) for _ in range(20)))

    assert [r.status_code for r in responses] == [200] * 20
    assert sum(r.json()["credited"] for r in responses) == 1
    balance = await api.balance(user_id)
    assert (balance["balance"], [row["kind"] for row in balance["ledger"]]) == (50, ["payment"])


async def test_2_ten_concurrent_failures_on_the_last_leg_refund_once(api: Api) -> None:
    user_id = new_user_id()
    await api.pay(user_id, 10)
    # alpha fails fast; beta accepts and stays silent so the test can fire the failures itself.
    task_id = await api.generate(user_id, "e2e @alpha:fail @alpha:delay=0.5 @beta:drop")
    task = await api.wait_task(task_id, lambda t: live(t, "beta"), within=30)
    beta = next(a for a in task["attempts"] if a["provider"] == "beta")
    failure = {"attempt_id": beta["id"], "external_id": beta["external_id"], "status": "failed",
               "error_type": "technical"}  # fmt: skip

    responses = await asyncio.gather(
        *(api.signed("/webhooks/providers/beta", failure, provider_secret("beta")) for _ in range(10))
    )

    assert sorted(r.json()["outcome"] for r in responses) == ["ignored"] * 9 + ["task_failed"]
    balance = await api.balance(user_id)
    assert balance["balance"] == 10
    assert sorted(row["kind"] for row in balance["ledger"]) == ["charge", "payment", "refund"]


async def test_2b_ten_duplicate_failed_webhooks_from_the_provider_refund_once(api: Api) -> None:
    user_id = new_user_id()
    await api.pay(user_id, 10)
    task_id = await api.generate(user_id, "e2e @alpha:fail @alpha:delay=0.5 @beta:fail @beta:delay=0.5 @beta:dup=10")

    await api.wait_task(task_id, lambda t: t["status"] == "failed", within=30)
    await asyncio.sleep(1)  # let every duplicate land

    balance = await api.balance(user_id)
    assert balance["balance"] == 10
    assert sorted(row["kind"] for row in balance["ledger"]) == ["charge", "payment", "refund"]


async def test_3_first_leg_fails_second_completes(api: Api) -> None:
    user_id = new_user_id()
    await api.pay(user_id, 10)
    task_id = await api.generate(user_id, "e2e @alpha:fail @alpha:delay=0.5 @beta:delay=0.5")

    task = await api.wait_task(task_id, lambda t: t["status"] != "in_progress", within=30)

    assert task["status"] == "succeeded"
    assert [(a["provider"], a["status"]) for a in task["attempts"]] == [("alpha", "failed"), ("beta", "succeeded")]
    assert all(a["created_at"] and a["finished_at"] for a in task["attempts"])
    assert (await api.balance(user_id))["balance"] == 8


async def test_4_late_completed_after_the_watchdog(api: Api) -> None:
    """Both variants of rule 6 run in parallel to keep the wall time near 2 x PROVIDER_TIMEOUT."""
    timeout = provider_timeout()
    user_a, user_b = new_user_id(), new_user_id()
    await asyncio.gather(api.pay(user_a, 10), api.pay(user_b, 10))

    # a) alpha answers at T+15, after the watchdog (T) but while beta (busy until 2T) still runs.
    task_a = await api.generate(user_a, f"e2e @alpha:late={timeout + 15:g} @beta:delay={timeout:g}")
    # b) alpha answers at 2T+20, after beta was also given up on at 2T and the money returned.
    task_b = await api.generate(user_b, f"e2e @alpha:late={2 * timeout + 20:g} @beta:drop")

    def has_event(kind: str) -> Any:
        return lambda t: kind in [e["kind"] for e in t["events"]]

    a, b = await asyncio.gather(
        api.wait_task(task_a, lambda t: t["status"] == "succeeded", within=timeout * 2 + 30),
        api.wait_task(task_b, has_event("late_result_ignored"), within=timeout * 3 + 30),
    )

    assert a["result_url"].startswith("https://cdn.alpha.example/")
    assert [(x["provider"], x["status"], x["reason"]) for x in a["attempts"]] == [
        ("alpha", "succeeded", "watchdog_timeout"),
        ("beta", "superseded", None),
    ]
    assert (await api.balance(user_a))["balance"] == 8

    assert (b["status"], b["result_url"]) == ("failed", None)
    assert [x["reason"] for x in b["attempts"]] == ["watchdog_timeout", "watchdog_timeout"]
    assert (await api.balance(user_b))["balance"] == 10
