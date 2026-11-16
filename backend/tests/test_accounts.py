"""Accounts: signup/login/logout, sessions, the login gate, OAuth linking, and builds ownership."""
import pytest
from fastapi.testclient import TestClient

from netproof import accounts
import netproof.server as server


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.delenv("AUTH_REQUIRED", raising=False)
    accounts.reset_engine()
    accounts._LOGIN_HITS.clear()
    yield TestClient(server.app)
    accounts.reset_engine()


def test_password_hash_roundtrip():
    h = accounts.hash_password("correct horse")
    assert accounts.verify_password("correct horse", h)
    assert not accounts.verify_password("wrong", h)
    assert h != accounts.hash_password("correct horse")  # salted


def test_session_tamper_rejected(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "k")
    tok = accounts.sign_session(7)
    assert accounts.read_session(tok) == 7
    payload, sig = tok.rsplit(".", 1)
    assert accounts.read_session(payload + "." + sig[:-2] + "AA") is None


def test_signup_login_logout(client):
    r = client.post("/auth/signup", json={"email": "Ada@Example.com", "password": "longenough"})
    assert r.status_code == 200
    assert client.get("/auth/me").json()["email"] == "ada@example.com"
    assert client.post("/auth/signup", json={"email": "ada@example.com", "password": "longenough"}).status_code == 409
    client.post("/auth/logout")
    client.cookies.clear()
    assert client.get("/auth/me").status_code == 401
    assert client.post("/auth/login", json={"email": "ada@example.com", "password": "nope-nope"}).status_code == 401
    assert client.post("/auth/login", json={"email": "ada@example.com", "password": "longenough"}).status_code == 200
    assert client.get("/auth/me").status_code == 200
    assert client.post("/auth/signup", json={"email": "x@y.z", "password": "short"}).status_code == 422


def test_builds_are_private(client):
    client.post("/auth/signup", json={"email": "a@x.io", "password": "password1"})
    bid = client.post("/api/builds", json={"domain": "fiber", "title": "Lab", "verdict": "PASS", "data": {"elements": []}}).json()["id"]
    assert client.get("/api/builds").json()[0]["title"] == "Lab"
    assert client.put(f"/api/builds/{bid}", json={"domain": "fiber", "title": "Lab 2", "data": {}}).status_code == 200
    assert client.get(f"/api/builds/{bid}").json()["title"] == "Lab 2"
    client.cookies.clear()
    client.post("/auth/signup", json={"email": "b@x.io", "password": "password2"})
    assert client.get(f"/api/builds/{bid}").status_code == 404
    assert client.delete(f"/api/builds/{bid}").status_code == 404
    assert client.get("/api/builds").json() == []
    client.cookies.clear()
    assert client.get("/api/builds").status_code == 401


def test_login_gate(client, monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "1")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.post("/check", json={"components": []}).status_code == 401
    assert client.get("/health").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.get("/auth/config").json()["required"] is True
    client.post("/auth/signup", json={"email": "g@x.io", "password": "password1"})
    assert client.get("/", follow_redirects=False).status_code == 200


def test_guest_login_creates_throwaway_user(client):
    r = client.post("/auth/guest")
    assert r.status_code == 200
    body = r.json()
    assert body["is_guest"] is True and body["name"] == "Guest"
    assert body["email"].startswith("guest-") and body["email"].endswith("@guest.local")
    assert accounts.COOKIE in r.cookies  # session cookie set
    me = client.get("/auth/me").json()
    assert me["id"] == body["id"] and me["email"] == body["email"] and me["is_guest"] == 1
    # Marked in the database, with no password to log in with.
    from sqlalchemy import select as _select
    with accounts.engine().connect() as c:
        row = c.execute(_select(accounts.users).where(accounts.users.c.id == body["id"])).mappings().first()
    assert row["is_guest"] == 1 and row["password_hash"] is None
    assert client.post("/auth/login", json={"email": body["email"], "password": "anything-goes"}).status_code == 401
    # A guest session works like any other: it can save builds.
    assert client.post("/api/builds", json={"domain": "fiber", "title": "Guest lab", "data": {}}).status_code == 200


def test_guest_users_are_unique(client):
    a = client.post("/auth/guest").json()
    client.cookies.clear()
    b = client.post("/auth/guest").json()
    assert a["id"] != b["id"] and a["email"] != b["email"]


def test_guest_domain_reserved_for_signup(client):
    assert client.post("/auth/signup", json={"email": "guest-abc@guest.local", "password": "longenough"}).status_code == 422


def test_guest_rate_limited_like_login(client):
    for _ in range(20):
        assert client.post("/auth/guest").status_code == 200
        client.cookies.clear()
    r = client.post("/auth/guest")
    assert r.status_code == 429
    assert r.json()["error"] == "rate_limited"  # server flattens HTTPException detail to top level


def test_guest_column_added_to_preexisting_database(client, tmp_path, monkeypatch):
    """A users table built before is_guest existed still works (migration)."""
    import sqlite3
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(320) NOT NULL UNIQUE,"
                   " name VARCHAR(200), password_hash VARCHAR(300), google_id VARCHAR(100) UNIQUE,"
                   " github_id VARCHAR(100) UNIQUE, created_at DATETIME NOT NULL)")
        db.execute("INSERT INTO users (email, created_at) VALUES ('old@x.io', '2026-01-01 00:00:00')")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    accounts.reset_engine()
    accounts._LOGIN_HITS.clear()
    assert client.post("/auth/guest").status_code == 200
    assert client.get("/auth/me").json()["is_guest"] == 1
    client.cookies.clear()
    assert client.post("/auth/login", json={"email": "old@x.io", "password": "x"}).status_code == 401


def test_oauth_disabled_without_keys_and_links_by_email(client, monkeypatch):
    for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    assert client.get("/auth/config").json() == {"google": False, "github": False, "required": False}
    assert client.get("/auth/github/start", follow_redirects=False).status_code == 404
    client.post("/auth/signup", json={"email": "c@x.io", "password": "password1"})
    uid = client.get("/auth/me").json()["id"]
    assert accounts.upsert_oauth_user("github", "123", "c@x.io", "C") == uid      # linked by email
    assert accounts.upsert_oauth_user("github", "123", "other@x.io", "C") == uid  # found by provider id
    assert accounts.upsert_oauth_user("google", "g9", "new@x.io", "N") != uid     # new account
    # Callback with a bad state never logs anyone in.
    r = client.get("/auth/github/callback?code=x&state=y", follow_redirects=False)
    assert r.status_code == 303 and "error=oauth_state" in r.headers["location"]
