"""Models and their legs. The catalog is tiny and read on every decision, straight from the database:
no in-process cache, so a catalog change is seen by all workers at once."""

from dataclasses import dataclass

import asyncpg

from app.db import Connection
from app.domain import Leg

_QUERY = """
    SELECT m.slug, m.price,
           array_agg((l.priority, l.provider, l.max_ref_images, l.skip_on_moderation) ORDER BY l.priority) AS legs
      FROM models m JOIN legs l ON l.model_slug = m.slug
"""


@dataclass(frozen=True, slots=True)
class Model:
    slug: str
    price: int
    legs: tuple[Leg, ...]


def _model(row: asyncpg.Record) -> Model:
    return Model(row["slug"], row["price"], tuple(Leg(*leg) for leg in row["legs"]))


async def get_model(conn: Connection, slug: str) -> Model | None:
    row = await conn.fetchrow(_QUERY + " WHERE m.slug = $1 GROUP BY m.slug", slug)
    return _model(row) if row else None


async def list_models(conn: Connection) -> list[Model]:
    return [_model(row) for row in await conn.fetch(_QUERY + " GROUP BY m.slug ORDER BY m.slug")]
