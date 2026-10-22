"""Fixed-window rate limit per client. The counter lives in PostgreSQL, so all workers share it (rule 7)."""

import math
from dataclasses import dataclass

from app.db import Connection


@dataclass(frozen=True, slots=True)
class Window:
    hits: int  # requests from this client in the current minute, this one included
    retry_after: int  # seconds until the window resets


async def hit(conn: Connection, client: str) -> Window:
    row = await conn.fetchrow(
        """INSERT INTO rate_limits AS r (client, window_start, hits) VALUES ($1, date_trunc('minute', now()), 1)
           ON CONFLICT (client) DO UPDATE
              SET hits = CASE WHEN r.window_start = excluded.window_start THEN r.hits + 1 ELSE 1 END,
                  window_start = excluded.window_start
        RETURNING r.hits, extract(epoch FROM r.window_start + interval '1 minute' - now()) AS left""",
        client,
    )
    assert row is not None  # an upsert always returns its row
    return Window(hits=row["hits"], retry_after=max(1, math.ceil(row["left"])))
