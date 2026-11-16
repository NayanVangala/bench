"""Accounts and saved builds: login (email + password, Google, GitHub),
signed session cookies, and a per-user builds store.

Storage is SQLAlchemy Core over ``DATABASE_URL`` (Render Postgres in
production; SQLite locally and in tests). Tables are created on first use.

Guest access: POST /auth/guest creates a throwaway user (random
``guest-…@guest.local`` email, no password, ``is_guest=1``) and sets the
same signed session cookie as a normal login. A guest can never log in
with a password and can never sign up with a ``@guest.local`` address;
its saved builds live only under that throwaway account.

Environment:
  DATABASE_URL          postgres://... or sqlite:///path (default sqlite:///./bench.db)
  SECRET_KEY            signs session cookies (set in production; a random
                        per-process key is used otherwise, so sessions end on restart)
  AUTH_REQUIRED=1       every page/API needs a login (see ``auth_gate``)
  PUBLIC_URL            optional, e.g. https://bench.example; used for OAuth redirects
  GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET   enable "Continue with Google"
  GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET   enable "Continue with GitHub"
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import (Column, DateTime, ForeignKey, Integer, MetaData, String, Table, Text,
                        create_engine, delete, insert, select, text, update)
from sqlalchemy.exc import IntegrityError

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

metadata = MetaData()
users = Table(
    "users", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("email", String(320), unique=True, nullable=False),
    Column("name", String(200)),
    Column("password_hash", String(300)),
    Column("google_id", String(100), unique=True),
    Column("github_id", String(100), unique=True),
    # 1 for throwaway guest accounts (POST /auth/guest): no password, random
    # guest-...@guest.local email. 0 for normal / OAuth accounts.
    Column("is_guest", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
builds = Table(
    "builds", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("domain", String(20), nullable=False),
    Column("title", String(200), nullable=False),
    Column("verdict", String(10)),
    Column("data", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

_engine = None


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL") or "sqlite:///./bench.db"
    # Render/Heroku style URLs -> SQLAlchemy's psycopg 3 driver.
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def engine():
    global _engine
    if _engine is None:
        _engine = create_engine(_db_url(), pool_pre_ping=True, future=True)
        metadata.create_all(_engine)
        _ensure_is_guest_column()
    return _engine


def _ensure_is_guest_column() -> None:
    """Add users.is_guest to databases created before the column existed.

    ``create_all`` only creates missing tables, it never alters an existing
    ``users`` table, so check the live columns and ALTER if needed. Works
    on both SQLite and Postgres; a fresh database already has the column.
    """
    from sqlalchemy import inspect
    cols = [c["name"] for c in inspect(_engine).get_columns("users")]
    if "is_guest" not in cols:
        with _engine.begin() as c:
            c.execute(text("ALTER TABLE users ADD COLUMN is_guest INTEGER NOT NULL DEFAULT 0"))


def reset_engine() -> None:
    """Drop the cached engine (tests switch DATABASE_URL between cases)."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Passwords (scrypt, stdlib) and session cookies (HMAC-signed)
# ---------------------------------------------------------------------------

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, dklen=64, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: Optional[str]) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    _, salt_b64, dk_b64 = stored.split("$")
    dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), dklen=64, **_SCRYPT)
    return hmac.compare_digest(dk, base64.b64decode(dk_b64))


_FALLBACK_SECRET = secrets.token_bytes(32)
COOKIE = "bench_session"
SESSION_DAYS = 30


def _secret() -> bytes:
    s = os.environ.get("SECRET_KEY")
    return s.encode() if s else _FALLBACK_SECRET


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_session(user_id: int) -> str:
    payload = _b64(json.dumps({"uid": user_id, "exp": int(time.time()) + SESSION_DAYS * 86400}).encode())
    sig = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    return payload + "." + sig


def read_session(token: Optional[str]) -> Optional[int]:
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    good = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, good):
        return None
    try:
        data = json.loads(_unb64(payload))
    except ValueError:
        return None
    if int(data.get("exp", 0)) < time.time():
        return None
    return int(data["uid"])


def _is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto.split(",")[0].strip() == "https"


def _set_session(resp, request: Request, user_id: int):
    resp.set_cookie(COOKIE, sign_session(user_id), max_age=SESSION_DAYS * 86400, httponly=True,
                    samesite="lax", secure=_is_https(request), path="/")
    return resp


def current_user(request: Request) -> Optional[dict]:
    uid = read_session(request.cookies.get(COOKIE))
    if uid is None:
        return None
    with engine().connect() as c:
        row = c.execute(select(users.c.id, users.c.email, users.c.name, users.c.is_guest).where(users.c.id == uid)).mappings().first()
    return dict(row) if row else None


def require_user(request: Request) -> dict:
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail={"error": "not_logged_in", "message": "Log in to continue."})
    return u


# ---------------------------------------------------------------------------
# Login gate (AUTH_REQUIRED=1): pages redirect to /login, APIs answer 401
# ---------------------------------------------------------------------------

_OPEN_PATHS = ("/login", "/auth/", "/health", "/styles.css", "/favicon.svg", "/apple-touch-icon.png")


def auth_required() -> bool:
    return os.environ.get("AUTH_REQUIRED", "").strip() in ("1", "true", "yes")


async def auth_gate(request: Request, call_next):
    path = request.url.path
    if auth_required() and not any(path == p or path.startswith(p) for p in _OPEN_PATHS):
        if read_session(request.cookies.get(COOKIE)) is None:
            if request.method == "GET" and (path == "/" or path.endswith(".html")):
                return RedirectResponse("/login", status_code=303)
            return JSONResponse(status_code=401, content={"error": "not_logged_in", "message": "Log in to continue."})
    return await call_next(request)


# ---------------------------------------------------------------------------
# Login attempt limiter (per IP)
# ---------------------------------------------------------------------------

# ponytail: in-memory, per instance; move to the database if the app scales out.
_LOGIN_HITS: dict[str, deque] = defaultdict(deque)
LOGIN_LIMIT = 20  # attempts per IP per 15 minutes


def _too_many_logins(request: Request) -> bool:
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[-1].strip() if fwd.strip() else (request.client.host if request.client else "?")
    now = time.monotonic()
    hits = _LOGIN_HITS[ip]
    while hits and now - hits[0] > 900:
        hits.popleft()
    hits.append(now)
    return len(hits) > LOGIN_LIMIT


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

router = APIRouter()
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SignupIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=8, max_length=200)
    name: Optional[str] = Field(default=None, max_length=200)


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=200)


def _providers() -> dict:
    return {
        "google": bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET")),
        "github": bool(os.environ.get("GITHUB_CLIENT_ID") and os.environ.get("GITHUB_CLIENT_SECRET")),
    }


@router.get("/auth/config")
def auth_config() -> dict:
    return {**_providers(), "required": auth_required()}


@router.get("/auth/me")
def auth_me(request: Request) -> dict:
    return require_user(request)


GUEST_EMAIL_DOMAIN = "guest.local"


def create_guest_user() -> tuple[int, str]:
    """Create a throwaway guest user: random unguessable email, no password.

    Retries on the (negligible) chance the random email collides with an
    existing row. The returned user can never log in with a password
    (``password_hash`` is NULL, so ``verify_password`` always fails) and
    ``is_guest=1`` marks it in the database and in ``/auth/me``.
    """
    for _ in range(5):
        email = f"guest-{secrets.token_hex(16)}@{GUEST_EMAIL_DOMAIN}"
        try:
            with engine().begin() as c:
                uid = c.execute(insert(users).values(email=email, name="Guest", password_hash=None,
                                                     is_guest=1, created_at=_now())).inserted_primary_key[0]
            return uid, email
        except IntegrityError:
            continue
    raise HTTPException(status_code=503, detail={"error": "guest_failed", "message": "Could not create a guest account. Try again."})


@router.post("/auth/guest")
def guest_login(request: Request):
    if _too_many_logins(request):
        raise HTTPException(status_code=429, detail={"error": "rate_limited", "message": "Too many attempts. Try again in a few minutes."})
    uid, email = create_guest_user()
    return _set_session(JSONResponse({"id": uid, "email": email, "name": "Guest", "is_guest": True}), request, uid)


@router.post("/auth/signup")
def signup(body: SignupIn, request: Request):
    if _too_many_logins(request):
        raise HTTPException(status_code=429, detail={"error": "rate_limited", "message": "Too many attempts. Try again in a few minutes."})
    email = body.email.strip().lower()
    if not _EMAIL.match(email):
        raise HTTPException(status_code=422, detail={"error": "bad_email", "message": "Enter a valid email address."})
    if email.endswith("@" + GUEST_EMAIL_DOMAIN):
        raise HTTPException(status_code=422, detail={"error": "bad_email", "message": "That email domain is reserved for guest accounts."})
    with engine().begin() as c:
        if c.execute(select(users.c.id).where(users.c.email == email)).first():
            raise HTTPException(status_code=409, detail={"error": "email_taken", "message": "An account with this email already exists. Log in instead."})
        uid = c.execute(insert(users).values(email=email, name=(body.name or "").strip() or None,
                                             password_hash=hash_password(body.password), created_at=_now())).inserted_primary_key[0]
    return _set_session(JSONResponse({"id": uid, "email": email}), request, uid)


@router.post("/auth/login")
def login(body: LoginIn, request: Request):
    if _too_many_logins(request):
        raise HTTPException(status_code=429, detail={"error": "rate_limited", "message": "Too many attempts. Try again in a few minutes."})
    email = body.email.strip().lower()
    with engine().connect() as c:
        row = c.execute(select(users.c.id, users.c.password_hash).where(users.c.email == email)).first()
    if not row or not verify_password(body.password, row.password_hash):
        raise HTTPException(status_code=401, detail={"error": "bad_login", "message": "Wrong email or password."})
    return _set_session(JSONResponse({"id": row.id, "email": email}), request, row.id)


@router.post("/auth/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


# ---- OAuth (Google, GitHub): server-side code flow with a state cookie ----

_OAUTH = {
    "google": {
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scope": "openid email profile",
    },
    "github": {
        "authorize": "https://github.com/login/oauth/authorize",
        "token": "https://github.com/login/oauth/access_token",
        "scope": "read:user user:email",
    },
}


def _public_base(request: Request) -> str:
    base = os.environ.get("PUBLIC_URL")
    if base:
        return base.rstrip("/")
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return ("https" if _is_https(request) else "http") + "://" + host


def _redirect_uri(request: Request, provider: str) -> str:
    return f"{_public_base(request)}/auth/{provider}/callback"


def _client(provider: str) -> tuple[str, str]:
    p = provider.upper()
    cid, secret = os.environ.get(f"{p}_CLIENT_ID"), os.environ.get(f"{p}_CLIENT_SECRET")
    if provider not in _OAUTH or not cid or not secret:
        raise HTTPException(status_code=404, detail={"error": "provider_disabled", "message": f"{provider} sign-in is not set up."})
    return cid, secret


@router.get("/auth/{provider}/start")
def oauth_start(provider: str, request: Request):
    cid, _ = _client(provider)
    state = secrets.token_urlsafe(24)
    params = {"client_id": cid, "redirect_uri": _redirect_uri(request, provider), "scope": _OAUTH[provider]["scope"], "state": state}
    if provider == "google":
        params.update(response_type="code", prompt="select_account")
    resp = RedirectResponse(_OAUTH[provider]["authorize"] + "?" + urlencode(params), status_code=303)
    resp.set_cookie("bench_oauth_state", state, max_age=600, httponly=True, samesite="lax", secure=_is_https(request), path="/")
    return resp


def _oauth_identity(provider: str, code: str, request: Request) -> tuple[str, str, Optional[str]]:
    """Exchange the code; return (provider_user_id, verified_email, name)."""
    cid, secret = _client(provider)
    with httpx.Client(timeout=15) as h:
        tok = h.post(_OAUTH[provider]["token"], headers={"Accept": "application/json"}, data={
            "client_id": cid, "client_secret": secret, "code": code,
            "redirect_uri": _redirect_uri(request, provider), "grant_type": "authorization_code"}).json()
        access = tok.get("access_token")
        if not access:
            raise HTTPException(status_code=400, detail={"error": "oauth_failed", "message": "Sign-in was cancelled or failed."})
        auth = {"Authorization": f"Bearer {access}"}
        if provider == "google":
            info = h.get("https://openidconnect.googleapis.com/v1/userinfo", headers=auth).json()
            if not info.get("email") or not info.get("email_verified"):
                raise HTTPException(status_code=400, detail={"error": "oauth_no_email", "message": "Your Google account has no verified email."})
            return str(info["sub"]), info["email"].lower(), info.get("name")
        info = h.get("https://api.github.com/user", headers=auth).json()
        emails = h.get("https://api.github.com/user/emails", headers=auth).json()
        primary = next((e["email"] for e in emails if isinstance(e, dict) and e.get("primary") and e.get("verified")), None)
        if not primary:
            raise HTTPException(status_code=400, detail={"error": "oauth_no_email", "message": "Your GitHub account has no verified primary email."})
        return str(info["id"]), primary.lower(), info.get("name") or info.get("login")


def upsert_oauth_user(provider: str, provider_id: str, email: str, name: Optional[str]) -> int:
    """Find by provider id, else link by verified email, else create."""
    col = users.c.google_id if provider == "google" else users.c.github_id
    with engine().begin() as c:
        row = c.execute(select(users.c.id).where(col == provider_id)).first()
        if row:
            return row.id
        row = c.execute(select(users.c.id).where(users.c.email == email)).first()
        if row:
            c.execute(update(users).where(users.c.id == row.id).values({col.name: provider_id}))
            return row.id
        return c.execute(insert(users).values({"email": email, "name": name, col.name: provider_id,
                                               "created_at": _now()})).inserted_primary_key[0]


@router.get("/auth/{provider}/callback")
def oauth_callback(provider: str, request: Request, code: str = "", state: str = ""):
    expected = request.cookies.get("bench_oauth_state")
    if not code or not state or not expected or not hmac.compare_digest(state, expected):
        return RedirectResponse("/login?error=oauth_state", status_code=303)
    try:
        pid, email, name = _oauth_identity(provider, code, request)
    except HTTPException as exc:
        return RedirectResponse("/login?error=" + exc.detail.get("error", "oauth_failed"), status_code=303)
    uid = upsert_oauth_user(provider, pid, email, name)
    resp = _set_session(RedirectResponse("/", status_code=303), request, uid)
    resp.delete_cookie("bench_oauth_state", path="/")
    return resp


# ---- Saved builds -------------------------------------------------------

MAX_BUILD_BYTES = 1_000_000


class BuildIn(BaseModel):
    domain: str = Field(pattern="^(circuit|fiber|optics)$")
    title: str = Field(min_length=1, max_length=200)
    verdict: Optional[str] = Field(default=None, max_length=10)
    data: dict


def _build_row(r) -> dict:
    d = dict(r)
    for k in ("created_at", "updated_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    if "data" in d:
        d["data"] = json.loads(d["data"])
    return d


def _encode(data: dict) -> str:
    text = json.dumps(data)
    if len(text) > MAX_BUILD_BYTES:
        raise HTTPException(status_code=413, detail={"error": "build_too_large", "message": "This build is too large to save."})
    return text


@router.get("/api/builds")
def list_builds(request: Request) -> list:
    u = require_user(request)
    with engine().connect() as c:
        rows = c.execute(select(builds.c.id, builds.c.domain, builds.c.title, builds.c.verdict, builds.c.updated_at)
                         .where(builds.c.user_id == u["id"]).order_by(builds.c.updated_at.desc())).mappings().all()
    return [_build_row(r) for r in rows]


@router.post("/api/builds")
def create_build(body: BuildIn, request: Request) -> dict:
    u = require_user(request)
    now = _now()
    with engine().begin() as c:
        bid = c.execute(insert(builds).values(user_id=u["id"], domain=body.domain, title=body.title.strip(),
                                              verdict=body.verdict, data=_encode(body.data),
                                              created_at=now, updated_at=now)).inserted_primary_key[0]
    return {"id": bid}


def _own(c, bid: int, uid: int):
    row = c.execute(select(builds).where(builds.c.id == bid, builds.c.user_id == uid)).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Build not found."})
    return row


@router.get("/api/builds/{bid}")
def get_build(bid: int, request: Request) -> dict:
    u = require_user(request)
    with engine().connect() as c:
        return _build_row(_own(c, bid, u["id"]))


@router.put("/api/builds/{bid}")
def update_build(bid: int, body: BuildIn, request: Request) -> dict:
    u = require_user(request)
    with engine().begin() as c:
        _own(c, bid, u["id"])
        c.execute(update(builds).where(builds.c.id == bid).values(domain=body.domain, title=body.title.strip(),
                                                                   verdict=body.verdict, data=_encode(body.data),
                                                                   updated_at=_now()))
    return {"id": bid}


@router.delete("/api/builds/{bid}")
def delete_build(bid: int, request: Request) -> dict:
    u = require_user(request)
    with engine().begin() as c:
        _own(c, bid, u["id"])
        c.execute(delete(builds).where(builds.c.id == bid))
    return {"ok": True}
