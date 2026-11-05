"""
Database access.

Every read happens inside `company_scope`, which sets the transaction-local
GUC that row-level security reads. Transaction-local matters: with a
connection pool, a session-level setting would leak one user's company access
to the next request that borrowed the same connection.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Sequence

import asyncpg

from .config import settings

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.pg_dsn,
            min_size=settings.pg_pool_min,
            max_size=settings.pg_pool_max,
            command_timeout=settings.statement_timeout_ms / 1000,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def company_scope(
    company_ids: Sequence[int],
) -> AsyncIterator[asyncpg.Connection]:
    """Open a transaction scoped to exactly the companies this user may read."""
    if not company_ids:
        raise PermissionError("no company access")
    ids = [int(c) for c in company_ids]  # int() guards against injection
    pool = await get_pool()
    async with pool.acquire() as con:
        async with con.transaction():
            await con.execute(
                "SELECT set_config('app.allowed_companies', $1, true)",
                ",".join(str(i) for i in ids),
            )
            await con.execute(
                f"SET LOCAL statement_timeout = {int(settings.statement_timeout_ms)}"
            )
            yield con


async def audit_insert(**fields) -> None:
    """Append one row to the audit trail. Never raises into the request path."""
    pool = await get_pool()
    cols = list(fields.keys())
    placeholders = ", ".join(f"${i+1}" for i in range(len(cols)))
    sql = (
        f"INSERT INTO audit.query_log ({', '.join(cols)}) VALUES ({placeholders})"
    )
    async with pool.acquire() as con:
        await con.execute(sql, *fields.values())
