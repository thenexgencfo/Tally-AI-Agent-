"""
Reversible token vault.

Tokens are stable within a session so the model can reason consistently
("[PARTY_1] owes more than [PARTY_2]") and the answer can be mapped back.
They are scoped per session and expire, so nothing accumulates: the vault is
a cache, never a system of record, and may be flushed at any time.
"""
from __future__ import annotations

from typing import Protocol

import redis.asyncio as aioredis


def _fwd(session: str) -> str:
    return f"mask:{session}:f"


def _rev(session: str) -> str:
    return f"mask:{session}:r"


def _ctr(session: str) -> str:
    return f"mask:{session}:c"


class TokenVault(Protocol):
    async def token_for(self, session: str, entity: str, value: str) -> str: ...
    async def value_for(self, session: str, token: str) -> str | None: ...
    async def clear(self, session: str) -> None: ...


class RedisTokenVault:
    def __init__(self, client: aioredis.Redis, ttl_seconds: int = 3600) -> None:
        self._r = client
        self._ttl = ttl_seconds

    async def token_for(self, session: str, entity: str, value: str) -> str:
        field = f"{entity}\x00{value}"
        existing = await self._r.hget(_fwd(session), field)
        if existing:
            return existing.decode() if isinstance(existing, bytes) else existing

        n = await self._r.hincrby(_ctr(session), entity, 1)
        token = f"[{entity}_{n}]"
        # If another writer won the race, keep theirs so tokens stay stable.
        won = await self._r.hsetnx(_fwd(session), field, token)
        if not won:
            current = await self._r.hget(_fwd(session), field)
            token = current.decode() if isinstance(current, bytes) else current
        else:
            await self._r.hset(_rev(session), token, value)

        for key in (_fwd(session), _rev(session), _ctr(session)):
            await self._r.expire(key, self._ttl)
        return token

    async def value_for(self, session: str, token: str) -> str | None:
        v = await self._r.hget(_rev(session), token)
        if v is None:
            return None
        return v.decode() if isinstance(v, bytes) else v

    async def clear(self, session: str) -> None:
        await self._r.delete(_fwd(session), _rev(session), _ctr(session))


class InMemoryTokenVault:
    """Test double. Not for production -- no expiry, no sharing across workers."""

    def __init__(self) -> None:
        self._fwd: dict[str, dict[str, str]] = {}
        self._rev: dict[str, dict[str, str]] = {}
        self._ctr: dict[str, dict[str, int]] = {}

    async def token_for(self, session: str, entity: str, value: str) -> str:
        fwd = self._fwd.setdefault(session, {})
        field = f"{entity}\x00{value}"
        if field in fwd:
            return fwd[field]
        ctr = self._ctr.setdefault(session, {})
        ctr[entity] = ctr.get(entity, 0) + 1
        token = f"[{entity}_{ctr[entity]}]"
        fwd[field] = token
        self._rev.setdefault(session, {})[token] = value
        return token

    async def value_for(self, session: str, token: str) -> str | None:
        return self._rev.get(session, {}).get(token)

    async def clear(self, session: str) -> None:
        self._fwd.pop(session, None)
        self._rev.pop(session, None)
        self._ctr.pop(session, None)
