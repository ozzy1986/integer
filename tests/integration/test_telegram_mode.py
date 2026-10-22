"""With TELEGRAM_BOT_TOKEN set, the user id must be proven by Telegram-signed initData."""

from typing import Any

import pytest

from tests.conftest import new_user_id
from tests.integration.conftest import Harness
from tests.unit.test_telegram import TOKEN, make_init_data

pytestmark = pytest.mark.usefixtures("harness")


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {"telegram_bot_token": TOKEN, "telegram_init_data_ttl": 10**10}


def auth(user_id: int) -> dict[str, str]:
    return {"X-Telegram-Init-Data": make_init_data(user_id=user_id)}


async def test_generate_without_init_data_is_401(harness: Harness) -> None:
    assert (await harness.generate(new_user_id())).status_code == 401


async def test_generate_for_another_user_is_403(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=10)
    assert (await harness.generate(user_id, headers=auth(user_id + 1))).status_code == 403


async def test_generate_for_yourself_works(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=10)
    assert (await harness.generate(user_id, headers=auth(user_id))).status_code == 202


async def test_someone_elses_balance_and_tasks_are_hidden(harness: Harness) -> None:
    owner = await harness.new_user(tokens=10)
    task_id = (await harness.generate(owner, headers=auth(owner))).json()["task_id"]
    stranger = auth(owner + 1)

    assert (await harness.client.get(f"/users/{owner}/balance", headers=stranger)).status_code == 403
    assert (await harness.client.get(f"/users/{owner}/tasks", headers=stranger)).status_code == 403
    assert (await harness.client.get(f"/tasks/{task_id}", headers=stranger)).status_code == 404
    assert (await harness.client.get(f"/tasks/{task_id}", headers=auth(owner))).status_code == 200


async def test_webhooks_do_not_need_init_data(harness: Harness) -> None:
    # Payment webhooks come from the payment provider, authenticated by their signature.
    assert (await harness.pay(new_user_id(), 5)).status_code == 200
