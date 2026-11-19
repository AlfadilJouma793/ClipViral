"""Accounts, teams, plans, usage, API keys and Stripe billing."""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from clipviral import accounts, billing, brands, config, jobs, main


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    (tmp_path / "jobs").mkdir()
    monkeypatch.setattr(brands, "BRANDS_DIR", tmp_path / "brands")
    monkeypatch.setattr(accounts, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(jobs, "enqueue", lambda *a, **k: None)
    accounts.init()
    accounts._fails.clear()
    return tmp_path


def client():
    return TestClient(main.app)


def signup(c, email, pw="password123", **kw):
    r = c.post("/api/auth/signup", json={"email": email, "password": pw, **kw})
    assert r.status_code == 200, r.text
    return c.get("/api/me").json()


def test_signup_login_logout_and_validation(env):
    c = client()
    assert c.get("/api/me").status_code == 401
    assert c.get("/api/jobs").status_code == 401
    assert c.post("/api/auth/signup", json={"email": "bad", "password": "password123"}).status_code == 400
    assert c.post("/api/auth/signup", json={"email": "a@b.co", "password": "short"}).status_code == 400
    me = signup(c, "Ann@Example.com", name="Ann")
    assert me["user"]["email"] == "ann@example.com" and me["role"] == "owner"
    assert me["usage"]["plan"]["key"] == "free" and me["usage"]["minutes_limit"] == 60
    assert c.post("/api/auth/signup", json={"email": "ann@example.com", "password": "password123"}).status_code == 400
    # cookie is HttpOnly
    c2 = client()
    r = c2.post("/api/auth/login", json={"email": "ann@example.com", "password": "password123"})
    assert r.status_code == 200 and "httponly" in r.headers["set-cookie"].lower()
    assert c2.post("/api/auth/logout").status_code == 200
    assert c2.get("/api/me").status_code == 401
    # stored password is a scrypt hash, never plain text
    with accounts.db() as con:
        h = con.execute("SELECT pw_hash FROM users").fetchone()[0]
    assert h.startswith("scrypt$") and "password123" not in h


def test_login_throttling(env):
    c = client()
    signup(c, "t@example.com")
    c2 = client()
    for _ in range(8):
        assert c2.post("/api/auth/login", json={"email": "t@example.com", "password": "nope-nope"}).status_code == 400
    r = c2.post("/api/auth/login", json={"email": "t@example.com", "password": "password123"})
    assert r.status_code == 400 and "Too many" in r.json()["detail"]


def test_workspaces_are_isolated(env):
    a, b = client(), client()
    signup(a, "a@example.com")
    signup(b, "b@example.com")
    ws_a = a.get("/api/me").json()["workspace"]["id"]
    job = jobs.create("secret", {}, workspace_id=ws_a)
    jobs.update(job["id"], status="done")
    assert [j["id"] for j in a.get("/api/jobs").json()] == [job["id"]]
    assert b.get("/api/jobs").json() == []
    for path in (f"/api/jobs/{job['id']}", f"/api/jobs/{job['id']}/export.zip",
                 f"/api/jobs/{job['id']}/clips/c01/video", f"/api/jobs/{job['id']}/clips/c01/words"):
        assert b.get(path).status_code == 404, path
    assert b.delete(f"/api/jobs/{job['id']}").status_code == 404
    assert jobs.load(job["id"]) is not None
    br = a.post("/api/brands", json={"name": "A brand"}).json()
    assert b.get("/api/brands").json() == []
    assert b.patch(f"/api/brands/{br['id']}", json={"name": "x"}).status_code == 404
    assert b.delete(f"/api/brands/{br['id']}").status_code == 404


def test_first_user_adopts_projects_created_before_accounts(env):
    old = jobs.create("legacy", {})
    signup(client(), "first@example.com")
    assert jobs.load(old["id"])["workspace_id"]


def test_team_invites_seats_and_roles(env):
    owner = client()
    signup(owner, "boss@example.com")
    ws = owner.get("/api/me").json()["workspace"]["id"]
    # free plan: 1 seat -> no invites
    r = owner.post("/api/team/invites", json={"email": "mate@example.com"})
    assert r.status_code == 402
    accounts.set_plan(ws, "creator")  # 2 seats
    r = owner.post("/api/team/invites", json={"email": "mate@example.com"})
    assert r.status_code == 200
    link = r.json()["link"]
    token = link.split("invite=")[1]
    assert owner.post("/api/team/invites", json={"email": "third@example.com"}).status_code == 402  # seats full
    assert client().get(f"/api/invites/{token}").json()["email"] == "mate@example.com"
    # wrong email can't use it
    assert client().post("/api/auth/signup", json={"email": "other@example.com", "password": "password123",
                                                   "invite": token}).status_code == 400
    mate = client()
    signup(mate, "mate@example.com", invite=token)
    me = mate.get("/api/me").json()
    assert me["workspace"]["id"] == ws and me["role"] == "member"
    assert len(me["workspaces"]) == 2
    # shared projects
    job = jobs.create("shared", {}, workspace_id=ws)
    assert job["id"] in [j["id"] for j in mate.get("/api/jobs").json()]
    # members can't manage the team, billing or keys
    assert mate.post("/api/team/invites", json={"email": "x@example.com"}).status_code == 403
    assert mate.post("/api/billing/checkout", json={"plan": "pro"}).status_code == 403
    assert mate.get("/api/keys").status_code == 403
    # used invite can't be reused
    assert client().get(f"/api/invites/{token}").status_code == 404
    # owner removes member -> member falls back to own workspace
    mate_id = me["user"]["id"]
    assert owner.delete(f"/api/team/members/{mate_id}").status_code == 200
    me2 = mate.get("/api/me").json()
    assert me2["workspace"]["id"] != ws
    assert mate.get(f"/api/jobs/{job['id']}").status_code == 404
    owner_id = owner.get("/api/me").json()["user"]["id"]
    assert owner.delete(f"/api/team/members/{owner_id}").status_code == 400


def test_usage_quota_and_plan_limits(env):
    c = client()
    signup(c, "q@example.com")
    ws = c.get("/api/me").json()["workspace"]["id"]
    with pytest.raises(accounts.QuotaError, match="30 minutes"):
        accounts.charge(ws, "j1", 45)  # free: max 30-minute videos
    accounts.charge(ws, "j1", 25)
    accounts.charge(ws, "j1", 25)  # same job again: not double-charged
    assert accounts.minutes_used(ws) == 25
    accounts.charge(ws, "j2", 25)
    with pytest.raises(accounts.QuotaError, match="left"):
        accounts.charge(ws, "j3", 20)  # 50 used of 60
    accounts.refund(ws, "j2")
    assert accounts.minutes_used(ws) == 25
    # brand template limit
    assert c.post("/api/brands", json={"name": "One"}).status_code == 200
    assert c.post("/api/brands", json={"name": "Two"}).status_code == 402
    # out of minutes -> new projects refused up front
    accounts.charge(ws, "j4", 30)
    accounts.charge(ws, "j5", 5)
    r = c.post("/api/jobs", data={"url": "https://example.com/a.mp4", "rights": "true"})
    assert r.status_code == 402


def test_free_plan_watermark_and_quota_in_pipeline(env, monkeypatch):
    from clipviral.pipeline import render as rmod
    c = client()
    signup(c, "w@example.com")
    ws = c.get("/api/me").json()["workspace"]["id"]
    seen = {}
    monkeypatch.setattr(rmod, "render_clip", lambda d, src, meta, clip, words, opts: seen.update(opts) or {"duration": 1})
    job = jobs.create("x", {"watermark": "@mine"}, workspace_id=ws)
    (jobs.job_dir(job["id"]) / "transcript.json").write_text(json.dumps({"words": []}))
    jobs.update(job["id"], source="s.mp4", meta={"duration": 60, "width": 1280, "height": 720, "has_audio": True},
                clips=[{"id": "c01", "start": 0, "end": 10, "style": "bold", "layout": "auto", "accent": "#FFE14D",
                        "position": "lower"}])
    jobs._render_one(job["id"], "c01")
    assert seen["watermark"] == accounts.FREE_WATERMARK
    accounts.set_plan(ws, "starter")
    jobs._render_one(job["id"], "c01")
    assert seen["watermark"] == "@mine"


def test_api_keys_pro_only_bearer_and_revoke(env):
    c = client()
    signup(c, "k@example.com")
    ws = c.get("/api/me").json()["workspace"]["id"]
    assert c.post("/api/keys", json={"name": "zapier"}).status_code == 402
    accounts.set_plan(ws, "pro")
    r = c.post("/api/keys", json={"name": "zapier"})
    assert r.status_code == 200
    key = r.json()["key"]
    assert key.startswith("cv_live_") and len(key) > 40
    assert "key" not in c.get("/api/keys").json()["keys"][0]  # never shown again
    api = client()
    h = {"Authorization": f"Bearer {key}"}
    assert api.get("/api/jobs", headers=h).status_code == 200
    assert api.get("/api/me", headers=h).status_code == 403  # account endpoints need a user
    assert api.get("/api/jobs", headers={"Authorization": "Bearer cv_live_wrong"}).status_code == 401
    accounts.set_plan(ws, "creator")  # downgrade: keys stop working
    assert api.get("/api/jobs", headers=h).status_code == 401
    accounts.set_plan(ws, "pro")
    kid = c.get("/api/keys").json()["keys"][0]["id"]
    assert c.delete(f"/api/keys/{kid}").status_code == 200
    assert api.get("/api/jobs", headers=h).status_code == 401
    with accounts.db() as con:
        assert key not in json.dumps([dict(r) for r in con.execute("SELECT * FROM api_keys").fetchall()])


# ---------------------------------------------------------------- Stripe
def _sign(payload: bytes, secret: str) -> str:
    t = int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


@pytest.fixture
def stripe_env(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setenv("STRIPE_PRICE_STARTER", "price_s")
    monkeypatch.setenv("STRIPE_PRICE_CREATOR", "price_c")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_p")


def _event(eid, etype, obj):
    return json.dumps({"id": eid, "object": "event", "type": etype, "data": {"object": obj}}).encode()


def test_checkout_session_parameters(env, stripe_env, monkeypatch):
    import stripe
    c = client()
    signup(c, "pay@example.com")
    ws = c.get("/api/me").json()["workspace"]["id"]
    captured = {}

    def fake_create(**params):
        captured.update(params)
        return stripe.StripeObject.construct_from({"url": "https://checkout.stripe.com/c/x"}, "k")
    monkeypatch.setattr(stripe.checkout.Session, "create", fake_create)
    r = c.post("/api/billing/checkout", json={"plan": "creator"})
    assert r.status_code == 200 and r.json()["url"].startswith("https://checkout.stripe.com/")
    assert captured["mode"] == "subscription"
    assert captured["line_items"] == [{"price": "price_c", "quantity": 1}]
    assert captured["client_reference_id"] == ws and captured["customer_email"] == "pay@example.com"
    assert captured["subscription_data"]["metadata"] == {"workspace_id": ws, "plan": "creator"}
    assert c.post("/api/billing/checkout", json={"plan": "free"}).status_code == 400
    assert c.post("/api/billing/portal").status_code == 400  # no customer yet


def test_webhook_signature_and_plan_sync(env, stripe_env):
    c = client()
    signup(c, "hook@example.com")
    ws = c.get("/api/me").json()["workspace"]["id"]
    api = client()
    # bad signature rejected
    body = _event("evt_1", "checkout.session.completed", {"object": "checkout.session", "mode": "subscription",
                  "client_reference_id": ws, "customer": "cus_1", "subscription": "sub_1",
                  "metadata": {"workspace_id": ws, "plan": "pro"}})
    assert api.post("/api/billing/webhook", content=body, headers={"stripe-signature": "t=1,v1=bad"}).status_code == 400
    assert accounts.plan_of(ws)["key"] == "free"
    # valid signature -> plan upgraded, customer stored
    r = api.post("/api/billing/webhook", content=body, headers={"stripe-signature": _sign(body, "whsec_test")})
    assert r.status_code == 200, r.text
    w = accounts.get_workspace(ws)
    assert w["plan"] == "pro" and w["stripe_customer_id"] == "cus_1" and w["stripe_subscription_id"] == "sub_1"
    # plan change via subscription.updated (price -> plan), matched by customer
    upd = _event("evt_2", "customer.subscription.updated", {"object": "subscription", "id": "sub_1",
                 "customer": "cus_1", "status": "active", "metadata": {},
                 "items": {"object": "list", "data": [{"object": "subscription_item", "price": {"object": "price", "id": "price_s"}}]}})
    assert api.post("/api/billing/webhook", content=upd, headers={"stripe-signature": _sign(upd, "whsec_test")}).status_code == 200
    assert accounts.plan_of(ws)["key"] == "starter"
    # duplicate delivery is ignored
    accounts.set_plan(ws, "creator")
    assert api.post("/api/billing/webhook", content=upd, headers={"stripe-signature": _sign(upd, "whsec_test")}).status_code == 200
    assert accounts.plan_of(ws)["key"] == "creator"
    # cancellation -> free
    dele = _event("evt_3", "customer.subscription.deleted", {"object": "subscription", "id": "sub_1", "customer": "cus_1",
                  "status": "canceled", "metadata": {"workspace_id": ws}})
    assert api.post("/api/billing/webhook", content=dele, headers={"stripe-signature": _sign(dele, "whsec_test")}).status_code == 200
    assert accounts.plan_of(ws)["key"] == "free"


def test_webhook_without_secret_is_refused(env, monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    assert client().post("/api/billing/webhook", content=b"{}").status_code == 503
