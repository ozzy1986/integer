"""End-to-end harness: talks HTTP to the running compose stack (4 workers + fake providers).

Skipped unless E2E_BASE_URL is set; `docker compose run --rm tests` sets it.
"""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest

from app.signing import SIGNATURE_HEADER, sign

BASE_URL = os.environ.get("E2E_BASE_URL")


def provider_secret(name: str) -> str:
    secret: str = json.loads(os.environ["PROVIDER_SECRETS"])[name]
    return secret


def provider_timeout() -> float:
    return float(os.environ.get("E2E_PROVIDER_TIMEOUT", "60"))


class Api:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def signed(self, path: str, payload: dict[str, Any], secret: str) -> httpx.Response:
        body = json.dumps(payload).encode()
        return await self.client.post(
            path, content=body, headers={SIGNATURE_HEADER: sign(secret, body), "Content-Type": "application/json"}
        )

    async def pay(self, user_id: int, tokens: int, payment_id: str | None = None) -> httpx.Response:
        payment = {"payment_id": payment_id or f"e2e-{uuid.uuid4()}", "user_id": user_id, "tokens": tokens,
                   "status": "succeeded"}  # fmt: skip
        return await self.signed("/webhooks/payments", payment, os.environ["PAYMENTS_SECRET"])

    async def generate(self, user_id: int, prompt: str, model: str = "image-fast") -> str:
        response = await self.client.post("/generate", json={"user_id": user_id, "model_slug": model, "prompt": prompt})
        assert response.status_code == 202, response.text
        task_id: str = response.json()["task_id"]
        return task_id

    async def task(self, task_id: str) -> dict[str, Any]:
        response = await self.client.get(f"/tasks/{task_id}")
        assert response.status_code == 200, response.text
        result: dict[str, Any] = response.json()
        return result

    async def balance(self, user_id: int) -> dict[str, Any]:
        result: dict[str, Any] = (await self.client.get(f"/users/{user_id}/balance")).json()
        return result

    async def wait_task(self, task_id: str, until: Callable[[dict[str, Any]], bool], within: float) -> dict[str, Any]:
        return await wait_for(lambda: self.task(task_id), until, within)


async def wait_for[T](
    probe: Callable[[], Awaitable[T]], until: Callable[[T], bool], within: float, interval: float = 0.5
) -> T:
    deadline = asyncio.get_running_loop().time() + within
    while True:
        value = await probe()
        if until(value):
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"condition not met within {within}s; last value: {value}")
        await asyncio.sleep(interval)


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    assert BASE_URL
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        yield Api(client)


def new_user_id() -> int:
    return uuid.uuid4().int % 2**52 + 1
