from dataclasses import dataclass

import asyncpg
import httpx

from app.config import Settings
from app.workers import BackgroundLoop, Dispatcher, Watchdog


@dataclass(frozen=True, slots=True)
class Services:
    """Process-wide dependencies, created in the app lifespan. Holds connections, never business state."""

    settings: Settings
    pool: asyncpg.Pool
    http: httpx.AsyncClient
    dispatcher: Dispatcher
    watchdog: Watchdog
    background: BackgroundLoop
