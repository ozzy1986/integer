"""Telegram Mini App initData validation.

The task needs no registration: like the employer's Telegram service, a user is just a Telegram id.
When TELEGRAM_BOT_TOKEN is configured, the id is not trusted from the request body but proven by
initData signed by Telegram (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).
"""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

INIT_DATA_HEADER = "X-Telegram-Init-Data"


class InitDataError(ValueError):
    pass


def verify_init_data(init_data: str, bot_token: str, max_age: int, now: float | None = None) -> int:
    """Return the Telegram user id from valid initData, raise InitDataError otherwise."""
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise InitDataError("malformed init data") from exc
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise InitDataError("duplicate fields")

    received_hash = fields.pop("hash", "")
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), received_hash.encode()):
        raise InitDataError("bad signature")

    try:
        auth_date = int(fields["auth_date"])
    except (KeyError, ValueError) as exc:
        raise InitDataError("missing auth_date") from exc
    if (now if now is not None else time.time()) - auth_date > max_age:
        raise InitDataError("init data expired")

    try:
        user_id = json.loads(fields["user"])["id"]
    except (KeyError, ValueError, TypeError) as exc:
        raise InitDataError("missing user") from exc
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise InitDataError("bad user id")
    return user_id
