import asyncio

from app.cache import TtlCache


class Counter:
    def __init__(self) -> None:
        self.calls = 0

    async def compute(self) -> int:
        self.calls += 1
        await asyncio.sleep(0)
        return self.calls


async def test_value_is_recomputed_only_after_the_ttl() -> None:
    now = [100.0]
    counter = Counter()
    cache = TtlCache(10.0, counter.compute, clock=lambda: now[0])

    assert await cache.get() == 1
    now[0] += 9.9
    assert await cache.get() == 1
    now[0] += 0.2
    assert await cache.get() == 2


async def test_concurrent_callers_share_one_computation() -> None:
    counter = Counter()
    cache = TtlCache(10.0, counter.compute)

    assert await asyncio.gather(*(cache.get() for _ in range(20))) == [1] * 20
    assert counter.calls == 1


async def test_zero_ttl_means_no_caching() -> None:
    counter = Counter()
    cache = TtlCache(0.0, counter.compute)
    assert [await cache.get() for _ in range(3)] == [1, 2, 3]
