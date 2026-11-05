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
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import redis.asyncio as aioredis
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

ROLES = ("junior", "senior", "partner")
log = logging.getLogger("tally_agent")


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
    # Which kinds of X-Act-As this principal may use ("tg" = Telegram ID).
    delegate_via: tuple[str, ...] = ("tg",)
    telegram_id: str | None = None
    acting_via: str | None = None

    @property
    def audit_id(self) -> str:
        return f"{self.id} via {self.acting_via}" if self.acting_via else self.id


class AuthError(Exception):
    pass


class _UserRow(BaseModel):
    """One users.json entry. Unknown or mistyped fields are errors, not ignored:
    a misspelt "may_unmask" must not silently become False/True."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, max_length=128)
    role: str
    companies: list[int] = Field(default_factory=list)
    may_unmask: bool = False
    can_delegate: bool = False
    force_masked: bool = False
    delegate_via: list[str] = Field(default_factory=lambda: ["tg"])
    telegram_id: str | int | None = None
    key_sha256: str = ""
    disabled: bool = False

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        if v not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        return v

    @field_validator("delegate_via")
    @classmethod
    def _via(cls, v: list[str]) -> list[str]:
        if not set(v) <= {"tg", "user"}:
            raise ValueError('delegate_via may contain only "tg" and "user"')
        return v

    @field_validator("key_sha256")
    @classmethod
    def _key(cls, v: str) -> str:
        if v and not v.upper().startswith("REPLACE"):
            if len(v) != 64 or any(c not in "0123456789abcdefABCDEF" for c in v):
                raise ValueError("key_sha256 must be 64 hex characters")
        return v


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass
class UserStore:
    users: list[tuple[str, User]] = field(default_factory=list)   # (key_sha256, user)
    by_id: dict[str, User] = field(default_factory=dict)
    by_telegram: dict[str, User] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "UserStore":
        """Parse and validate the whole file. Raises ValueError on ANY problem;
        callers must then refuse access rather than fall back to a stale list."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"users file is not valid JSON: {exc.msg} (line {exc.lineno})") from exc
        if not isinstance(data, list):
            raise ValueError("users file must be a JSON list")
        store = cls()
        seen_ids: set[str] = set()
        seen_keys: set[str] = set()
        for i, raw in enumerate(data):
            try:
                row = _UserRow.model_validate(raw)
            except ValidationError as exc:
                who = raw.get("id", f"entry {i + 1}") if isinstance(raw, dict) else f"entry {i + 1}"
                first = exc.errors()[0]
                raise ValueError(f"users file, {who}: {'.'.join(map(str, first['loc']))}: "
                                 f"{first['msg']}") from None
            if row.id in seen_ids:
                raise ValueError(f"users file: duplicate id {row.id!r}")
            seen_ids.add(row.id)
            if row.disabled:
                continue
            digest = row.key_sha256
            if digest.upper().startswith("REPLACE"):
                # Unfilled example entry: skip it (and never let it delegate or
                # be reached by Telegram ID) rather than refusing to start.
                log.warning("users file: %s has no key yet, skipped", row.id)
                continue
            tg = str(row.telegram_id) if row.telegram_id not in (None, "") else None
            user = User(
                id=row.id, role=row.role, companies=tuple(row.companies),
                may_unmask=row.may_unmask, can_delegate=row.can_delegate,
                force_masked=row.force_masked, delegate_via=tuple(row.delegate_via),
                telegram_id=tg,
            )
            if digest:
                if digest.lower() in seen_keys:
                    raise ValueError(f"users file: {row.id} reuses another user's key")
                seen_keys.add(digest.lower())
                store.users.append((digest.lower(), user))
            store.by_id[user.id] = user
            if tg:
                if tg in store.by_telegram:
                    raise ValueError(f"users file: telegram_id {tg} is mapped twice")
                store.by_telegram[tg] = user
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
        if kind not in principal.delegate_via:
            raise AuthError(f"this key may not act as '{kind}:' users")
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
