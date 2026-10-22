from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from app import ratelimit
from app.services import Services
from app.signing import SIGNATURE_HEADER, verify
from app.telegram import INIT_DATA_HEADER, InitDataError, verify_init_data


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


def telegram_user(request: Request, services: ServicesDep) -> int | None:
    """The caller's Telegram id proven by initData, or None when Telegram auth is not configured."""
    token = services.settings.telegram_bot_token
    if token is None:
        return None
    init_data = request.headers.get(INIT_DATA_HEADER)
    if not init_data:
        raise HTTPException(401, f"{INIT_DATA_HEADER} header is required")
    try:
        return verify_init_data(init_data, token.get_secret_value(), services.settings.telegram_init_data_ttl)
    except InitDataError as exc:
        raise HTTPException(401, "invalid Telegram init data") from exc


VerifiedUser = Annotated[int | None, Depends(telegram_user)]


async def rate_limited(request: Request, services: ServicesDep) -> None:
    """Throttle a client of the public endpoints that write. Behind a proxy the address is the one it
    forwarded (uvicorn trusts X-Forwarded-For only from FORWARDED_ALLOW_IPS)."""
    limit = services.settings.rate_limit_per_minute
    if not limit:
        return
    client = request.client.host if request.client else "unknown"
    async with services.pool.acquire() as conn:
        window = await ratelimit.hit(conn, client)
    if window.hits > limit:
        raise HTTPException(
            429,
            f"too many requests: at most {limit} per minute from one client",
            headers={"Retry-After": str(window.retry_after)},
        )


RateLimited = Depends(rate_limited)


def authorize(verified: int | None, user_id: int) -> None:
    if verified is not None and verified != user_id:
        raise HTTPException(403, "this is another user's data")


async def signed_payload[M: BaseModel](request: Request, secret: str, model: type[M]) -> M:
    """Verify the HMAC of the raw body *before* parsing it, then validate."""
    body = await request.body()
    if not verify(secret, body, request.headers.get(SIGNATURE_HEADER)):
        raise HTTPException(401, "bad signature")
    try:
        return model.model_validate_json(body)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False, include_context=False)) from exc
