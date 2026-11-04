"""
Test configuration.

DB tests need the seeded database:  RO_PASS=devro OWNER_PASS=devowner SEED=1 ./scripts/setup_db.sh
(scripts/test.sh does this). Redis db 15 is used and flushed per test.
Environment is set BEFORE app modules are imported, because settings are
read at import time.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

KEYS = {
    "partner": "tak_test_partner_key_0001",
    "senior": "tak_test_senior_key_0002",
    "junior": "tak_test_junior_key_0003",
    "other": "tak_test_other_company_0004",
    "svc": "tak_test_service_key_0005",
    "tg": "tak_test_telegram_bridge_0006",
}
_tmp = Path(tempfile.mkdtemp(prefix="tally-agent-test-"))
_users = [
    {"id": "partner.test", "role": "partner", "companies": [1], "may_unmask": True,
     "telegram_id": "111", "key_sha256": hashlib.sha256(KEYS["partner"].encode()).hexdigest()},
    {"id": "senior.test", "role": "senior", "companies": [1], "may_unmask": True,
     "key_sha256": hashlib.sha256(KEYS["senior"].encode()).hexdigest()},
    {"id": "junior.test", "role": "junior", "companies": [1], "may_unmask": False,
     "telegram_id": "333", "key_sha256": hashlib.sha256(KEYS["junior"].encode()).hexdigest()},
    {"id": "other.test", "role": "partner", "companies": [2], "may_unmask": True,
     "key_sha256": hashlib.sha256(KEYS["other"].encode()).hexdigest()},
    {"id": "svc.n8n", "role": "junior", "companies": [], "can_delegate": True,
     "key_sha256": hashlib.sha256(KEYS["svc"].encode()).hexdigest()},
    {"id": "svc.telegram", "role": "junior", "companies": [], "can_delegate": True,
     "force_masked": True, "key_sha256": hashlib.sha256(KEYS["tg"].encode()).hexdigest()},
]
(_tmp / "users.json").write_text(json.dumps(_users))

os.environ.update({
    "PG_DSN": os.environ.get("TEST_PG_DSN", "postgresql://tally_ro:devro@127.0.0.1:5432/tally"),
    "REDIS_URL": os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/15"),
    "USERS_FILE": str(_tmp / "users.json"),
    "LLM_ENABLED": "false",
    "REFERENCE_DATE": "2025-03-31",
    "AUDIT_HASH_SALT": "test-salt",
    "RATE_LIMIT_PER_MINUTE": "1000",
})
OWNER_DSN = os.environ.get("TEST_OWNER_DSN", "postgresql://tally_owner:devowner@127.0.0.1:5432/tally")

import asyncpg  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
import redis.asyncio as aioredis  # noqa: E402

from app import db  # noqa: E402
from app.agent.dates import resolve_period  # noqa: E402
from app.agent.llm import LLMUnavailable  # noqa: E402
from app.agent.planner import rules_plan  # noqa: E402
from app.auth import UserStore  # noqa: E402
from app.config import settings  # noqa: E402

USERS = UserStore.load(settings.users_file)


def user(kind: str):
    return USERS.authenticate(KEYS[kind])


class FakeLLM:
    """Records every prompt. `plan` / `narrative` may be values or callables."""

    model = "fake-llm"

    def __init__(self, plan=None, narrative: object = "", fail: bool = False) -> None:
        self.plan = plan
        self.narrative = narrative
        self.fail = fail
        self.prompts: list[tuple[str, str]] = []

    async def chat_json(self, system: str, user: str, schema: dict) -> dict:
        self.prompts.append((system, user))
        if self.fail:
            raise LLMUnavailable("down")
        if callable(self.plan):
            return self.plan(user)
        return self.plan or {"report_id": "none", "params": {}, "clarification": "?"}

    async def chat_text(self, system: str, user: str, max_tokens: int = 200) -> str:
        self.prompts.append((system, user))
        if self.fail:
            raise LLMUnavailable("down")
        return self.narrative(user) if callable(self.narrative) else str(self.narrative)

    async def health(self) -> bool:
        return not self.fail


def rules_router(role: str = "partner"):
    """A 'model' that routes like the keyword planner -- realistic plans for e2e tests."""
    import datetime as dt

    def _plan(masked_question: str) -> dict:
        p = rules_plan(masked_question, role, resolve_period(masked_question, dt.date(2025, 3, 31)))
        return {"report_id": p.report_id or "none", "params": p.params,
                "clarification": p.clarification or ""}
    return _plan


async def _db_ok() -> bool:
    try:
        con = await asyncpg.connect(settings.pg_dsn, timeout=3)
        await con.close()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest_asyncio.fixture
async def redis_client():
    r = aioredis.from_url(settings.redis_url)
    try:
        await r.ping()
    except Exception:  # noqa: BLE001
        pytest.skip("redis not available")
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest_asyncio.fixture
async def dbready():
    if not await _db_ok():
        pytest.skip("seeded postgres not available (run scripts/setup_db.sh with SEED=1)")
    yield
    await db.close_pool()   # the pool is bound to this test's event loop


@pytest_asyncio.fixture
async def owner_con(dbready):
    con = await asyncpg.connect(OWNER_DSN)
    yield con
    await con.close()
