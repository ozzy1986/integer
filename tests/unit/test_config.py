from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings

VALID: dict[str, Any] = {
    "database_url": "postgresql://u:p@db/x",
    "provider_urls": {"alpha": "http://a"},
    "provider_secrets": {"alpha": "a" * 32},
    "payments_secret": "p" * 32,
}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """pydantic-settings merges dict fields from the environment into init values; isolate from it."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def test_empty_optional_values_mean_off() -> None:
    # docker compose renders `${TELEGRAM_BOT_TOKEN:-}` as "", which must not switch Telegram auth on.
    settings = Settings(**VALID, telegram_bot_token="", demo_payments_url="")
    assert settings.telegram_bot_token is None
    assert settings.demo_payments_url is None


@pytest.mark.parametrize(
    "change",
    [
        {"payments_secret": "short"},
        {"provider_secrets": {"alpha": "short"}},
        {"provider_secrets": {}},
        {"provider_urls": {"alpha": "http://a", "beta": "http://b"}},  # beta has no secret
        {"send_timeout": 30, "dispatch_lease": 30},
    ],
)
def test_unsafe_configuration_is_refused(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings(**{**VALID, **change})
