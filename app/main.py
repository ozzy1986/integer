"""Application factory. Run with: uvicorn app.main:create_app --factory --workers 4"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.api import public, webhooks
from app.config import Settings, get_settings
from app.db import create_pool
from app.http_security import BodySizeLimit, SecurityHeaders
from app.providers import ProviderGateway
from app.services import Services
from app.workers import BackgroundLoop, Dispatcher, Watchdog


def create_app(
    settings: Settings | None = None, *, provider_transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = await create_pool(settings)
        http = httpx.AsyncClient(
            transport=provider_transport,
            timeout=httpx.Timeout(settings.send_timeout),
            follow_redirects=False,
        )
        dispatcher = Dispatcher(pool, ProviderGateway(http, settings.provider_urls), settings.dispatch_lease,
                                settings.provider_timeout)  # fmt: skip
        watchdog = Watchdog(pool)
        background = BackgroundLoop(dispatcher, watchdog, settings.poll_interval)
        app.state.services = Services(settings, pool, http, dispatcher, watchdog, background)
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
    return app
