"""The fake providers' HTTP behaviour: what the service will see from them."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.signing import SIGNATURE_HEADER, verify
from fake_providers.app import FakeSettings, create_fake_app

SECRETS = {"alpha": "a" * 40, "beta": "b" * 40}
PAYMENTS_SECRET = "p" * 40
CONTROL_TOKEN = "c" * 40


class Receiver:
    """Plays the service: records webhooks, answers with scripted status codes (default 200)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.codes: list[int] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        code = self.codes.pop(0) if self.codes else 200
        return httpx.Response(code, json={"credited": True, "outcome": "ok"})

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture
def receiver() -> Receiver:
    return Receiver()


@pytest.fixture
async def fake(receiver: Receiver) -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    settings = FakeSettings(
        service_url="http://service.test",
        provider_secrets=SECRETS,
        payments_secret=PAYMENTS_SECRET,
        control_token=CONTROL_TOKEN,
        default_behaviour={"delay_min": 0, "delay_max": 0},
        retry_backoff=0,
    )
    app = create_fake_app(settings, service_transport=httpx.MockTransport(receiver.handler))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fake.test") as client,
    ):
        yield client, app.state.world


async def submit(
    client: httpx.AsyncClient, provider: str = "alpha", prompt: str = "cat", attempt_id: str = "a-1"
) -> httpx.Response:
    return await client.post(f"/{provider}/tasks", json={"attempt_id": attempt_id, "prompt": prompt, "ref_images": []})


async def test_accepts_a_task_and_delivers_a_signed_completed_webhook(fake: Any, receiver: Receiver) -> None:
    client, world = fake
    response = await submit(client)
    await world.drain()

    assert response.status_code == 202
    external_id = response.json()["external_id"]
    [request] = receiver.requests
    assert request.url == "http://service.test/webhooks/providers/alpha"
    assert verify(SECRETS["alpha"], request.content, request.headers[SIGNATURE_HEADER])
    body = json.loads(request.content)
    assert body["status"] == "completed"
    assert (body["attempt_id"], body["external_id"]) == ("a-1", external_id)
    assert body["result_url"].startswith("https://")


async def test_resubmitting_the_same_attempt_is_idempotent(fake: Any, receiver: Receiver) -> None:
    client, world = fake
    first, second = await submit(client), await submit(client)
    await world.drain()
    assert first.json() == second.json()
    assert len(receiver.requests) == 1


async def test_each_provider_signs_with_its_own_secret(fake: Any, receiver: Receiver) -> None:
    client, world = fake
    await submit(client, provider="beta")
    await world.drain()
    [request] = receiver.requests
    assert request.url.path == "/webhooks/providers/beta"
    assert verify(SECRETS["beta"], request.content, request.headers[SIGNATURE_HEADER])
    assert not verify(SECRETS["alpha"], request.content, request.headers[SIGNATURE_HEADER])


async def test_failure_directive(fake: Any, receiver: Receiver) -> None:
    client, world = fake
    await submit(client, prompt="cat @alpha:fail=moderation")
    await world.drain()
    assert [(b["status"], b["error_type"]) for b in receiver.bodies()] == [("failed", "moderation")]


async def test_sync_503(fake: Any, receiver: Receiver) -> None:
    client, world = fake
    assert (await submit(client, prompt="@alpha:sync=503")).status_code == 503
    await world.drain()
    assert receiver.requests == []


async def test_duplicates_are_identical(fake: Any, receiver: Receiver) -> None:
    client, world = fake
    await submit(client, prompt="@alpha:dup=5")
    await world.drain()
    assert len(receiver.requests) == 5
    assert len({r.content for r in receiver.requests}) == 1


async def test_dropped_webhook_never_arrives(fake: Any, receiver: Receiver) -> None:
    client, world = fake
    assert (await submit(client, prompt="@alpha:drop")).status_code == 202
    await world.drain()
    assert receiver.requests == []


async def test_failed_delivery_is_retried_like_a_real_provider(fake: Any, receiver: Receiver) -> None:
    client, world = fake
    receiver.codes = [500, 503, 200]
    await submit(client)
    await world.drain()
    assert len(receiver.requests) == 3


async def test_4xx_from_the_service_is_not_retried(fake: Any, receiver: Receiver) -> None:
    client, world = fake
    receiver.codes = [404]
    await submit(client)
    await world.drain()
    assert len(receiver.requests) == 1


async def test_unknown_provider_is_404(fake: Any) -> None:
    client, _ = fake
    assert (await submit(client, provider="gamma")).status_code == 404


async def test_malformed_task_is_422(fake: Any) -> None:
    client, _ = fake
    assert (await client.post("/alpha/tasks", json={"prompt": "x"})).status_code == 422


class TestControl:
    async def test_behaviour_can_be_changed_with_the_token(self, fake: Any, receiver: Receiver) -> None:
        client, world = fake
        response = await client.put(
            "/alpha/behaviour",
            json={"fail_rate": 1, "fail_type": "payload"},
            headers={"X-Control-Token": CONTROL_TOKEN},
        )
        assert response.status_code == 200
        assert (await client.get("/alpha/behaviour")).json()["fail_type"] == "payload"
        await submit(client)
        await world.drain()
        assert receiver.bodies()[0]["error_type"] == "payload"

    @pytest.mark.parametrize("headers", [{}, {"X-Control-Token": "wrong"}])
    async def test_behaviour_change_requires_the_token(self, fake: Any, headers: dict[str, str]) -> None:
        client, _ = fake
        assert (await client.put("/alpha/behaviour", json={"fail_rate": 1}, headers=headers)).status_code == 403

    async def test_invalid_behaviour_is_422(self, fake: Any) -> None:
        client, _ = fake
        response = await client.put(
            "/alpha/behaviour", json={"fail_rate": 2}, headers={"X-Control-Token": CONTROL_TOKEN}
        )
        assert response.status_code == 422


class TestPayments:
    async def test_pay_sends_signed_payment_webhooks(self, fake: Any, receiver: Receiver) -> None:
        client, _ = fake
        response = await client.post("/payments/pay", json={"user_id": 7, "tokens": 30, "copies": 20})

        assert response.status_code == 200
        result = response.json()
        assert result["delivered"] == [200] * 20
        assert len(receiver.requests) == 20
        bodies = receiver.bodies()
        assert {b["payment_id"] for b in bodies} == {result["payment_id"]}
        assert bodies[0] == {"payment_id": result["payment_id"], "user_id": 7, "tokens": 30, "status": "succeeded"}
        assert all(verify(PAYMENTS_SECRET, r.content, r.headers[SIGNATURE_HEADER]) for r in receiver.requests)
        assert all(r.url.path == "/webhooks/payments" for r in receiver.requests)

    @pytest.mark.parametrize("body", [{"user_id": 7, "tokens": 0}, {"user_id": 7, "tokens": 5, "copies": 21}])
    async def test_invalid_payment_request_is_422(self, fake: Any, body: dict[str, int]) -> None:
        client, _ = fake
        assert (await client.post("/payments/pay", json=body)).status_code == 422
