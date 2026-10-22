"""A value recomputed at most once per `ttl` seconds in this process.

Only for derived, read-only numbers such as health checks: never for business state, which lives in the
database (rule 7). A stale value here is harmless; an expensive query on every public request is not.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable


class TtlCache[T]:
    def __init__(
        self, ttl: float, compute: Callable[[], Awaitable[T]], clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl = ttl
        self._compute = compute
        self._clock = clock
        self._lock = asyncio.Lock()  # concurrent callers on expiry share one computation
        self._entry: tuple[float, T] | None = None  # (computed at, value)

    async def get(self) -> T:
        async with self._lock:
            now = self._clock()
            if self._entry is None or now - self._entry[0] >= self._ttl:
                self._entry = (now, await self._compute())
            return self._entry[1]
