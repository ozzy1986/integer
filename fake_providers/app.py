"""Fake generation providers (alpha, beta, ...) and a fake payment provider in one small app.

    POST /{provider}/tasks        202 {"external_id"}; later a signed webhook to the service
    GET|PUT /{provider}/behaviour  read / change the behaviour (PUT needs X-Control-Token)
    POST /payments/pay            send a signed payment webhook to the service, optionally many copies

Run with: uvicorn fake_providers.app:create_fake_app --factory. One process on purpose: its job registry
lives in memory, which is fine for a fake and keeps it simple.
"""

import asyncio
import hmac
import json
import logging
import random
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import AliasChoices, BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.access_log import hide_local_health_checks
from app.signing import SIGNATURE_HEADER, sign
from fake_providers.behaviour import Behaviour, apply_directives, make_plan

log = logging.getLogger("fake_providers")
MAX_DELIVERY_TRIES = 4


class FakeSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    service_url: str = Field(validation_alias=AliasChoices("SERVICE_URL", "service_url"))
    provider_secrets: dict[str, SecretStr]
    payments_secret: SecretStr
    control_token: SecretStr | None = Field(None, validation_alias=AliasChoices("FAKE_CONTROL_TOKEN", "control_token"))
    default_behaviour: dict[str, Any] = Field(
        default_factory=dict, validation_alias=AliasChoices("FAKE_DEFAULT_BEHAVIOUR", "default_behaviour")
    )
    retry_backoff: float = Field(1.0, ge=0, validation_alias=AliasChoices("FAKE_RETRY_BACKOFF", "retry_backoff"))
    # The demo is public: memory stays bounded however many tasks arrive (and however late they answer).
    max_in_flight: int = Field(1000, ge=1, validation_alias=AliasChoices("FAKE_MAX_IN_FLIGHT", "max_in_flight"))
    max_jobs: int = Field(10_000, ge=1, validation_alias=AliasChoices("FAKE_MAX_JOBS", "max_jobs"))


class TaskIn(BaseModel):
    attempt_id: str = Field(min_length=1, max_length=64)
    prompt: str = Field(max_length=10_000)
    ref_images: list[str] = Field(default_factory=list, max_length=64)


class PayIn(BaseModel):
    user_id: int = Field(gt=0)
    tokens: int = Field(gt=0, le=1_000_000)
    copies: int = Field(1, ge=1, le=20)
    status: Literal["succeeded", "pending", "failed", "canceled"] = "succeeded"


class World:
    """All mutable state of the fake: behaviours, accepted jobs, in-flight deliveries."""

    def __init__(self, settings: FakeSettings, http: httpx.AsyncClient, rng: random.Random) -> None:
        self.settings = settings
        self.http = http
        self.rng = rng
        default = Behaviour.model_validate(settings.default_behaviour)
        self.behaviours = {name: default for name in settings.provider_secrets}
        # (provider, attempt_id) -> external_id of recent jobs, oldest first: enough to deduplicate a resend,
        # which comes within the service's dispatch lease, without growing forever.
        self.jobs: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def overloaded(self) -> bool:
        return len(self._tasks) >= self.settings.max_in_flight

    def remember(self, key: tuple[str, str], external_id: str) -> None:
        self.jobs[key] = external_id
        while len(self.jobs) > self.settings.max_jobs:
            self.jobs.popitem(last=False)

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for every scheduled delivery (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def deliver(
        self, path: str, secret: str, payload: dict[str, Any], copies: int, concurrent: bool
    ) -> list[int]:
        body = json.dumps(payload).encode()
        headers = {SIGNATURE_HEADER: sign(secret, body), "Content-Type": "application/json"}
        if concurrent:
            return list(await asyncio.gather(*(self._post(path, body, headers) for _ in range(copies))))
        codes = []
        for _ in range(copies):
            codes.append(await self._post(path, body, headers))
        return codes

    async def _post(self, path: str, body: bytes, headers: dict[str, str]) -> int:
        """Like a real provider: retry 5xx and network errors with backoff, give up on 4xx."""
        code = 0
        for attempt in range(MAX_DELIVERY_TRIES):
            if attempt:  # pause between tries only: after the last one there is nothing left to wait for
                await asyncio.sleep(self.settings.retry_backoff * 2 ** (attempt - 1))
            try:
                response = await self.http.post(f"{self.settings.service_url}{path}", content=body, headers=headers)
                code = response.status_code
                if code < 500:
                    return code
            except httpx.TransportError as exc:
                log.warning("delivery to %s failed: %r", path, exc)
                code = 0
        return code

    async def run_job(self, provider: str, task: TaskIn, external_id: str, behaviour: Behaviour) -> None:
        plan = make_plan(behaviour, self.rng)
        if not plan.deliver:
            log.info("%s %s: webhook dropped", provider, external_id)
            return
        await asyncio.sleep(plan.delay)
        payload: dict[str, Any] = {"attempt_id": task.attempt_id, "external_id": external_id, "status": plan.outcome}
        if plan.outcome == "completed":
            payload["result_url"] = f"https://cdn.{provider}.example/results/{external_id}.png"
        else:
            payload["error_type"] = plan.error_type
        secret = self.settings.provider_secrets[provider].get_secret_value()
        codes = await self.deliver(f"/webhooks/providers/{provider}", secret, payload, plan.copies, plan.concurrent)
        log.info("%s %s: %s x%d -> %s", provider, external_id, plan.outcome, plan.copies, codes)


def create_fake_app(
    settings: FakeSettings | None = None,
    *,
    service_transport: httpx.AsyncBaseTransport | None = None,
    rng: random.Random | None = None,
) -> FastAPI:
    settings = settings or FakeSettings()  # values come from the environment
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    hide_local_health_checks()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient(transport=service_transport, timeout=10.0, trust_env=False) as http:
            world = World(settings, http, rng or random.Random())  # noqa: S311 - not cryptographic
            app.state.world = world
            yield
            await world.drain()

    app = FastAPI(title="Fake providers", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def world_of(provider: str) -> World:
        world: World = app.state.world
        if provider not in world.behaviours:
            raise HTTPException(404, "unknown provider")
        return world

    @app.post("/{provider}/tasks", status_code=202)
    async def submit(provider: str, task: TaskIn, response: Response) -> dict[str, str]:
        world = world_of(provider)
        key = (provider, task.attempt_id)
        if key in world.jobs:  # idempotent by attempt_id: a resend creates no second job
            return {"external_id": world.jobs[key]}
        if world.overloaded:  # like a real provider at capacity; the service moves on to the next leg
            response.status_code = 503
            return {"detail": "overloaded"}
        behaviour = apply_directives(world.behaviours[provider], task.prompt, provider)
        plan = make_plan(behaviour, world.rng)
        if plan.sync_fail == "503":
            response.status_code = 503
            return {"detail": "service unavailable"}
        if plan.sync_fail == "timeout":
            # Hang past the caller's timeout, then accept anyway: the caller cannot know we did.
            await asyncio.sleep(behaviour.sync_timeout_seconds)
        external_id = f"{provider}-{uuid.uuid4().hex[:16]}"
        world.remember(key, external_id)
        # The sync decision is already made; replan only the delivery part from the same behaviour.
        world.spawn(world.run_job(provider, task, external_id, behaviour.model_copy(update={"sync_fail_rate": 0.0})))
        return {"external_id": external_id}

    @app.get("/{provider}/behaviour")
    async def get_behaviour(provider: str) -> Behaviour:
        return world_of(provider).behaviours[provider]

    @app.put("/{provider}/behaviour")
    async def put_behaviour(
        provider: str,
        behaviour: Behaviour,
        token: Annotated[str | None, Header(alias="X-Control-Token")] = None,
    ) -> Behaviour:
        expected = settings.control_token
        if (
            expected is None
            or token is None
            or not hmac.compare_digest(token.encode(), expected.get_secret_value().encode())
        ):
            raise HTTPException(403, "bad control token")
        world_of(provider).behaviours[provider] = behaviour
        return behaviour

    @app.post("/payments/pay")
    async def pay(request: PayIn) -> dict[str, Any]:
        world: World = app.state.world
        payment = {
            "payment_id": f"pay_{uuid.uuid4().hex}",
            "user_id": request.user_id,
            "tokens": request.tokens,
            "status": request.status,
        }
        codes = await world.deliver(
            "/webhooks/payments", settings.payments_secret.get_secret_value(), payment, request.copies, True
        )
        return {"payment_id": payment["payment_id"], "delivered": codes}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
