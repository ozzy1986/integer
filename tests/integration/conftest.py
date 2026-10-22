"""Harness: the real app on the real database; provider HTTP calls go to a scriptable stub.

Webhooks are sent by the tests themselves (properly signed), so timing is deterministic while the
races are real: concurrent requests run in separate database connections and transactions.
"""

import asyncio
import itertools
import json
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.config import Settings
from app.main import create_app
from app.services import Services
from app.signing import SIGNATURE_HEADER, sign
from tests.conftest import new_user_id

PROVIDER_SECRETS = {"alpha": "a" * 40, "beta": "b" * 40}
PAYMENTS_SECRET = "p" * 40

Reply = Callable[[httpx.Request], httpx.Response]


def accepted(request: httpx.Request) -> httpx.Response:
    return httpx.Response(202, json={"external_id": f"ext-{uuid.uuid4()}"})


def status(code: int) -> Reply:
    return lambda request: httpx.Response(code, json={"detail": "stub"})


def raises(exc: type[Exception]) -> Reply:
    def reply(request: httpx.Request) -> httpx.Response:
        raise exc("stubbed", request=request)  # type: ignore[call-arg]

    return reply


@dataclass
class ProviderStub:
    """Answers POST /tasks per provider: queued replies first, then `accepted`."""

    replies: dict[str, deque[Reply]] = field(default_factory=lambda: defaultdict(deque))
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def script(self, provider: str, *replies: Reply) -> None:
        self.replies[provider].extend(replies)

    def handler(self, request: httpx.Request) -> httpx.Response:
        provider = request.url.host.split(".")[0]
        assert request.url.path == "/tasks"
        self.calls.append((provider, json.loads(request.content)))
        queue = self.replies[provider]
        return (queue.popleft() if queue else accepted)(request)

    def providers_called(self) -> list[str]:
        return [provider for provider, _ in self.calls]


def make_settings(db_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": db_url,
        "provider_urls": {name: f"http://{name}.test" for name in PROVIDER_SECRETS},
        "provider_secrets": PROVIDER_SECRETS,
        "payments_secret": PAYMENTS_SECRET,
        "background_jobs": False,  # tests drive the dispatcher and the watchdog explicitly
        "provider_timeout": 60.0,
        "db_pool_max": 30,
    }
    values.update(overrides)
    return Settings(**values)


class Harness:
    def __init__(self, app: FastAPI, client: httpx.AsyncClient, stub: ProviderStub) -> None:
        self.app = app
        self.client = client
        self.stub = stub
        self._payment_ids = itertools.count()

    @property
    def services(self) -> Services:
        services: Services = self.app.state.services
        return services

    async def post_signed(self, path: str, payload: dict[str, Any], secret: str) -> httpx.Response:
        body = json.dumps(payload).encode()
        headers = {SIGNATURE_HEADER: sign(secret, body), "Content-Type": "application/json"}
        return await self.client.post(path, content=body, headers=headers)

    async def pay(self, user_id: int, tokens: int, payment_id: str | None = None) -> httpx.Response:
        payment = {
            "payment_id": payment_id or f"pay-{uuid.uuid4()}",
            "user_id": user_id,
            "tokens": tokens,
            "status": "succeeded",
        }
        return await self.post_signed("/webhooks/payments", payment, PAYMENTS_SECRET)

    async def new_user(self, tokens: int = 100) -> int:
        user_id = new_user_id()
        if tokens:
            assert (await self.pay(user_id, tokens)).status_code == 200
        return user_id

    async def generate(self, user_id: int, /, model: str = "image-fast", refs: int = 0, **extra: Any) -> httpx.Response:
        body = {
            "user_id": user_id,
            "model_slug": model,
            "prompt": "a cat in a hat",
            "ref_images": [f"https://img.example/{i}.png" for i in range(refs)],
        }
        headers = extra.pop("headers", None)
        body.update(extra)
        return await self.client.post("/generate", json=body, headers=headers)

    async def start_task(self, user_id: int, model: str = "image-fast", refs: int = 0) -> str:
        response = await self.generate(user_id, model, refs)
        assert response.status_code == 202, response.text
        task_id: str = response.json()["task_id"]
        await self.dispatch()
        return task_id

    async def dispatch(self) -> int:
        return await self.services.dispatcher.run_once()

    async def watchdog(self) -> int:
        return await self.services.watchdog.run_once()

    async def task(self, task_id: str) -> dict[str, Any]:
        response = await self.client.get(f"/tasks/{task_id}")
        assert response.status_code == 200, response.text
        result: dict[str, Any] = response.json()
        return result

    async def live_attempt(self, task_id: str) -> dict[str, Any]:
        attempts = (await self.task(task_id))["attempts"]
        live = [a for a in attempts if a["status"] in ("pending", "sent")]
        assert len(live) == 1, attempts
        return live[0]

    async def webhook(self, provider: str, attempt_id: str, **payload: Any) -> httpx.Response:
        body = {"attempt_id": attempt_id, "external_id": f"ext-{attempt_id}", **payload}
        return await self.post_signed(f"/webhooks/providers/{provider}", body, PROVIDER_SECRETS[provider])

    async def complete(self, provider: str, attempt_id: str, url: str = "https://cdn.example/r.png") -> httpx.Response:
        return await self.webhook(provider, attempt_id, status="completed", result_url=url)

    async def fail(self, provider: str, attempt_id: str, error: str = "technical") -> httpx.Response:
        return await self.webhook(provider, attempt_id, status="failed", error_type=error)

    async def balance(self, user_id: int) -> dict[str, Any]:
        response = await self.client.get(f"/users/{user_id}/balance")
        assert response.status_code == 200, response.text
        result: dict[str, Any] = response.json()
        return result

    async def ledger_kinds(self, user_id: int) -> list[str]:
        rows = (await self.balance(user_id))["ledger"]
        return [row["kind"] for row in reversed(rows)]  # oldest first

    async def make_overdue(self, task_id: str) -> None:
        async with self.services.pool.acquire() as conn:
            await conn.execute(
                "UPDATE attempts SET deadline_at = now() - interval '1 second' WHERE task_id = $1 AND status = 'sent'",
                uuid.UUID(task_id),
            )


@pytest.fixture
def stub() -> ProviderStub:
    return ProviderStub()


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {}


@pytest.fixture
async def harness(migrated_db: str, stub: ProviderStub, settings_overrides: dict[str, Any]) -> AsyncIterator[Harness]:
    app = create_app(
        make_settings(migrated_db, **settings_overrides), provider_transport=httpx.MockTransport(stub.handler)
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://service.test") as client:
            yield Harness(app, client, stub)
        # Isolation: the dispatcher and the watchdog work on the whole table, so attempts left alive by
        # this test must not be picked up by the next one. Money rows are untouched.
        await app.state.services.pool.execute(
            "UPDATE attempts SET status = 'superseded', finished_at = now() WHERE status IN ('pending', 'sent')"
        )


async def gather_responses(*calls: Any) -> list[httpx.Response]:
    responses: list[httpx.Response] = await asyncio.gather(*calls)
    return responses


@pytest.fixture(scope="session", autouse=True)
async def balance_equals_ledger_after_all_tests(pool: Any) -> AsyncIterator[None]:
    """Rule 8, checked globally: after every scenario of the session no balance drifts from its ledger."""
    yield
    async with pool.acquire() as conn:
        assert await conn.fetch("SELECT * FROM balance_drift") == []
