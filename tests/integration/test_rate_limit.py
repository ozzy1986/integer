"""Per-client rate limit of the public endpoints that write (POST /generate, POST /demo/topup)."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from tests.integration.conftest import Harness

LIMIT = 3


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {"rate_limit_per_minute": LIMIT}


@asynccontextmanager
async def client_at(harness: Harness, address: str) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=harness.app, client=(address, 4321))
    async with httpx.AsyncClient(transport=transport, base_url="http://service.test") as client:
        yield client


def new_address() -> str:
    # Unique per test run, so counters left in this minute by earlier tests never interfere.
    return f"2001:db8::{uuid.uuid4().hex[:4]}:{uuid.uuid4().hex[:4]}"


async def generate(client: httpx.AsyncClient) -> httpx.Response:
    body = {"user_id": 42, "model_slug": "image-fast", "prompt": "x"}
    return await client.post("/generate", json=body)


async def test_writes_over_the_limit_are_429_with_retry_after(harness: Harness) -> None:
    async with client_at(harness, new_address()) as client:
        allowed = [await generate(client) for _ in range(LIMIT)]
        refused = await generate(client)

    assert [r.status_code for r in allowed] == [402] * LIMIT  # user 42 has no tokens: not throttled, just poor
    assert refused.status_code == 429
    assert "too many requests" in refused.json()["detail"]
    assert 1 <= int(refused.headers["retry-after"]) <= 60


async def test_the_limit_is_per_client(harness: Harness) -> None:
    async with client_at(harness, new_address()) as busy:
        for _ in range(LIMIT + 1):
            await generate(busy)
    async with client_at(harness, new_address()) as other:
        assert (await generate(other)).status_code == 402


async def test_reads_and_webhooks_are_not_limited(harness: Harness) -> None:
    async with client_at(harness, new_address()) as client:
        for _ in range(LIMIT + 2):
            assert (await client.get("/models")).status_code == 200
            assert (await client.get("/users/42/balance")).status_code == 200
