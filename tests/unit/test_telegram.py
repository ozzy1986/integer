"""Telegram Mini App initData validation (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)."""

import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

from app.telegram import InitDataError, verify_init_data

TOKEN = "123456:TEST-bot-token"
NOW = 1_800_000_000


def make_init_data(user_id: int = 42, auth_date: int = NOW, token: str = TOKEN, **extra: str) -> str:
    fields = {"auth_date": str(auth_date), "user": json.dumps({"id": user_id, "first_name": "Ann"}), **extra}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_valid_init_data_yields_the_telegram_user_id() -> None:
    assert verify_init_data(make_init_data(user_id=777, query_id="q"), TOKEN, max_age=60, now=NOW) == 777


def test_tampered_user_is_rejected() -> None:
    forged = make_init_data(user_id=1).replace("%22id%22%3A+1", "%22id%22%3A+2")
    assert forged != make_init_data(user_id=1)
    with pytest.raises(InitDataError, match="signature"):
        verify_init_data(forged, TOKEN, max_age=60, now=NOW)


def test_data_signed_for_another_bot_is_rejected() -> None:
    with pytest.raises(InitDataError, match="signature"):
        verify_init_data(make_init_data(token="999:other"), TOKEN, max_age=60, now=NOW)


def test_stale_init_data_is_rejected() -> None:
    with pytest.raises(InitDataError, match="expired"):
        verify_init_data(make_init_data(auth_date=NOW - 61), TOKEN, max_age=60, now=NOW)


@pytest.mark.parametrize(
    "init_data",
    ["", "garbage", "auth_date=1&user=%7B%7D", "hash=00&hash=00", "user=%7B%7D&auth_date=x&hash=00"],
)
def test_malformed_init_data_is_rejected(init_data: str) -> None:
    with pytest.raises(InitDataError):
        verify_init_data(init_data, TOKEN, max_age=60, now=NOW)


def test_user_without_numeric_id_is_rejected() -> None:
    fields = {"auth_date": str(NOW), "user": json.dumps({"id": "abc"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    with pytest.raises(InitDataError, match="user"):
        verify_init_data(urlencode(fields), TOKEN, max_age=60, now=NOW)
