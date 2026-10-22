"""The service wired to the real fake providers in one process: no stubs, real signed webhooks both ways."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.main import create_app
from fake_providers.app import FakeSettings, World, create_fake_app
from tests.conftest import new_user_id
from tests.integration.conftest import PAYMENTS_SECRET, PROVIDER_SECRETS, make_settings


class Forward(httpx.AsyncBaseTransport):
    """Breaks the chicken-and-egg: the service needs the fake's transport and vice versa."""

    target: httpx.AsyncBaseTransport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.target.handle_async_request(request)


class Stack:
    def __init__(self, client: httpx.AsyncClient, fake_client: httpx.AsyncClient, app: Any, world: World) -> None:
        self.client = client
        self.fake = fake_client
        self.app = app
        self.world = world

    async def settle(self, rounds: int = 6) -> None:
        """Run dispatcher/watchdog and let the fakes deliver until nothing moves."""
        for _ in range(rounds):
            await self.app.state.services.dispatcher.run_once()
            await self.app.state.services.watchdog.run_once()
            await self.world.drain()

    async def topup(self, user_id: int, tokens: int, copies: int = 1) -> dict[str, Any]:
        response = await self.fake.post("/payments/pay", json={"user_id": user_id, "tokens": tokens, "copies": copies})
        result: dict[str, Any] = response.json()
        return result

    async def generate(self, user_id: int, prompt: str, model: str = "image-fast") -> str:
        response = await self.client.post("/generate", json={"user_id": user_id, "model_slug": model, "prompt": prompt})
        assert response.status_code == 202, response.text
        task_id: str = response.json()["task_id"]
        return task_id

    async def task(self, task_id: str) -> dict[str, Any]:
        result: dict[str, Any] = (await self.client.get(f"/tasks/{task_id}")).json()
        return result

    async def balance(self, user_id: int) -> int:
        result: int = (await self.client.get(f"/users/{user_id}/balance")).json()["balance"]
        return result


@pytest.fixture
async def stack(migrated_db: str) -> AsyncIterator[Stack]:
    to_fake = Forward()
    service = create_app(
        make_settings(
            migrated_db,
            provider_urls={name: f"http://fake.test/{name}" for name in PROVIDER_SECRETS},
            provider_timeout=0.3,
            send_timeout=0.5,
            demo_payments_url="http://fake.test/payments",
            demo_max_topup=100,
        ),
        provider_transport=to_fake,
    )
    fake = create_fake_app(
        FakeSettings(
            service_url="http://service.test",
            provider_secrets=PROVIDER_SECRETS,
            payments_secret=PAYMENTS_SECRET,
            default_behaviour={"delay_min": 0, "delay_max": 0, "late_delay": 0.6, "sync_timeout_seconds": 1},
            retry_backoff=0,
        ),
        service_transport=httpx.ASGITransport(app=service),
    )
    to_fake.target = httpx.ASGITransport(app=fake)
    async with service.router.lifespan_context(service), fake.router.lifespan_context(fake):
        services = service.state.services
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=service), base_url="http://service.test") as client,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url="http://fake.test") as fake_client,
        ):
            yield Stack(client, fake_client, service, fake.state.world)
        await services.pool.execute(
            "UPDATE attempts SET status = 'superseded', finished_at = now() WHERE status IN ('pending', 'sent')"
        )


async def test_happy_path(stack: Stack) -> None:
    user_id = new_user_id()
    await stack.topup(user_id, 10)
    task_id = await stack.generate(user_id, "a cat")
    await stack.settle()

    task = await stack.task(task_id)
    assert task["status"] == "succeeded"
    assert task["result_url"].startswith("https://cdn.alpha.example/")
    assert await stack.balance(user_id) == 8


async def test_twenty_duplicate_payment_webhooks_credit_once(stack: Stack) -> None:
    user_id = new_user_id()
    result = await stack.topup(user_id, 30, copies=20)
    assert result["delivered"] == [200] * 20
    assert await stack.balance(user_id) == 30


async def test_first_leg_fails_second_completes(stack: Stack) -> None:
    user_id = new_user_id()
    await stack.topup(user_id, 10)
    task_id = await stack.generate(user_id, "a cat @alpha:fail=technical")
    await stack.settle()

    task = await stack.task(task_id)
    assert task["status"] == "succeeded"
    assert [(a["provider"], a["status"]) for a in task["attempts"]] == [("alpha", "failed"), ("beta", "succeeded")]


async def test_ten_duplicate_failures_on_last_leg_refund_once(stack: Stack) -> None:
    user_id = new_user_id()
    await stack.topup(user_id, 10)
    task_id = await stack.generate(user_id, "@alpha:fail @beta:fail @beta:dup=10")
    await stack.settle()

    assert (await stack.task(task_id))["status"] == "failed"
    assert await stack.balance(user_id) == 10


async def test_sync_503_and_sync_timeout_move_to_the_next_leg(stack: Stack) -> None:
    user_id = new_user_id()
    await stack.topup(user_id, 10)
    first = await stack.generate(user_id, "@alpha:sync=503")
    second = await stack.generate(user_id, "@alpha:sync=timeout")
    await stack.settle()

    for task_id, reason in ((first, "http_503"), (second, "send_timeout")):
        task = await stack.task(task_id)
        assert task["attempts"][0]["reason"] == reason
        assert task["status"] == "succeeded"


async def test_moderation_on_video_long_skips_alpha_and_refunds(stack: Stack) -> None:
    user_id = new_user_id()
    await stack.topup(user_id, 25)
    task_id = await stack.generate(user_id, "@beta:fail=moderation", model="video-long")
    await stack.settle()

    task = await stack.task(task_id)
    assert (task["status"], len(task["attempts"])) == ("failed", 1)
    assert await stack.balance(user_id) == 25


async def test_dropped_webhook_is_handled_by_the_watchdog(stack: Stack) -> None:
    user_id = new_user_id()
    await stack.topup(user_id, 10)
    task_id = await stack.generate(user_id, "@alpha:drop")
    await stack.settle(rounds=1)
    await asyncio.sleep(0.4)  # past PROVIDER_TIMEOUT
    await stack.settle()

    task = await stack.task(task_id)
    assert task["status"] == "succeeded"
    assert task["attempts"][0]["reason"] == "watchdog_timeout"


async def test_late_completed_after_watchdog_is_accepted_while_task_runs(stack: Stack) -> None:
    user_id = new_user_id()
    await stack.topup(user_id, 10)
    task_id = await stack.generate(user_id, "@alpha:late @beta:delay=3")
    await stack.app.state.services.dispatcher.run_once()
    await asyncio.sleep(0.4)
    assert await stack.app.state.services.watchdog.run_once() == 1
    await stack.app.state.services.dispatcher.run_once()  # beta takes 3 s; alpha's late result comes at 0.6 s
    await stack.world.drain()

    task = await stack.task(task_id)
    assert task["status"] == "succeeded"
    assert task["result_url"].startswith("https://cdn.alpha.example/")
    assert [a["status"] for a in task["attempts"]] == ["succeeded", "superseded"]
    assert await stack.balance(user_id) == 8


class TestDemoTopUp:
    async def test_topup_arrives_as_signed_webhooks_and_credits_once(self, stack: Stack) -> None:
        user_id = new_user_id()
        response = await stack.client.post("/demo/topup", json={"user_id": user_id, "tokens": 30, "copies": 20})
        assert response.status_code == 200
        assert response.json()["delivered"] == [200] * 20
        assert await stack.balance(user_id) == 30

    async def test_topup_is_capped(self, stack: Stack) -> None:
        response = await stack.client.post("/demo/topup", json={"user_id": new_user_id(), "tokens": 101})
        assert response.status_code == 422
