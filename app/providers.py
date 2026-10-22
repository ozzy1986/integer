"""HTTP client for generation providers: POST {base}/tasks -> 202 {"external_id": ...}.

Maps every way a submission can go wrong to a ProviderError (rule 3), so the engine stays HTTP-agnostic.
"""

from dataclasses import dataclass
from uuid import UUID

import httpx

from app.domain import ProviderError


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
    def __init__(self, client: httpx.AsyncClient, base_urls: dict[str, str]) -> None:
        self._client = client
        self._base_urls = {name: url.rstrip("/") for name, url in base_urls.items()}

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
            response = await self._client.post(f"{base_url}/tasks", json=payload)
        except httpx.TimeoutException as exc:
            raise SubmitFailed(ProviderError.TECHNICAL, "send_timeout") from exc
        except httpx.TransportError as exc:
            raise SubmitFailed(ProviderError.TECHNICAL, "connect_error") from exc

        if response.status_code >= 500:
            raise SubmitFailed(ProviderError.TECHNICAL, f"http_{response.status_code}")
        if response.status_code >= 400:
            raise SubmitFailed(ProviderError.PAYLOAD, f"http_{response.status_code}")
        try:
            external_id = response.json()["external_id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise SubmitFailed(ProviderError.TECHNICAL, "bad_response") from exc
        if response.status_code not in (200, 201, 202) or not isinstance(external_id, str) or not external_id:
            raise SubmitFailed(ProviderError.TECHNICAL, "bad_response")
        return external_id[:128]
