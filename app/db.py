"""Postgres connection pool — the one place asyncpg is touched directly."""
import json

import asyncpg

from app.config import settings

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    # Auto-encode/decode jsonb <-> dict so trace payloads round-trip without
    # manual json.dumps/loads at every call site. default=str covers values
    # (e.g. datetimes from shipment rows) that land in a trace payload.
    await conn.set_type_codec(
        "jsonb",
        encoder=lambda value: json.dumps(value, default=str),
        decoder=json.loads,
        schema="pg_catalog",
        format="text",
    )


async def connect() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=10, init=_init_connection)
    return _pool


async def disconnect() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized — call connect() at startup first.")
    return _pool
