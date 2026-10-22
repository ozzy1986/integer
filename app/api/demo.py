"""Demo-only: top up a balance through the fake payment provider (enabled by DEMO_PAYMENTS_URL).

The money still arrives the production way, as signed payment webhooks; this endpoint only asks the
fake provider to send them, optionally many identical copies at once to show they credit once.
"""

import httpx
from fastapi import APIRouter, HTTPException

from app.api.deps import ServicesDep, VerifiedUser, authorize
from app.schemas import TopUpIn, TopUpOut

router = APIRouter(prefix="/demo", tags=["demo"])


@router.post("/topup")
async def topup(body: TopUpIn, services: ServicesDep, verified: VerifiedUser) -> TopUpOut:
    authorize(verified, body.user_id)
    if body.tokens > services.settings.demo_max_topup:
        raise HTTPException(422, f"demo top-up is limited to {services.settings.demo_max_topup} tokens")
    try:
        response = await services.http.post(
            f"{services.settings.demo_payments_url}/pay", json=body.model_dump(), timeout=60.0
        )
        response.raise_for_status()
        return TopUpOut.model_validate(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "payment provider is unavailable") from exc
