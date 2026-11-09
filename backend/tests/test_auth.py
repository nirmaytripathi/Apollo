"""Accounts: register / login / logout / me, JWT sessions, protected routes, per-user runs. Offline."""

import time

import jwt
import pytest
from fastapi.testclient import TestClient

import api
from core import auth, config, db
from graph import nodes
from sqlalchemy import select
from tools import passages as passages_mod
from verify import claims as claims_mod

from .fakes import fake_embed, fake_llm, fake_search, verify_reply

PW = "correct horse"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'apollo-test.db'}")
    monkeypatch.setattr(config, "LEGACY_USERS_DB", str(tmp_path / "no-legacy-users.db"))
    monkeypatch.setattr(api, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(nodes, "web_search", fake_search())
    monkeypatch.setattr(nodes, "page_content", lambda url, raw, errors: (f"Page text for {url}. It reports a finding.", "trafilatura"))
    monkeypatch.setattr(nodes, "llm_chat", fake_llm(lambda n: '{"complete": true, "gaps": []}'))
    monkeypatch.setattr(passages_mod, "embed", fake_embed)
    monkeypatch.setattr(claims_mod, "embed", fake_embed)
    monkeypatch.setattr(claims_mod, "llm_chat", lambda system, user, **k: verify_reply(user))
    monkeypatch.setattr(claims_mod, "_available_chain", lambda: [("groq", "m")])
    auth._failures.clear()
    api._runs.clear()


def signed_in(email="a@example.com", name="Asha"):
    c = TestClient(api.app)
    r = c.post("/api/auth/register", json={"name": name, "email": email, "password": PW})
    assert r.status_code == 201, r.text
    return c


def test_routes_need_a_session():
    c = TestClient(api.app)
    for method, url in [("get", "/api/runs"), ("post", "/api/runs"), ("get", "/api/system"), ("get", "/api/demo-topics"),
                        ("get", "/api/runs/x/events"), ("get", "/api/auth/me"), ("post", "/api/cache/clear")]:
        assert getattr(c, method)(url).status_code == 401, url


def test_register_signs_in_with_httponly_cookie():
    c = TestClient(api.app)
    r = c.post("/api/auth/register", json={"name": "Asha", "email": "Asha@Example.com", "password": PW})
    assert r.status_code == 201 and r.json()["user"]["email"] == "asha@example.com"
    cookie = r.headers["set-cookie"].lower()
    assert "apollo_session=" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    assert "password" not in r.text.lower()  # neither the password nor its hash is ever returned
    assert c.get("/api/auth/me").json()["user"]["name"] == "Asha"


@pytest.mark.parametrize("body, status", [
    ({"name": "A", "email": "not-an-email", "password": PW}, 422),
    ({"name": "A", "email": "a@example.com", "password": "short"}, 422),
    ({"name": " ", "email": "a@example.com", "password": PW}, 422),
])
def test_register_validates_input(body, status):
    assert TestClient(api.app).post("/api/auth/register", json=body).status_code == status


def test_duplicate_email_is_rejected():
    signed_in()
    r = TestClient(api.app).post("/api/auth/register", json={"name": "B", "email": "A@example.com", "password": PW})
    assert r.status_code == 409


def test_password_is_stored_hashed():
    signed_in()
    with db.engine().connect() as conn:
        stored = conn.execute(select(db.users.c.password_hash)).scalar_one()
    assert PW not in stored and stored.startswith("scrypt$")


def test_login_logout_and_generic_errors():
    signed_in()
    c = TestClient(api.app)
    wrong = c.post("/api/auth/login", json={"email": "a@example.com", "password": "wrong password"})
    unknown = c.post("/api/auth/login", json={"email": "nobody@example.com", "password": PW})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]  # cannot tell which emails exist
    assert c.post("/api/auth/login", json={"email": "A@EXAMPLE.com", "password": PW}).status_code == 200
    assert c.get("/api/auth/me").status_code == 200
    c.post("/api/auth/logout")
    assert c.get("/api/auth/me").status_code == 401


def test_failed_logins_are_rate_limited():
    signed_in()
    c = TestClient(api.app)
    for _ in range(auth.LOGIN_MAX_FAILURES):
        assert c.post("/api/auth/login", json={"email": "a@example.com", "password": "nope nope"}).status_code == 401
    # even the right password is refused until the window passes
    assert c.post("/api/auth/login", json={"email": "a@example.com", "password": PW}).status_code == 429


def test_bearer_token_and_rejected_tokens():
    signed_in()
    user = auth.login("a@example.com", PW)
    c = TestClient(api.app)
    assert c.get("/api/runs", headers={"Authorization": f"Bearer {auth.create_token(user)}"}).status_code == 200
    expired = jwt.encode({"sub": user["id"], "exp": int(time.time()) - 10, "iss": "apollo"}, auth.secret(), algorithm="HS256")
    forged = jwt.encode({"sub": user["id"], "exp": int(time.time()) + 999, "iss": "apollo"}, "not-the-secret", algorithm="HS256")
    for token in (expired, forged, "garbage"):
        assert c.get("/api/runs", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_runs_are_private_to_their_owner():
    alice, bob = signed_in("alice@example.com", "Alice"), signed_in("bob@example.com", "Bob")
    run_id = alice.post("/api/runs", json={"topic": "Effects of drip irrigation", "max_rounds": 1}).json()["run_id"]
    assert [r["run_id"] for r in alice.get("/api/runs").json()] == [run_id]
    assert bob.get("/api/runs").json() == []
    for url in (f"/api/runs/{run_id}", f"/api/runs/{run_id}/report", f"/api/runs/{run_id}/metrics", f"/api/runs/{run_id}/events"):
        assert bob.get(url).status_code == 404, url  # someone else's run looks like a missing one


def test_runs_without_an_owner_are_hidden_from_everyone():
    legacy = api.Run("Older topic", 1, False, run_id="legacy1")  # saved before accounts: owner None
    legacy.status = "complete"
    api._runs[legacy.id] = legacy
    for c in (signed_in("a@example.com"), signed_in("b@example.com")):
        assert c.get("/api/runs").json() == []  # a new account starts with an empty history
        assert c.get("/api/runs/legacy1").status_code == 404
