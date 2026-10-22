"""HTTP client for generation providers: POST {base}/tasks -> 202 {"external_id": ...}.

Maps every way a submission can go wrong to a ProviderError (rule 3), so the engine stays HTTP-agnostic.
"""

import asyncio
from dataclasses import dataclass
from uuid import UUID

import httpx

from app.domain import ProviderError

# Only these 4xx say "the request itself is malformed", which any leg would reject (rule 2's `payload`).
# The rest — 401/403 (our credentials for this provider), 404, 408 (timeout), 409, 429 (rate limit) — are
# this provider's trouble, so the next leg may well succeed.
_PAYLOAD_STATUSES = frozenset({400, 413, 415, 422})


@dataclass(frozen=True, slots=True)
class Submission:
    attempt_id: UUID
    provider: str
    prompt: str
    ref_images: list[str]


class SubmitFailed(Exception):
    def __init__(self, error: ProviderError, reason: str) -> None:
        super().__init__(f"{error}: {reason}")
        self.error = error
        self.reason = reason


class ProviderGateway:
    def __init__(self, client: httpx.AsyncClient, base_urls: dict[str, str], deadline: float) -> None:
        self._client = client
        self._base_urls = {name: url.rstrip("/") for name, url in base_urls.items()}
        # httpx timeouts limit each network operation; this caps the whole call (a slow drip included).
        self._deadline = deadline

    async def submit(self, submission: Submission) -> str:
        """Return the provider's external_id or raise SubmitFailed."""
        base_url = self._base_urls.get(submission.provider)
        if base_url is None:
            raise SubmitFailed(ProviderError.TECHNICAL, "unknown_provider")
        payload = {
            # attempt_id doubles as the idempotency key: a resend after a crash creates no second job,
            # and webhooks carry it back, so even a job whose 202 we never saw can be matched.
            "attempt_id": str(submission.attempt_id),
            "prompt": submission.prompt,
            "ref_images": submission.ref_images,
        }
        try:
            async with asyncio.timeout(self._deadline):
                response = await self._client.post(f"{base_url}/tasks", json=payload)
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise SubmitFailed(ProviderError.TECHNICAL, "send_timeout") from exc
        except httpx.TransportError as exc:
            raise SubmitFailed(ProviderError.TECHNICAL, "connect_error") from exc

        if response.status_code in _PAYLOAD_STATUSES:
            raise SubmitFailed(ProviderError.PAYLOAD, f"http_{response.status_code}")
        if response.status_code >= 400:
            raise SubmitFailed(ProviderError.TECHNICAL, f"http_{response.status_code}")
        try:
            external_id = response.json()["external_id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise SubmitFailed(ProviderError.TECHNICAL, "bad_response") from exc
        if (
            response.status_code not in (200, 201, 202)
            or not isinstance(external_id, str)
            or not external_id
            or not external_id.isprintable()  # a NUL could not even be stored
        ):
            raise SubmitFailed(ProviderError.TECHNICAL, "bad_response")
        return external_id[:128]
