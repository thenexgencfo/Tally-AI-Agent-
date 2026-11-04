"""
Authentication and authorisation.

Each person gets their own API key. Only the SHA-256 of a key is stored, so
the users file is not a credential store if it leaks. Keys are compared in
constant time.

A service principal (the n8n Telegram bridge) may act on behalf of a mapped
user with `X-Act-As: tg:<telegram id>` or `user:<id>`. The request then runs
with that person's permissions -- never the service's -- and the audit row
records both.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import redis.asyncio as aioredis

ROLES = ("junior", "senior", "partner")


@dataclass(frozen=True)
class User:
    id: str
    role: str
    companies: tuple[int, ...]
    may_unmask: bool = False
    can_delegate: bool = False
    # Delegated requests through this principal never show real identifiers,
    # whatever the target user's role. Set it on any bridge whose answers
    # leave your network (Telegram, WhatsApp).
    force_masked: bool = False
    telegram_id: str | None = None
    acting_via: str | None = None

    @property
    def audit_id(self) -> str:
        return f"{self.id} via {self.acting_via}" if self.acting_via else self.id


class AuthError(Exception):
    pass


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass
class UserStore:
    users: list[tuple[str, User]] = field(default_factory=list)   # (key_sha256, user)
    by_id: dict[str, User] = field(default_factory=dict)
    by_telegram: dict[str, User] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "UserStore":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        store = cls()
        for row in data:
            role = row["role"]
            if role not in ROLES:
                raise ValueError(f"user {row['id']}: unknown role {role!r}")
            user = User(
                id=row["id"],
                role=role,
                companies=tuple(int(c) for c in row.get("companies", [])),
                may_unmask=bool(row.get("may_unmask", False)),
                can_delegate=bool(row.get("can_delegate", False)),
                force_masked=bool(row.get("force_masked", False)),
                telegram_id=str(row["telegram_id"]) if row.get("telegram_id") else None,
            )
            if row.get("disabled"):
                continue
            digest = row.get("key_sha256", "")
            if digest:
                if len(digest) != 64:
                    raise ValueError(f"user {user.id}: key_sha256 must be 64 hex chars")
                store.users.append((digest.lower(), user))
            store.by_id[user.id] = user
            if user.telegram_id:
                store.by_telegram[user.telegram_id] = user
        return store

    def authenticate(self, api_key: str | None, act_as: str | None = None) -> User:
        if not api_key:
            raise AuthError("missing API key")
        digest = hash_key(api_key)
        principal: User | None = None
        # Constant-time comparison against every entry; no early exit.
        for stored, user in self.users:
            if hmac.compare_digest(stored, digest):
                principal = user
        if principal is None:
            raise AuthError("invalid API key")
        if not act_as:
            return principal
        if not principal.can_delegate:
            raise AuthError("this key may not act on behalf of other users")
        kind, _, ident = act_as.partition(":")
        target = (self.by_telegram.get(ident) if kind == "tg"
                  else self.by_id.get(ident) if kind == "user" else None)
        if target is None or target.can_delegate:
            raise AuthError("unknown or non-delegable target user")
        return User(**{
            **target.__dict__,
            "acting_via": principal.id,
            "may_unmask": target.may_unmask and not principal.force_masked,
        })


class RateLimiter:
    def __init__(self, client: aioredis.Redis, per_minute: int) -> None:
        self._r = client
        self._limit = per_minute

    async def check(self, user_id: str) -> None:
        bucket = f"rl:{user_id}:{int(time.time() // 60)}"
        n = await self._r.incr(bucket)
        if n == 1:
            await self._r.expire(bucket, 70)
        if n > self._limit:
            raise AuthError("rate limit exceeded; try again in a minute")
