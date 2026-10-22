"""Webhooks from generation providers and from the payment provider. Authenticated only by HMAC."""

from fastapi import APIRouter, HTTPException, Request

from app import billing, engine
from app.api.deps import ServicesDep, signed_payload
from app.schemas import PaymentOut, PaymentWebhookIn, ProviderWebhookIn, WebhookOut

router = APIRouter(prefix="/webhooks")


@router.post("/providers/{name}")
async def provider_webhook(name: str, request: Request, services: ServicesDep) -> WebhookOut:
    secret = services.settings.provider_secrets.get(name)
    if secret is None:
        raise HTTPException(404, "unknown provider")
    payload = await signed_payload(request, secret.get_secret_value(), ProviderWebhookIn)

    async with services.pool.acquire() as conn:
        attempt_id = payload.attempt_id
        if attempt_id is None:
            assert payload.external_id is not None  # type narrowing: the model validator requires one of them
            attempt_id = await engine.find_attempt(conn, name, payload.external_id)
        if attempt_id is None:
            outcome = engine.Outcome.UNKNOWN
        elif payload.status == "completed":
            assert payload.result_url is not None  # type narrowing: the model validator requires it
            outcome = await engine.complete_attempt(conn, attempt_id, payload.result_url, provider=name)
        else:
            assert payload.error_type is not None  # type narrowing: the model validator requires it
            outcome = await engine.fail_attempt(conn, attempt_id, payload.error_type, "webhook", provider=name)

    if outcome is engine.Outcome.UNKNOWN:
        raise HTTPException(404, "unknown attempt")
    if outcome is engine.Outcome.NEXT_ATTEMPT:
        services.background.wake()
    # Duplicates are answered 200: the provider must stop retrying, the effect already happened once.
    return WebhookOut(outcome=outcome)


@router.post("/payments")
async def payment_webhook(request: Request, services: ServicesDep) -> PaymentOut:
    payload = await signed_payload(request, services.settings.payments_secret.get_secret_value(), PaymentWebhookIn)
    payment = billing.Payment(payload.payment_id, payload.user_id, payload.tokens, payload.status)
    async with services.pool.acquire() as conn, conn.transaction():
        try:
            credited = await billing.apply_payment(conn, payment)
        except billing.PaymentConflict as exc:
            raise HTTPException(409, "payment_id already used with a different user or amount") from exc
    return PaymentOut(credited=credited)
