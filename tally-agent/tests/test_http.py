"""HTTP layer: authentication, delegation, rate limiting, headers. Sync TestClient."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import KEYS

pytestmark = pytest.mark.usefixtures("dbready")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def auth(kind):
    return {"Authorization": f"Bearer {KEYS[kind]}"}


def test_requires_key(client):
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_me_and_catalogue(client):
    me = client.get("/v1/me", headers=auth("junior")).json()
    assert me["role"] == "junior" and [c["company_id"] for c in me["companies"]] == [1]
    ids = {r["id"] for r in client.get("/v1/reports", headers=auth("junior")).json()}
    assert "party_details" not in ids and "trial_balance" in ids


def test_ask_over_http(client):
    r = client.post("/v1/ask", headers=auth("partner"),
                    json={"question": "How much does Acme owe?", "company_id": 1})
    assert r.status_code == 200, r.text
    assert "₹1,50,000.00 Dr" in r.json()["answer"]


def test_cross_company_is_403(client):
    r = client.post("/v1/ask", headers=auth("partner"),
                    json={"question": "trial balance", "company_id": 2})
    assert r.status_code == 403


def test_delegation(client):
    h = {**auth("svc"), "X-Act-As": "tg:333"}
    me = client.get("/v1/me", headers=h).json()
    assert me["id"] == "junior.test via svc.n8n" and me["role"] == "junior"
    # A non-delegating key cannot impersonate.
    assert client.get("/v1/me", headers={**auth("junior"), "X-Act-As": "tg:111"}).status_code == 401
    # Unknown telegram users get nothing.
    assert client.get("/v1/me", headers={**auth("svc"), "X-Act-As": "tg:999"}).status_code == 401


def test_telegram_bridge_forces_masking_even_for_partner(client):
    h = {**auth("tg"), "X-Act-As": "tg:111"}          # 111 is the partner
    me = client.get("/v1/me", headers=h).json()
    assert me["role"] == "partner" and me["may_unmask"] is False
    r = client.post("/v1/ask", headers=h, json={"question": "which invoices are overdue",
                                                "company_id": 1}).json()
    assert "Borealis" not in str(r) and "Party " in str(r["rows"])


def test_question_length_capped(client):
    r = client.post("/v1/ask", headers=auth("partner"), json={"question": "x" * 1001, "company_id": 1})
    assert r.status_code == 422


def test_security_headers_and_no_docs(client):
    r = client.get("/", headers=auth("partner"))
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404


def test_static_allow_list(client):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/..%2Fmain.py").status_code == 404


def test_health_reports_degraded_without_model(client):
    r = client.get("/healthz").json()
    assert r["db"] and r["redis"] and r["status"] == "degraded"
    assert r["sync"]["companies"] >= 2 and r["sync"]["failed"] == 0
    assert "White Design" not in str(r)            # aggregates only, never names


def test_rate_limit(client, monkeypatch):
    monkeypatch.setattr(app.state.limiter, "_limit", 3)
    codes = [client.get("/v1/reports", headers=auth("senior")).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and 429 in codes[3:]


def test_removing_a_user_revokes_access_without_restart(client):
    import json
    import os
    import time
    from app.config import settings
    path = settings.users_file
    original = open(path).read()
    assert client.get("/v1/me", headers=auth("junior")).status_code == 200
    try:
        users = [u for u in json.loads(original) if u["id"] != "junior.test"]
        with open(path, "w") as f:
            json.dump(users, f)
        os.utime(path, ns=(time.time_ns(), time.time_ns() + 1_000_000))
        assert client.get("/v1/me", headers=auth("junior")).status_code == 401
        assert client.get("/v1/me", headers=auth("partner")).status_code == 200
        # A broken edit must NOT fall back to the old list (which would keep a
        # just-removed person in): everyone is refused until it is fixed.
        for broken in ['{not json', json.dumps(users)[:-1] + ',]',
                       json.dumps([{**users[0], "companies": 1}]),
                       json.dumps([{**users[0], "key_sha256": None}]),
                       json.dumps([{**users[0], "may_unmaks": True}])]:
            with open(path, "w") as f:
                f.write(broken)
            os.utime(path, ns=(time.time_ns(), time.time_ns() + 2_000_000))
            r = client.get("/v1/me", headers=auth("partner"))
            assert r.status_code == 503, broken
            assert client.get("/healthz").json()["users_file"] == "invalid"
    finally:
        with open(path, "w") as f:
            f.write(original)
        os.utime(path, ns=(time.time_ns(), time.time_ns() + 3_000_000))


def test_unfilled_example_entries_are_skipped(tmp_path):
    import json
    from app.auth import UserStore
    p = tmp_path / "users.json"
    p.write_text(json.dumps([
        {"id": "todo", "role": "partner", "companies": [1], "telegram_id": "42", "key_sha256": "REPLACE"},
        {"id": "real", "role": "junior", "companies": [1], "key_sha256": "a" * 64},
    ]))
    store = UserStore.load(p)
    assert set(store.by_id) == {"real"} and "42" not in store.by_telegram


def test_shipped_example_file_loads():
    from app.auth import UserStore
    store = UserStore.load("config/users.example.json")
    assert store.by_id == {} and store.by_telegram == {}


def test_bridge_limited_to_its_delegation_kind(client):
    # The Telegram bridge may act only as "tg:" users, not pick any user id.
    assert client.get("/v1/me", headers={**auth("tg"), "X-Act-As": "user:partner.test"}).status_code == 401
    assert client.get("/v1/me", headers={**auth("tg"), "X-Act-As": "tg:111"}).status_code == 200


def test_bridge_rate_limit_is_shared_across_impersonated_users(client, monkeypatch):
    import redis as sync_redis
    from app.config import settings
    r = sync_redis.from_url(settings.redis_url)
    for k in r.scan_iter("rl:*"):
        r.delete(k)
    monkeypatch.setattr(app.state.limiter, "_limit", 3)
    codes = [client.get("/v1/me", headers={**auth("svc"), "X-Act-As": f"tg:{t}"}).status_code
             for t in ("111", "333", "111", "333", "111")]
    assert codes[:3] == [200, 200, 200] and 429 in codes[3:]


def test_ask_without_company_uses_users_first_company(client):
    r = client.post("/v1/ask", headers=auth("partner"), json={"question": "trial balance"})
    assert r.status_code == 200 and r.json()["report_id"] == "trial_balance"


def test_non_finite_numbers_never_crash(client):
    for body in ('{"report_id":"top_parties","company_id":1,"params":{"date_from":"2024-04-01",'
                 '"date_to":"2025-03-31","limit":1e999}}',
                 '{"report_id":"voucher_search","company_id":1,"params":{"date_from":"2024-04-01",'
                 '"date_to":"2025-03-31","min_amount":1e999}}'):
        r = client.post("/v1/report", content=body,
                        headers={**auth("partner"), "Content-Type": "application/json"})
        assert r.status_code in (200, 400), r.text      # dropped or rejected -- never a 500
