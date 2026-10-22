from dataclasses import dataclass

import asyncpg
import httpx

from app.cache import TtlCache
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
    balance_drift: TtlCache[int]  # users whose balance differs from their ledger (rule 8, must be 0)
