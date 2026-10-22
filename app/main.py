"""Application factory. Run with: uvicorn app.main:create_app --factory --workers 4"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.access_log import hide_local_health_checks
from app.api import demo, public, webhooks
from app.cache import TtlCache
from app.config import Settings, get_settings
from app.db import create_pool
from app.http_security import BodySizeLimit, SecurityHeaders
from app.providers import ProviderGateway
from app.services import Services
from app.workers import BackgroundLoop, Dispatcher, Watchdog

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    settings: Settings | None = None, *, provider_transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    hide_local_health_checks()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = await create_pool(settings)
        http = httpx.AsyncClient(
            transport=provider_transport,
            timeout=httpx.Timeout(settings.send_timeout),
            follow_redirects=False,
            # Provider calls go to configured URLs only: never through a proxy picked up from the environment.
            trust_env=False,
        )
        gateway = ProviderGateway(http, settings.provider_urls, settings.send_timeout)
        dispatcher = Dispatcher(pool, gateway, settings.dispatch_lease, settings.provider_timeout)
        watchdog = Watchdog(pool)
        background = BackgroundLoop(dispatcher, watchdog, settings.poll_interval)

        async def count_balance_drift() -> int:
            drift: int = await pool.fetchval("SELECT count(*) FROM balance_drift")
            return drift

        balance_drift = TtlCache(settings.invariants_cache_seconds, count_balance_drift)
        app.state.services = Services(settings, pool, http, dispatcher, watchdog, background, balance_drift)
        if settings.background_jobs:
            background.start()
        try:
            yield
        finally:
            await background.stop()
            await http.aclose()
            await pool.close()

    app = FastAPI(
        title="Generation service",
        summary="Charges tokens, routes tasks through provider legs, refunds exactly once.",
        version="1.0.0",
        lifespan=lifespan,
        redoc_url=None,
    )
    app.add_middleware(BodySizeLimit, max_bytes=settings.max_body_bytes)
    app.add_middleware(SecurityHeaders)
    app.include_router(public.router)
    app.include_router(webhooks.router)
    if settings.demo_payments_url:
        app.include_router(demo.router)
    # The demo UI. Mounted last so it never shadows an API route.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    return app
