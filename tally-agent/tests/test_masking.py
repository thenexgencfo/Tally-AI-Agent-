import pytest

from app.masking import InMemoryTokenVault, MaskingEngine, NameIndex, RedisTokenVault

PARTIES = ["Acme Interiors Pvt Ltd", "Borealis Furnishings LLP", "Citrine Realty", "Deepak Stationers"]
OTHERS = ["Design Fees", "Office Rent", "HDFC Bank A/c", "Salaries"]


@pytest.fixture
def engine():
    return MaskingEngine(InMemoryTokenVault())


@pytest.fixture
def names():
    return NameIndex.build(PARTIES, OTHERS)


async def test_round_trip(engine, names):
    q = "Does Acme Interiors Pvt Ltd (PAN AABCA1234K) owe more than Citrine Realty?"
    m = await engine.mask_text(q, "s", names=names)
    assert "Acme" not in m.text and "AABCA1234K" not in m.text and "Citrine" not in m.text
    assert await engine.unmask_text(m.text, "s") == q


async def test_tokens_stable_within_session(engine, names):
    a = await engine.mask_text("Acme Interiors Pvt Ltd", "s", names=names)
    b = await engine.mask_text("balance of acme interiors pvt ltd", "s", names=names)
    assert a.text == "[PARTY_1]" and "[PARTY_1]" in b.text


async def test_alias_resolves_to_canonical_name(engine, names):
    m = await engine.mask_text("how much does acme owe", "s", names=names)
    assert m.text == "how much does [PARTY_1] owe"
    assert await engine.unmask_text("[PARTY_1]", "s") == "Acme Interiors Pvt Ltd"


async def test_non_party_ledgers_and_lookalikes_untouched(engine, names):
    for text in ["Design Fees for the year", "Acmeson Traders", "Office Rent paid"]:
        assert (await engine.mask_text(text, "s", names=names)).text == text


async def test_sessions_are_isolated(engine, names):
    m = await engine.mask_text("Acme Interiors Pvt Ltd", "alice:1", names=names)
    assert await engine.unmask_text(m.text, "bob:1") == m.text   # bob cannot resolve alice's token


async def test_mask_rows(engine, names):
    rows = [{"party": "Acme Interiors Pvt Ltd", "it_pan": "AABCA1234K", "gstn": "27AABCA1234K1Z5",
             "outstanding": "150000.00", "ledger": "Design Fees", "bank_account_number": "50200012345678",
             "narration": "Paid by Borealis Furnishings LLP, ref AABCA1234K", "age_days": 320}]
    [out] = await engine.mask_rows(rows, "s", names=names)
    assert out["party"] == "[PARTY_1]"
    assert out["it_pan"].startswith("[PAN_") and out["gstn"].startswith("[GSTIN_")
    assert out["bank_account_number"].startswith("[ACCOUNT_")
    assert out["outstanding"] == "150000.00" and out["age_days"] == 320
    assert out["ledger"] == "Design Fees"
    assert "Borealis" not in out["narration"] and "AABCA1234K" not in out["narration"]


async def test_masked_display_for_unprivileged(engine, names):
    m = await engine.mask_text("Acme Interiors Pvt Ltd PAN AABCA1234K mail a@acme.example", "s", names=names)
    shown = await engine.redisplay(m.text, "s", may_unmask=False)
    assert shown == "Party 1 PAN XXXXXX234K mail [email withheld]"


async def test_redis_vault_expires(redis_client, names):
    eng = MaskingEngine(RedisTokenVault(redis_client, ttl_seconds=60))
    m = await eng.mask_text("Acme Interiors Pvt Ltd", "u:abc12345", names=names)
    assert await eng.unmask_text(m.text, "u:abc12345") == "Acme Interiors Pvt Ltd"
    ttl = await redis_client.ttl("mask:u:abc12345:r")
    assert 0 < ttl <= 60
