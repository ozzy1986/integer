"""The background loop as it runs in every worker: a hanging provider must not stall anything else."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from tests.integration.conftest import Harness, accepted


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {"poll_interval": 0.05}


async def eventually(check: Callable[[], Awaitable[bool]], within: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + within
    while not await check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"not true within {within}s")
        await asyncio.sleep(0.02)


async def test_a_hanging_provider_holds_up_neither_other_sends_nor_the_watchdog(harness: Harness) -> None:
    release = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return accepted(request)

    overdue_task = await harness.start_task(await harness.new_user(tokens=30), model="video-long")  # beta, sent
    harness.stub.script("alpha", hang)
    background = harness.services.background
    background.start()
    try:
        await harness.generate(await harness.new_user(tokens=10))  # image-fast -> alpha, which hangs
        await eventually(lambda: _called(harness, "alpha"))

        await harness.make_overdue(overdue_task)
        other_task = (await harness.generate(await harness.new_user(tokens=30), model="video-long")).json()["task_id"]
        background.wake()

        await eventually(lambda: _first_attempt_is(harness, other_task, "sent"))
        await eventually(lambda: _first_attempt_is(harness, overdue_task, "failed"))
    finally:
        release.set()
        await background.stop()


async def _called(harness: Harness, provider: str) -> bool:
    return provider in harness.stub.providers_called()


async def _first_attempt_is(harness: Harness, task_id: str, status: str) -> bool:
    return bool((await harness.task(task_id))["attempts"][0]["status"] == status)
