# Multi-user hosted MCP with OAuth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let people on an allowlist connect their own Gmail to the hosted MCP through Google sign-in, from any MCP client including Claude web and mobile, with no cross-user access.

**Architecture:** The hosted server becomes its own OAuth 2.1 authorization server. The MCP Python SDK (`mcp` 1.30) supplies the endpoint handlers (`create_auth_routes`); we supply a Postgres-backed provider, a Google sign-in leg (`/google/callback`) and per-user Gmail credentials. `serverless.py` dispatches `/mcp` (bearer-authenticated, user placed in a contextvar) and everything else to a Starlette OAuth app. The static `MCP_AUTH_TOKEN` maps to a sentinel `owner` user and keeps the current env-var credentials.

**Tech Stack:** Python 3.10+, `mcp` SDK OAuth server support, Starlette, httpx, psycopg 3 (Neon Postgres), `cryptography` (AES-GCM), pytest.

**Spec:** `docs/superpowers/specs/2026-10-06-multi-user-oauth-design.md`

## Global Constraints

- Gmail scope stays read-only: `gmail.readonly` (sign-in also asks `openid email`).
- Access token life 1 hour. Refresh token life 30 days, rotated on every use.
- Auth codes: 5 minute life, single use. PKCE S256 only. OAuth `state` verified on the Google callback.
- MCP tokens and auth codes are stored as SHA-256 hashes, never raw. Google refresh tokens are AES-GCM encrypted; the key (`TOKEN_ENC_KEY`) lives in env, never in the database.
- `user_id` comes only from the validated MCP token, never from tool arguments.
- Per-user rate limit about 60 tool calls per minute (`GMAIL_ATT_RATE_LIMIT`, default 60). `get_attachment_base64` keeps its 1.5 MB cap.
- Audit log stores tool name and message ID only, never attachment content.
- Static `MCP_AUTH_TOKEN` still works as the owner; shorter than 32 characters means it is disabled (fail closed).
- Storage is Neon Postgres via the Vercel Marketplace (`DATABASE_URL`).
- Local (non-hosted) mode must keep working with no database and no new env vars.
- Python `>=3.10` (existing `requires-python`).

## Review Focus

Failure modes the spec implies but a happy-path build would miss, most likely first. Each has a named test in the owning task.

1. A Google account that is not on the allowlist (or has an unverified email) finishes sign-in: must see a "not approved" page, with no user row stored and no auth code issued. (`test_callback_rejects_email_not_on_allowlist`, `test_callback_rejects_unverified_email`, Task 6)
2. An auth code presented twice: the second exchange must fail. (`test_auth_code_cannot_be_reused`, Task 6)
3. An old refresh token used after rotation: must be rejected. (`test_rotated_refresh_token_cannot_be_reused`, Task 6)
4. A forged, tampered or expired `state` on `/google/callback`: must return 400 and store nothing. (`test_callback_rejects_bad_state`, Task 6)
5. A user whose Google refresh token died (Testing mode expires it after 7 days): the tool call must return a clear "reconnect" error, and the next `/mcp` call must return 401 so the client restarts sign-in. (`test_dead_google_token_forces_reauth`, Task 7)

Also covered: cross-user isolation (`test_users_only_reach_their_own_mailbox`, Task 7). Known limitation, deliberately not fixed: the Google callback does not bind to the browser that started sign-in (login CSRF), and client registration is open, so anyone can create client rows; the allowlist gates real access.

## File Structure

| File | Responsibility |
|---|---|
| `src/gmail_attachments_mcp/crypto.py` (new) | AES-GCM, token hashing, random tokens, signed `state` |
| `src/gmail_attachments_mcp/store.py` (new) | `PgStore` (Postgres) and `MemoryStore` (tests), identical methods; `get_store`/`set_store` |
| `src/gmail_attachments_mcp/identity.py` (new) | `OWNER`, `current_user` contextvar, `NotConnected` |
| `src/gmail_attachments_mcp/users.py` (new) | `credentials_for(user_id)` builds Google credentials for a user, handles dead tokens |
| `src/gmail_attachments_mcp/oauth.py` (new) | `Settings`, `GoogleLogin`, `GmailOAuthProvider`, `build_routes` (SDK routes + `/google/callback`) |
| `src/gmail_attachments_mcp/admin.py` (new) | `gmail-attachments-initdb`, `gmail-attachments-allow` CLIs |
| `src/gmail_attachments_mcp/server.py` (modify) | `_service()` per user, `audited` decorator (rate limit + audit log) |
| `src/gmail_attachments_mcp/serverless.py` (modify) | `build_app`, dispatcher, 401 with discovery header |
| `tests/` (new) | `conftest.py`, `fakes.py`, one test module per source module |
| `pyproject.toml`, `.vercelignore`, `README.md` (modify) | deps, scripts, docs |

---

### Task 1: Scaffolding (deps, pytest, branch)

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/__init__.py`, `tests/conftest.py`

**Interfaces:**
- Produces: `enc_key` autouse fixture (a valid `TOKEN_ENC_KEY` for every test); `uv run pytest` works.

- [ ] **Step 1: Create the work branch**

```bash
cd ~/Documents/GitHub/mcp/gmail-attachments-mcp
git switch -c multi-user-oauth
```

- [ ] **Step 2: Edit `pyproject.toml`**

Replace the `dependencies` list and add the dev group and pytest config so the file reads:

```toml
[project]
name = "gmail-attachments-mcp"
version = "0.1.0"
description = "MCP server: list and read Gmail attachments (read-only). Local, or hosted multi-user."
requires-python = ">=3.10"
dependencies = [
    "mcp>=1.9.0,<2",
    "google-api-python-client>=2.130.0",
    "google-auth-oauthlib>=1.2.0",
    "pypdf>=4.2.0",
    "python-docx>=1.2.0",
    "openpyxl>=3.1.5",
    "python-pptx>=1.0.2",
    "psycopg[binary]>=3.2",
    "cryptography>=42",
    "httpx>=0.27",
]

[dependency-groups]
dev = ["pytest>=8"]

[project.scripts]
gmail-attachments-mcp = "gmail_attachments_mcp.server:main"
gmail-attachments-auth = "gmail_attachments_mcp.auth:main"
gmail-attachments-export = "gmail_attachments_mcp.auth:export_env"
gmail-attachments-initdb = "gmail_attachments_mcp.admin:initdb"
gmail-attachments-allow = "gmail_attachments_mcp.admin:allow"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/gmail_attachments_mcp"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

- [ ] **Step 3: Create `tests/__init__.py` (empty) and `tests/conftest.py`**

```python
import base64

import pytest


@pytest.fixture(autouse=True)
def enc_key(monkeypatch):
    monkeypatch.setenv("TOKEN_ENC_KEY", base64.urlsafe_b64encode(b"k" * 32).decode())
```

- [ ] **Step 4: Install and confirm pytest runs**

Run: `uv sync && uv run pytest -q`
Expected: `no tests ran` (exit code 5), no import errors. The `admin` scripts do not exist yet; that is fine until Task 8, but `uv sync` may warn about the missing entry point module. It does not fail.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock tests/__init__.py tests/conftest.py
git commit -m "chore: add multi-user deps and pytest scaffolding" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Crypto helpers

**Files:**
- Create: `src/gmail_attachments_mcp/crypto.py`
- Test: `tests/test_crypto.py`

**Interfaces:**
- Produces:
  - `encrypt(plaintext: str, context: str = "") -> str`
  - `decrypt(blob: str, context: str = "") -> str` (raises `cryptography.exceptions.InvalidTag` on tamper or wrong context)
  - `new_token() -> str` (urlsafe, 256 bits)
  - `hash_token(token: str) -> str` (SHA-256 hex)
  - `sign_state(payload: dict, ttl: int = 600) -> str`
  - `verify_state(state: str) -> dict | None`

- [ ] **Step 1: Write the failing tests** (`tests/test_crypto.py`)

```python
import base64

import pytest
from cryptography.exceptions import InvalidTag

from gmail_attachments_mcp.crypto import (
    decrypt,
    encrypt,
    hash_token,
    new_token,
    sign_state,
    verify_state,
)


def test_encrypt_round_trip():
    blob = encrypt("refresh-token", "a@example.com")
    assert "refresh-token" not in blob
    assert decrypt(blob, "a@example.com") == "refresh-token"


def test_encrypt_is_randomised():
    assert encrypt("x", "c") != encrypt("x", "c")


def test_decrypt_rejects_wrong_context():
    blob = encrypt("secret", "a@example.com")
    with pytest.raises(InvalidTag):
        decrypt(blob, "b@example.com")


def test_decrypt_rejects_tampering():
    raw = bytearray(base64.urlsafe_b64decode(encrypt("secret", "c")))
    raw[-1] ^= 1
    with pytest.raises(InvalidTag):
        decrypt(base64.urlsafe_b64encode(bytes(raw)).decode(), "c")


def test_missing_or_short_key_raises(monkeypatch):
    monkeypatch.setenv("TOKEN_ENC_KEY", "")
    with pytest.raises(RuntimeError, match="TOKEN_ENC_KEY"):
        encrypt("x")
    monkeypatch.setenv("TOKEN_ENC_KEY", base64.urlsafe_b64encode(b"short").decode())
    with pytest.raises(RuntimeError, match="TOKEN_ENC_KEY"):
        encrypt("x")


def test_tokens_are_unique_and_hash_is_stable():
    assert new_token() != new_token()
    assert len(new_token()) >= 43
    assert hash_token("abc") == hash_token("abc")
    assert hash_token("abc") != hash_token("abd")
    assert len(hash_token("abc")) == 64


def test_state_round_trip():
    assert verify_state(sign_state({"a": 1}))["a"] == 1


def test_state_rejects_tampering_and_garbage():
    state = sign_state({"a": 1})
    body, sig = state.rsplit(".", 1)
    assert verify_state(f"{body}x.{sig}") is None
    assert verify_state(f"{body}.{'0' * 64}") is None
    assert verify_state("garbage") is None
    assert verify_state("") is None


def test_state_rejects_expired():
    assert verify_state(sign_state({"a": 1}, ttl=-1)) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_crypto.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'gmail_attachments_mcp.crypto'`.

- [ ] **Step 3: Implement `src/gmail_attachments_mcp/crypto.py`**

```python
"""Token crypto: AES-GCM for Google refresh tokens, hashing and random tokens for MCP
tokens, HMAC-signed OAuth state. Key comes from TOKEN_ENC_KEY (32 bytes, urlsafe base64)."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _key() -> bytes:
    raw = os.environ.get("TOKEN_ENC_KEY", "")
    try:
        key = _b64d(raw)
    except Exception:
        key = b""
    if len(key) != 32:
        raise RuntimeError("TOKEN_ENC_KEY must be 32 bytes, urlsafe-base64 encoded")
    return key


def _state_key() -> bytes:
    return hashlib.sha256(b"oauth-state:" + _key()).digest()


def encrypt(plaintext: str, context: str = "") -> str:
    nonce = os.urandom(12)
    blob = AESGCM(_key()).encrypt(nonce, plaintext.encode(), context.encode())
    return base64.urlsafe_b64encode(nonce + blob).decode()


def decrypt(blob: str, context: str = "") -> str:
    raw = _b64d(blob)
    return AESGCM(_key()).decrypt(raw[:12], raw[12:], context.encode()).decode()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def sign_state(payload: dict, ttl: int = 600) -> str:
    data = json.dumps({**payload, "exp": time.time() + ttl}, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(data).decode().rstrip("=")
    sig = hmac.new(_state_key(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_state(state: str) -> dict | None:
    try:
        body, sig = state.rsplit(".", 1)
    except ValueError:
        return None
    expected = hmac.new(_state_key(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_b64d(body))
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_crypto.py -q`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add src/gmail_attachments_mcp/crypto.py tests/test_crypto.py
git commit -m "feat: token crypto, hashing and signed state" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Store (Postgres and in-memory)

**Files:**
- Create: `src/gmail_attachments_mcp/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: `MemoryStore`, `PgStore(dsn)`, `get_store()`, `set_store(store)`. Both stores expose exactly these sync methods (times are epoch seconds):
  - `init_schema() -> None`
  - `add_allowed(email: str) -> None`; `is_allowed(email: str) -> bool` (case-insensitive)
  - `upsert_user(email: str, refresh_token_enc: str) -> str` (user id; clears `revoked_at`)
  - `get_user(user_id: str) -> dict | None` with keys `id, email, refresh_token_enc, revoked_at`
  - `disconnect_user(user_id: str) -> None` (nulls the stored Google token, sets `revoked_at`, revokes all the user's MCP tokens)
  - `save_client(client_id: str, info_json: str) -> None`; `get_client(client_id: str) -> str | None`
  - `save_auth_code(code_hash: str, *, client_id, user_id, code_challenge, redirect_uri, redirect_uri_provided_explicitly: bool, resource: str | None, scopes: list[str], expires_at: float) -> None`
  - `get_auth_code(code_hash: str) -> dict | None` (unused and unexpired only; keys `client_id, user_id, code_challenge, redirect_uri, redirect_uri_provided_explicitly, resource, scopes, expires_at`)
  - `consume_auth_code(code_hash: str) -> bool` (atomic; True only the first time)
  - `save_token(token_hash: str, *, user_id, client_id, kind: str, scopes: list[str], resource: str | None, expires_at: float) -> None` (`kind` is `"access"` or `"refresh"`)
  - `get_token(token_hash: str, kind: str) -> dict | None` (unrevoked and unexpired only; keys `user_id, client_id, kind, scopes, resource, expires_at`)
  - `revoke_token(token_hash: str) -> bool` (True only if it was active)
  - `log_call(user_id: str, tool: str, message_id: str | None, ok: bool) -> None`
  - `count_calls(user_id: str, since_seconds: int) -> int`

- [ ] **Step 1: Write the contract tests** (`tests/test_store.py`). They run against `MemoryStore` always and against `PgStore` when `TEST_DATABASE_URL` is set.

```python
import os
import time

import pytest

from gmail_attachments_mcp.store import MemoryStore, PgStore

TABLES = "audit_log, mcp_tokens, auth_codes, oauth_clients, allowlist, users"


@pytest.fixture(params=["memory", "pg"])
def store(request):
    if request.param == "memory":
        return MemoryStore()
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("set TEST_DATABASE_URL to run the Postgres contract tests")
    s = PgStore(dsn)
    s.init_schema()
    s._run(f"TRUNCATE {TABLES} CASCADE")
    return s


def _code(store, user_id, code_hash="h1", expires_in=300):
    store.save_auth_code(
        code_hash,
        client_id="c1",
        user_id=user_id,
        code_challenge="chal",
        redirect_uri="http://localhost/cb",
        redirect_uri_provided_explicitly=True,
        resource=None,
        scopes=["gmail.readonly"],
        expires_at=time.time() + expires_in,
    )


def _token(store, user_id, token_hash="t1", kind="access", expires_in=3600):
    store.save_token(
        token_hash,
        user_id=user_id,
        client_id="c1",
        kind=kind,
        scopes=["gmail.readonly"],
        resource="https://x/mcp",
        expires_at=time.time() + expires_in,
    )


def test_allowlist_is_case_insensitive(store):
    assert not store.is_allowed("a@example.com")
    store.add_allowed("A@Example.com")
    store.add_allowed("a@example.com")
    assert store.is_allowed("a@EXAMPLE.com")


def test_upsert_user_is_stable_and_clears_revocation(store):
    uid = store.upsert_user("A@example.com", "enc1")
    assert store.upsert_user("a@example.com", "enc2") == uid
    user = store.get_user(uid)
    assert user["email"] == "a@example.com"
    assert user["refresh_token_enc"] == "enc2"
    assert user["revoked_at"] is None
    assert store.get_user("missing") is None


def test_disconnect_user_wipes_token_and_revokes_all_tokens(store):
    uid = store.upsert_user("a@example.com", "enc")
    other = store.upsert_user("b@example.com", "encb")
    _token(store, uid, "t-access", "access")
    _token(store, uid, "t-refresh", "refresh")
    _token(store, other, "t-other", "access")
    store.disconnect_user(uid)
    user = store.get_user(uid)
    assert user["refresh_token_enc"] is None
    assert user["revoked_at"] is not None
    assert store.get_token("t-access", "access") is None
    assert store.get_token("t-refresh", "refresh") is None
    assert store.get_token("t-other", "access") is not None


def test_client_round_trip(store):
    assert store.get_client("c1") is None
    store.save_client("c1", '{"a": 1}')
    store.save_client("c1", '{"a": 2}')
    assert store.get_client("c1") == '{"a": 2}'


def test_auth_code_is_single_use(store):
    uid = store.upsert_user("a@example.com", "enc")
    _code(store, uid)
    row = store.get_auth_code("h1")
    assert row["user_id"] == uid and row["scopes"] == ["gmail.readonly"]
    assert row["redirect_uri_provided_explicitly"] is True
    assert store.consume_auth_code("h1") is True
    assert store.consume_auth_code("h1") is False
    assert store.get_auth_code("h1") is None


def test_expired_auth_code_is_invisible(store):
    uid = store.upsert_user("a@example.com", "enc")
    _code(store, uid, expires_in=-1)
    assert store.get_auth_code("h1") is None
    assert store.consume_auth_code("h1") is False


def test_token_lifecycle(store):
    uid = store.upsert_user("a@example.com", "enc")
    _token(store, uid)
    row = store.get_token("t1", "access")
    assert row["user_id"] == uid and row["scopes"] == ["gmail.readonly"]
    assert store.get_token("t1", "refresh") is None
    assert store.revoke_token("t1") is True
    assert store.revoke_token("t1") is False
    assert store.get_token("t1", "access") is None


def test_expired_token_is_invisible(store):
    uid = store.upsert_user("a@example.com", "enc")
    _token(store, uid, expires_in=-1)
    assert store.get_token("t1", "access") is None


def test_call_counting_is_per_user_and_windowed(store):
    store.upsert_user("a@example.com", "enc")
    store.log_call("u-a", "list_attachments", "m1", True)
    store.log_call("u-a", "read_attachment_text", "m2", False)
    store.log_call("u-b", "list_attachments", None, True)
    assert store.count_calls("u-a", 60) == 2
    assert store.count_calls("u-b", 60) == 1
    assert store.count_calls("u-a", 0) == 0
```

Note: `log_call` takes any `user_id` string (no foreign key), which is why `u-a` works without a user row.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_store.py -q`
Expected: FAIL, `ImportError: cannot import name 'MemoryStore'`.

- [ ] **Step 3: Implement `src/gmail_attachments_mcp/store.py`**

```python
"""Persistence for hosted multi-user mode. PgStore talks to Postgres (Neon); MemoryStore has
identical methods for tests and local experiments. All times are epoch seconds."""
import os
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id text PRIMARY KEY,
  email text UNIQUE NOT NULL,
  google_refresh_token_enc text,
  created_at double precision NOT NULL,
  revoked_at double precision
);
CREATE TABLE IF NOT EXISTS allowlist (
  email text PRIMARY KEY,
  added_at double precision NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_clients (
  client_id text PRIMARY KEY,
  info_json text NOT NULL,
  created_at double precision NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_codes (
  code_hash text PRIMARY KEY,
  client_id text NOT NULL,
  user_id text NOT NULL REFERENCES users(id),
  code_challenge text NOT NULL,
  redirect_uri text NOT NULL,
  redirect_uri_provided_explicitly boolean NOT NULL,
  resource text,
  scopes text NOT NULL,
  expires_at double precision NOT NULL,
  used_at double precision
);
CREATE TABLE IF NOT EXISTS mcp_tokens (
  token_hash text PRIMARY KEY,
  user_id text NOT NULL REFERENCES users(id),
  client_id text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('access', 'refresh')),
  scopes text NOT NULL,
  resource text,
  expires_at double precision NOT NULL,
  revoked_at double precision
);
CREATE INDEX IF NOT EXISTS mcp_tokens_user ON mcp_tokens (user_id);
CREATE TABLE IF NOT EXISTS audit_log (
  id bigserial PRIMARY KEY,
  ts double precision NOT NULL,
  user_id text NOT NULL,
  tool text NOT NULL,
  message_id text,
  ok boolean NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_log_user_ts ON audit_log (user_id, ts);
"""

_CODE_KEYS = (
    "client_id", "user_id", "code_challenge", "redirect_uri",
    "redirect_uri_provided_explicitly", "resource", "scopes", "expires_at",
)
_TOKEN_KEYS = ("user_id", "client_id", "kind", "scopes", "resource", "expires_at")


class PgStore:
    def __init__(self, dsn: str):
        self._dsn = dsn

    def _conn(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self._dsn, autocommit=True, row_factory=dict_row)

    def _one(self, sql, params=()):
        with self._conn() as conn:
            return conn.execute(sql, params).fetchone()

    def _run(self, sql, params=()) -> int:
        with self._conn() as conn:
            return conn.execute(sql, params).rowcount

    def init_schema(self) -> None:
        self._run(SCHEMA)

    def add_allowed(self, email):
        self._run(
            "INSERT INTO allowlist (email, added_at) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (email.lower(), time.time()),
        )

    def is_allowed(self, email):
        return self._one("SELECT 1 AS x FROM allowlist WHERE email = %s", (email.lower(),)) is not None

    def upsert_user(self, email, refresh_token_enc):
        row = self._one(
            """INSERT INTO users (id, email, google_refresh_token_enc, created_at)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (email) DO UPDATE
                 SET google_refresh_token_enc = EXCLUDED.google_refresh_token_enc, revoked_at = NULL
               RETURNING id""",
            (str(uuid.uuid4()), email.lower(), refresh_token_enc, time.time()),
        )
        return row["id"]

    def get_user(self, user_id):
        return self._one(
            """SELECT id, email, google_refresh_token_enc AS refresh_token_enc, revoked_at
               FROM users WHERE id = %s""",
            (user_id,),
        )

    def disconnect_user(self, user_id):
        now = time.time()
        with self._conn() as conn, conn.transaction():
            conn.execute(
                "UPDATE users SET google_refresh_token_enc = NULL, revoked_at = %s WHERE id = %s",
                (now, user_id),
            )
            conn.execute(
                "UPDATE mcp_tokens SET revoked_at = %s WHERE user_id = %s AND revoked_at IS NULL",
                (now, user_id),
            )

    def save_client(self, client_id, info_json):
        self._run(
            """INSERT INTO oauth_clients (client_id, info_json, created_at) VALUES (%s, %s, %s)
               ON CONFLICT (client_id) DO UPDATE SET info_json = EXCLUDED.info_json""",
            (client_id, info_json, time.time()),
        )

    def get_client(self, client_id):
        row = self._one("SELECT info_json FROM oauth_clients WHERE client_id = %s", (client_id,))
        return row["info_json"] if row else None

    def save_auth_code(self, code_hash, *, client_id, user_id, code_challenge, redirect_uri,
                       redirect_uri_provided_explicitly, resource, scopes, expires_at):
        self._run(
            """INSERT INTO auth_codes (code_hash, client_id, user_id, code_challenge, redirect_uri,
                 redirect_uri_provided_explicitly, resource, scopes, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (code_hash, client_id, user_id, code_challenge, redirect_uri,
             redirect_uri_provided_explicitly, resource, " ".join(scopes), expires_at),
        )

    def get_auth_code(self, code_hash):
        row = self._one(
            """SELECT client_id, user_id, code_challenge, redirect_uri, redirect_uri_provided_explicitly,
                      resource, scopes, expires_at
               FROM auth_codes WHERE code_hash = %s AND used_at IS NULL AND expires_at > %s""",
            (code_hash, time.time()),
        )
        if row:
            row["scopes"] = row["scopes"].split()
        return row

    def consume_auth_code(self, code_hash):
        return self._run(
            "UPDATE auth_codes SET used_at = %s WHERE code_hash = %s AND used_at IS NULL AND expires_at > %s",
            (time.time(), code_hash, time.time()),
        ) == 1

    def save_token(self, token_hash, *, user_id, client_id, kind, scopes, resource, expires_at):
        self._run(
            """INSERT INTO mcp_tokens (token_hash, user_id, client_id, kind, scopes, resource, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (token_hash, user_id, client_id, kind, " ".join(scopes), resource, expires_at),
        )

    def get_token(self, token_hash, kind):
        row = self._one(
            """SELECT user_id, client_id, kind, scopes, resource, expires_at
               FROM mcp_tokens
               WHERE token_hash = %s AND kind = %s AND revoked_at IS NULL AND expires_at > %s""",
            (token_hash, kind, time.time()),
        )
        if row:
            row["scopes"] = row["scopes"].split()
        return row

    def revoke_token(self, token_hash):
        return self._run(
            "UPDATE mcp_tokens SET revoked_at = %s WHERE token_hash = %s AND revoked_at IS NULL",
            (time.time(), token_hash),
        ) == 1

    def log_call(self, user_id, tool, message_id, ok):
        self._run(
            "INSERT INTO audit_log (ts, user_id, tool, message_id, ok) VALUES (%s, %s, %s, %s, %s)",
            (time.time(), user_id, tool, message_id, ok),
        )

    def count_calls(self, user_id, since_seconds):
        row = self._one(
            "SELECT count(*) AS n FROM audit_log WHERE user_id = %s AND ts > %s",
            (user_id, time.time() - since_seconds),
        )
        return row["n"]


class MemoryStore:
    def __init__(self):
        self.users, self.clients, self.codes, self.tokens, self.audit = {}, {}, {}, {}, []
        self.allowlist = set()

    def init_schema(self):
        pass

    def add_allowed(self, email):
        self.allowlist.add(email.lower())

    def is_allowed(self, email):
        return email.lower() in self.allowlist

    def upsert_user(self, email, refresh_token_enc):
        email = email.lower()
        for user in self.users.values():
            if user["email"] == email:
                user["refresh_token_enc"], user["revoked_at"] = refresh_token_enc, None
                return user["id"]
        uid = str(uuid.uuid4())
        self.users[uid] = {"id": uid, "email": email, "refresh_token_enc": refresh_token_enc, "revoked_at": None}
        return uid

    def get_user(self, user_id):
        user = self.users.get(user_id)
        return dict(user) if user else None

    def disconnect_user(self, user_id):
        now = time.time()
        if user_id in self.users:
            self.users[user_id]["refresh_token_enc"] = None
            self.users[user_id]["revoked_at"] = now
        for token in self.tokens.values():
            if token["user_id"] == user_id and token["revoked_at"] is None:
                token["revoked_at"] = now

    def save_client(self, client_id, info_json):
        self.clients[client_id] = info_json

    def get_client(self, client_id):
        return self.clients.get(client_id)

    def save_auth_code(self, code_hash, *, client_id, user_id, code_challenge, redirect_uri,
                       redirect_uri_provided_explicitly, resource, scopes, expires_at):
        self.codes[code_hash] = {
            "client_id": client_id, "user_id": user_id, "code_challenge": code_challenge,
            "redirect_uri": redirect_uri,
            "redirect_uri_provided_explicitly": redirect_uri_provided_explicitly,
            "resource": resource, "scopes": list(scopes), "expires_at": expires_at, "used_at": None,
        }

    def get_auth_code(self, code_hash):
        row = self.codes.get(code_hash)
        if not row or row["used_at"] is not None or row["expires_at"] <= time.time():
            return None
        return {k: row[k] for k in _CODE_KEYS}

    def consume_auth_code(self, code_hash):
        if self.get_auth_code(code_hash) is None:
            return False
        self.codes[code_hash]["used_at"] = time.time()
        return True

    def save_token(self, token_hash, *, user_id, client_id, kind, scopes, resource, expires_at):
        self.tokens[token_hash] = {
            "user_id": user_id, "client_id": client_id, "kind": kind, "scopes": list(scopes),
            "resource": resource, "expires_at": expires_at, "revoked_at": None,
        }

    def get_token(self, token_hash, kind):
        row = self.tokens.get(token_hash)
        if not row or row["kind"] != kind or row["revoked_at"] is not None or row["expires_at"] <= time.time():
            return None
        return {k: row[k] for k in _TOKEN_KEYS}

    def revoke_token(self, token_hash):
        row = self.tokens.get(token_hash)
        if not row or row["revoked_at"] is not None:
            return False
        row["revoked_at"] = time.time()
        return True

    def log_call(self, user_id, tool, message_id, ok):
        self.audit.append((time.time(), user_id, tool, message_id, ok))

    def count_calls(self, user_id, since_seconds):
        cutoff = time.time() - since_seconds
        return sum(1 for ts, uid, *_ in self.audit if uid == user_id and ts > cutoff)


_store = None


def get_store():
    global _store
    if _store is None:
        _store = PgStore(os.environ["DATABASE_URL"])
    return _store


def set_store(store) -> None:
    global _store
    _store = store
```

- [ ] **Step 4: Run the in-memory tests**

Run: `uv run pytest tests/test_store.py -q`
Expected: `9 passed, 9 skipped` (the Postgres variants skip without `TEST_DATABASE_URL`).

- [ ] **Step 5: Run the Postgres variants** (needs Docker Desktop running)

```bash
docker run -d --name gmail-mcp-test-pg -e POSTGRES_PASSWORD=test -p 54329:5432 postgres:16
until docker exec gmail-mcp-test-pg pg_isready -U postgres; do sleep 1; done
TEST_DATABASE_URL=postgresql://postgres:test@localhost:54329/postgres uv run pytest tests/test_store.py -q
```
Expected: `18 passed`. If a Postgres test fails where memory passes, fix `PgStore` until the two agree. Leave the container up for Task 8; remove it at the end with `docker rm -f gmail-mcp-test-pg`.

- [ ] **Step 6: Commit**

```bash
git add src/gmail_attachments_mcp/store.py tests/test_store.py
git commit -m "feat: Postgres and in-memory store for users, codes, tokens and audit" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Identity and per-user Google credentials

**Files:**
- Create: `src/gmail_attachments_mcp/identity.py`, `src/gmail_attachments_mcp/users.py`
- Test: `tests/test_users.py`

**Interfaces:**
- Consumes: `store.get_store()`, `crypto.decrypt`, `auth.get_credentials()` (existing).
- Produces:
  - `identity.OWNER = "owner"`; `identity.current_user: ContextVar[str | None]` (default `None`); `identity.NotConnected(RuntimeError)`
  - `users.credentials_for(user_id: str) -> google.oauth2.credentials.Credentials` (refreshed; raises `NotConnected`)
  - `users._refresh(creds) -> None` (thin wrapper tests can patch)

- [ ] **Step 1: Write the failing tests** (`tests/test_users.py`)

```python
import pytest
from google.auth.exceptions import RefreshError

from gmail_attachments_mcp import users
from gmail_attachments_mcp.crypto import encrypt
from gmail_attachments_mcp.identity import OWNER, NotConnected
from gmail_attachments_mcp.store import MemoryStore, set_store


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("GOOGLE_WEB_CLIENT_ID", "gid")
    monkeypatch.setenv("GOOGLE_WEB_CLIENT_SECRET", "gsecret")
    s = MemoryStore()
    set_store(s)
    yield s
    set_store(None)


def test_credentials_use_the_users_own_refresh_token(store, monkeypatch):
    monkeypatch.setattr(users, "_refresh", lambda creds: None)
    uid = store.upsert_user("a@example.com", encrypt("rt-A", "a@example.com"))
    creds = users.credentials_for(uid)
    assert creds.refresh_token == "rt-A"
    assert creds.client_id == "gid" and creds.client_secret == "gsecret"
    assert list(creds.scopes) == ["https://www.googleapis.com/auth/gmail.readonly"]


def test_unknown_or_disconnected_user_is_not_connected(store):
    with pytest.raises(NotConnected):
        users.credentials_for("nobody")
    uid = store.upsert_user("a@example.com", encrypt("rt", "a@example.com"))
    store.disconnect_user(uid)
    with pytest.raises(NotConnected):
        users.credentials_for(uid)


def test_dead_google_token_disconnects_the_user(store, monkeypatch):
    def boom(creds):
        raise RefreshError("invalid_grant")

    monkeypatch.setattr(users, "_refresh", boom)
    uid = store.upsert_user("a@example.com", encrypt("rt", "a@example.com"))
    with pytest.raises(NotConnected, match="Reconnect"):
        users.credentials_for(uid)
    assert store.get_user(uid)["refresh_token_enc"] is None


def test_owner_uses_the_existing_env_credentials(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(users, "get_credentials", lambda: sentinel)
    assert users.credentials_for(OWNER) is sentinel
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_users.py -q`
Expected: FAIL, `ImportError: cannot import name 'users'`.

- [ ] **Step 3: Create `src/gmail_attachments_mcp/identity.py`**

```python
"""Who is calling. The hosted dispatcher sets `current_user` from the validated MCP token;
tools read it. It is never taken from tool arguments."""
from contextvars import ContextVar

OWNER = "owner"  # the static MCP_AUTH_TOKEN user, and local (stdio) mode

current_user: ContextVar[str | None] = ContextVar("current_user", default=None)


class NotConnected(RuntimeError):
    """The caller's Google connection is missing, expired or revoked."""
```

- [ ] **Step 4: Create `src/gmail_attachments_mcp/users.py`**

```python
"""Google credentials for a given user."""
import os

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from .auth import SCOPES, get_credentials
from .crypto import decrypt
from .identity import OWNER, NotConnected
from .store import get_store

RECONNECT = (
    "Your Google connection expired or was revoked. Reconnect the connector to sign in again."
)


def _refresh(creds: Credentials) -> None:
    creds.refresh(Request())


def credentials_for(user_id: str) -> Credentials:
    if user_id == OWNER:
        return get_credentials()
    store = get_store()
    user = store.get_user(user_id)
    if not user or user["revoked_at"] is not None or not user["refresh_token_enc"]:
        raise NotConnected(RECONNECT)
    creds = Credentials(
        token=None,
        refresh_token=decrypt(user["refresh_token_enc"], user["email"]),
        client_id=os.environ["GOOGLE_WEB_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_WEB_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    try:
        _refresh(creds)
    except RefreshError:
        store.disconnect_user(user_id)  # next /mcp call gets 401 and the client re-runs sign-in
        raise NotConnected(RECONNECT)
    return creds
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_users.py -q`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add src/gmail_attachments_mcp/identity.py src/gmail_attachments_mcp/users.py tests/test_users.py
git commit -m "feat: per-user Google credentials with dead-token handling" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Wire tools to the calling user (`server.py`)

**Files:**
- Modify: `src/gmail_attachments_mcp/server.py` (imports, `_service`, tool decorators)
- Test: `tests/test_server_wiring.py`

**Interfaces:**
- Consumes: `identity.current_user`, `identity.OWNER`, `users.credentials_for`, `store.get_store`.
- Produces: `server._gmail_client(user_id) -> Gmail service` (tests patch this), `server._service()` (uses `current_user`, falls back to `OWNER`), `server.audited(fn)` decorator (rate limit plus audit log for non-owner users).

- [ ] **Step 1: Write the failing tests** (`tests/test_server_wiring.py`)

```python
import asyncio

import pytest

from gmail_attachments_mcp import server
from gmail_attachments_mcp.identity import OWNER, current_user
from gmail_attachments_mcp.store import MemoryStore, set_store


def _as(user_id):
    return current_user.set(user_id)


def test_service_is_built_for_the_calling_user(monkeypatch):
    seen = []
    monkeypatch.setattr(server, "_gmail_client", lambda uid: seen.append(uid) or "svc")
    token = _as("u1")
    try:
        assert server._service() == "svc"
    finally:
        current_user.reset(token)
    assert seen == ["u1"]


def test_service_defaults_to_the_owner(monkeypatch):
    seen = []
    monkeypatch.setattr(server, "_gmail_client", lambda uid: seen.append(uid) or "svc")
    server._service()
    assert seen == [OWNER]


def test_tools_take_no_user_argument():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert {"list_attachments", "get_attachment_base64", "read_attachment_text"} <= names
    for tool in tools:
        assert "user_id" not in tool.inputSchema["properties"]
        assert "message_id" in tool.inputSchema["properties"]


def test_audited_logs_calls_and_rate_limits(monkeypatch):
    store = MemoryStore()
    set_store(store)
    monkeypatch.setenv("GMAIL_ATT_RATE_LIMIT", "2")
    calls = []

    @server.audited
    def fake(message_id: str):
        calls.append(message_id)
        return "ok"

    token = _as("u1")
    try:
        assert fake("m1") == "ok"
        assert fake("m2") == "ok"
        with pytest.raises(RuntimeError, match="Rate limit"):
            fake("m3")
    finally:
        current_user.reset(token)
        set_store(None)
    assert calls == ["m1", "m2"]
    assert store.count_calls("u1", 60) == 2
    assert store.audit[0][3] == "m1"


def test_audited_logs_failures(monkeypatch):
    store = MemoryStore()
    set_store(store)

    @server.audited
    def broken(message_id: str):
        raise ValueError("nope")

    token = _as("u1")
    try:
        with pytest.raises(ValueError):
            broken("m1")
    finally:
        current_user.reset(token)
        set_store(None)
    assert store.audit[0][4] is False


def test_audited_skips_the_owner_and_never_touches_the_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    set_store(None)

    @server.audited
    def fake(message_id: str):
        return "ok"

    assert fake("m1") == "ok"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_server_wiring.py -q`
Expected: FAIL, `AttributeError: module 'gmail_attachments_mcp.server' has no attribute '_gmail_client'`.

- [ ] **Step 3: Edit `server.py` imports**

Replace

```python
import base64
import io
import os
import re
from pathlib import Path

from googleapiclient.discovery import build
from mcp.server.fastmcp import FastMCP

from .auth import get_credentials
```

with

```python
import base64
import functools
import io
import os
import re
from pathlib import Path

from googleapiclient.discovery import build
from mcp.server.fastmcp import FastMCP

from .identity import OWNER, current_user
from .store import get_store
from .users import credentials_for
```

- [ ] **Step 4: Replace `_service()` and add `audited`**

Replace

```python
def _service():
    return build("gmail", "v1", credentials=get_credentials(), cache_discovery=False)
```

with

```python
def _gmail_client(user_id: str):
    return build("gmail", "v1", credentials=credentials_for(user_id), cache_discovery=False)


def _service():
    return _gmail_client(current_user.get() or OWNER)


def audited(fn):
    """Rate-limit and audit-log tool calls made by hosted users. The owner (static token or
    local mode) is exempt, so local mode never needs a database."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user_id = current_user.get()
        if user_id is None or user_id == OWNER:
            return fn(*args, **kwargs)
        store = get_store()
        limit = int(os.environ.get("GMAIL_ATT_RATE_LIMIT", "60"))
        if store.count_calls(user_id, 60) >= limit:
            raise RuntimeError("Rate limit: too many calls in the last minute. Wait a moment and retry.")
        message_id = kwargs.get("message_id") or (args[0] if args else None)
        ok = False
        try:
            result = fn(*args, **kwargs)
            ok = True
            return result
        finally:
            store.log_call(user_id, fn.__name__, message_id, ok)

    return wrapper
```

- [ ] **Step 5: Add `@audited` to the tools**

Directly under each of these three existing decorators add `@audited`:

```python
@mcp.tool()
@audited
def list_attachments(message_id: str) -> str:
```
```python
@mcp.tool()
@audited
def get_attachment_base64(message_id: str, filename: str, index: int = 0) -> str:
```
```python
@mcp.tool()
@audited
def read_attachment_text(
```

And change the local-only registration:

```python
if not REMOTE:  # hosted server has no persistent disk
    mcp.tool()(audited(download_attachment))
```

- [ ] **Step 6: Run to verify pass**

Run: `uv run pytest tests/test_server_wiring.py -q`
Expected: 5 passed. If `test_tools_take_no_user_argument` fails because the schema lost parameters, FastMCP is not following `__wrapped__`; fix by keeping `functools.wraps` and confirming `inspect.signature(server.list_attachments)` shows `message_id`.

- [ ] **Step 7: Confirm local mode is unchanged**

Run: `uv run pytest -q`
Expected: all earlier tests still pass.

- [ ] **Step 8: Commit**

```bash
git add src/gmail_attachments_mcp/server.py tests/test_server_wiring.py
git commit -m "feat: tools act as the calling user, with rate limit and audit log" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: OAuth server (provider, Google sign-in, callback)

**Files:**
- Create: `src/gmail_attachments_mcp/oauth.py`, `tests/fakes.py`
- Test: `tests/test_oauth.py`

**Interfaces:**
- Consumes: `store` methods (Task 3), `crypto` (Task 2).
- Produces:
  - `oauth.Settings(base_url, google_client_id, google_client_secret)` with `.callback_url`, `.resource_url`, `Settings.from_env()` (reads `PUBLIC_BASE_URL`, `GOOGLE_WEB_CLIENT_ID`, `GOOGLE_WEB_CLIENT_SECRET`)
  - `oauth.GoogleLogin(settings)` with `authorization_url(state) -> str`, `exchange_code(code) -> dict`, `email_for(access_token) -> tuple[str, bool]`, `revoke(refresh_token) -> None`
  - `oauth.GmailOAuthProvider(store, google, settings)`: implements the SDK's `OAuthAuthorizationServerProvider`; also `.store`, `.google`, `async disconnect(user_id)`
  - `oauth.build_routes(provider, settings) -> list[starlette Route]`: SDK routes (`/.well-known/oauth-authorization-server`, `/authorize`, `/token`, `/register`, `/revoke`), `/.well-known/oauth-protected-resource/mcp`, and `GET /google/callback`
  - `oauth.MCP_SCOPE = "gmail.readonly"`
  - `tests/fakes.FakeGoogle` (same four methods as `GoogleLogin`, no network)

- [ ] **Step 1: Create `tests/fakes.py`**

```python
from urllib.parse import urlencode


class FakeGoogle:
    """Stands in for oauth.GoogleLogin. No network."""

    def __init__(self):
        self.email = "a@example.com"
        self.verified = True
        self.refresh = "rt-a"
        self.revoked = []

    def authorization_url(self, state):
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({"state": state})

    def exchange_code(self, code):
        tokens = {"access_token": "google-access"}
        if self.refresh:
            tokens["refresh_token"] = self.refresh
        return tokens

    def email_for(self, access_token):
        return self.email, self.verified

    def revoke(self, refresh_token):
        self.revoked.append(refresh_token)
```

- [ ] **Step 2: Write the failing tests** (`tests/test_oauth.py`)

```python
import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.provider import AuthorizationParams, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from starlette.applications import Starlette
from starlette.testclient import TestClient

from gmail_attachments_mcp.crypto import decrypt, hash_token, sign_state, verify_state
from gmail_attachments_mcp.oauth import GmailOAuthProvider, GoogleLogin, Settings, build_routes
from gmail_attachments_mcp.store import MemoryStore

from .fakes import FakeGoogle

SETTINGS = Settings("https://mcp.example.com", "gid", "gsecret")


@pytest.fixture
def env():
    store, google = MemoryStore(), FakeGoogle()
    provider = GmailOAuthProvider(store, google, SETTINGS)
    client = OAuthClientInformationFull(
        client_id="c1", redirect_uris=["http://localhost/cb"], token_endpoint_auth_method="none"
    )
    asyncio.run(provider.register_client(client))
    http = TestClient(Starlette(routes=build_routes(provider, SETTINGS)), follow_redirects=False)
    return SimpleNamespace(store=store, google=google, provider=provider, client=client, http=http)


def good_state(ttl=600, **over):
    payload = {"cid": "c1", "ru": "http://localhost/cb", "rue": True, "cc": "chal",
               "st": "xyz", "sc": ["gmail.readonly"], "res": None}
    payload.update(over)
    return sign_state(payload, ttl=ttl)


def finish(env, state=None):
    return env.http.get("/google/callback", params={"code": "g1", "state": state or good_state()})


def query(resp):
    return parse_qs(urlparse(resp.headers["location"]).query)


def exchange(env):
    env.store.add_allowed("a@example.com")
    code = query(finish(env))["code"][0]
    auth_code = asyncio.run(env.provider.load_authorization_code(env.client, code))
    tokens = asyncio.run(env.provider.exchange_authorization_code(env.client, auth_code))
    return code, auth_code, tokens


def test_authorize_sends_the_user_to_google_with_signed_state(env):
    params = AuthorizationParams(
        state="xyz", scopes=None, code_challenge="chal",
        redirect_uri="http://localhost/cb", redirect_uri_provided_explicitly=True,
    )
    url = asyncio.run(env.provider.authorize(env.client, params))
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    payload = verify_state(query_from(url)["state"][0])
    assert payload["cid"] == "c1" and payload["cc"] == "chal" and payload["st"] == "xyz"


def query_from(url):
    return parse_qs(urlparse(url).query)


def test_google_url_asks_for_offline_readonly_access():
    q = query_from(GoogleLogin(SETTINGS).authorization_url("S"))
    assert q["client_id"] == ["gid"]
    assert q["redirect_uri"] == ["https://mcp.example.com/google/callback"]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert "https://www.googleapis.com/auth/gmail.readonly" in q["scope"][0]
    assert q["state"] == ["S"]


def test_callback_issues_code_for_allowed_email(env):
    env.store.add_allowed("a@example.com")
    resp = finish(env)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("http://localhost/cb?")
    q = query(resp)
    assert q["state"] == ["xyz"]
    row = env.store.get_auth_code(hash_token(q["code"][0]))
    user = env.store.get_user(row["user_id"])
    assert decrypt(user["refresh_token_enc"], "a@example.com") == "rt-a"
    assert row["code_challenge"] == "chal" and row["client_id"] == "c1"


def test_callback_rejects_email_not_on_allowlist(env):
    resp = finish(env)
    assert resp.status_code == 403
    assert "not approved" in resp.text.lower()
    assert env.store.users == {} and env.store.codes == {}


def test_callback_rejects_unverified_email(env):
    env.store.add_allowed("a@example.com")
    env.google.verified = False
    resp = finish(env)
    assert resp.status_code == 403
    assert env.store.users == {} and env.store.codes == {}


def test_callback_needs_offline_access(env):
    env.store.add_allowed("a@example.com")
    env.google.refresh = None
    resp = finish(env)
    assert resp.status_code == 400
    assert "offline access" in resp.text
    assert env.store.codes == {}


def test_callback_rejects_bad_state(env):
    env.store.add_allowed("a@example.com")
    body, sig = good_state().rsplit(".", 1)
    for state in ["garbage", "", f"{body}x.{sig}", good_state(ttl=-1)]:
        resp = env.http.get("/google/callback", params={"code": "g1", "state": state})
        assert resp.status_code == 400, state
    assert env.store.users == {} and env.store.codes == {}


def test_callback_passes_google_denial_back_to_the_client(env):
    resp = env.http.get("/google/callback", params={"error": "access_denied", "state": good_state()})
    assert resp.status_code == 302
    q = query(resp)
    assert q["error"] == ["access_denied"] and q["state"] == ["xyz"]


def test_auth_code_cannot_be_reused(env):
    code, auth_code, tokens = exchange(env)
    assert tokens.refresh_token and tokens.expires_in == 3600
    with pytest.raises(TokenError):
        asyncio.run(env.provider.exchange_authorization_code(env.client, auth_code))
    assert asyncio.run(env.provider.load_authorization_code(env.client, code)) is None


def test_auth_code_is_bound_to_its_client(env):
    env.store.add_allowed("a@example.com")
    code = query(finish(env))["code"][0]
    other = OAuthClientInformationFull(
        client_id="c2", redirect_uris=["http://localhost/cb"], token_endpoint_auth_method="none"
    )
    assert asyncio.run(env.provider.load_authorization_code(other, code)) is None


def test_rotated_refresh_token_cannot_be_reused(env):
    _, _, tokens = exchange(env)
    old = asyncio.run(env.provider.load_refresh_token(env.client, tokens.refresh_token))
    new = asyncio.run(env.provider.exchange_refresh_token(env.client, old, []))
    assert new.refresh_token != tokens.refresh_token
    assert asyncio.run(env.provider.load_refresh_token(env.client, tokens.refresh_token)) is None
    with pytest.raises(TokenError):
        asyncio.run(env.provider.exchange_refresh_token(env.client, old, []))
    assert asyncio.run(env.provider.load_access_token(new.access_token)).client_id == "c1"


def test_access_token_resolves_to_its_user(env):
    _, auth_code, tokens = exchange(env)
    access = asyncio.run(env.provider.load_access_token(tokens.access_token))
    assert access.subject == auth_code.subject
    assert asyncio.run(env.provider.load_access_token("not-a-token")) is None


def test_revoke_disconnects_the_user_and_google(env):
    _, auth_code, tokens = exchange(env)
    access = asyncio.run(env.provider.load_access_token(tokens.access_token))
    asyncio.run(env.provider.revoke_token(access))
    assert env.google.revoked == ["rt-a"]
    assert env.store.get_user(auth_code.subject)["refresh_token_enc"] is None
    assert asyncio.run(env.provider.load_access_token(tokens.access_token)) is None
    assert asyncio.run(env.provider.load_refresh_token(env.client, tokens.refresh_token)) is None
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_oauth.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'gmail_attachments_mcp.oauth'`.

- [ ] **Step 4: Implement `src/gmail_attachments_mcp/oauth.py`**

```python
"""OAuth 2.1 authorization server for the hosted MCP. The MCP SDK supplies the endpoint
handlers (register, authorize, token, revoke, metadata); this module supplies the provider
behind them, Google sign-in, and the /google/callback leg."""
import asyncio
import os
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from .crypto import decrypt, encrypt, hash_token, new_token, sign_state, verify_state

MCP_SCOPE = "gmail.readonly"
GOOGLE_SCOPES = "openid email https://www.googleapis.com/auth/gmail.readonly"
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
CODE_TTL = 300

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"


@dataclass(frozen=True)
class Settings:
    base_url: str
    google_client_id: str
    google_client_secret: str

    @property
    def callback_url(self) -> str:
        return f"{self.base_url}/google/callback"

    @property
    def resource_url(self) -> str:
        return f"{self.base_url}/mcp"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            base_url=os.environ["PUBLIC_BASE_URL"].rstrip("/"),
            google_client_id=os.environ["GOOGLE_WEB_CLIENT_ID"],
            google_client_secret=os.environ["GOOGLE_WEB_CLIENT_SECRET"],
        )


class GoogleLogin:
    """The only code that talks to Google's OAuth endpoints."""

    def __init__(self, settings: Settings):
        self._s = settings

    def authorization_url(self, state: str) -> str:
        return GOOGLE_AUTH_URL + "?" + urlencode({
            "client_id": self._s.google_client_id,
            "redirect_uri": self._s.callback_url,
            "response_type": "code",
            "scope": GOOGLE_SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        })

    def exchange_code(self, code: str) -> dict:
        resp = httpx.post(GOOGLE_TOKEN_URL, timeout=15, data={
            "code": code,
            "client_id": self._s.google_client_id,
            "client_secret": self._s.google_client_secret,
            "redirect_uri": self._s.callback_url,
            "grant_type": "authorization_code",
        })
        resp.raise_for_status()
        return resp.json()

    def email_for(self, access_token: str) -> tuple[str, bool]:
        resp = httpx.get(GOOGLE_USERINFO_URL, timeout=15,
                         headers={"Authorization": f"Bearer {access_token}"})
        resp.raise_for_status()
        data = resp.json()
        return data["email"].lower(), bool(data.get("email_verified"))

    def revoke(self, refresh_token: str) -> None:
        try:
            httpx.post(GOOGLE_REVOKE_URL, timeout=15, data={"token": refresh_token})
        except httpx.HTTPError:
            pass  # best effort; the stored copy is wiped regardless


async def _t(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


class GmailOAuthProvider:
    def __init__(self, store, google, settings: Settings):
        self.store, self.google, self._settings = store, google, settings

    async def get_client(self, client_id: str):
        raw = await _t(self.store.get_client, client_id)
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await _t(self.store.save_client, client_info.client_id, client_info.model_dump_json())

    async def authorize(self, client, params: AuthorizationParams) -> str:
        state = sign_state({
            "cid": client.client_id,
            "ru": str(params.redirect_uri),
            "rue": params.redirect_uri_provided_explicitly,
            "cc": params.code_challenge,
            "st": params.state,
            "sc": params.scopes or [MCP_SCOPE],
            "res": params.resource,
        })
        return self.google.authorization_url(state)

    async def load_authorization_code(self, client, authorization_code: str):
        row = await _t(self.store.get_auth_code, hash_token(authorization_code))
        if not row or row["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=row["scopes"],
            expires_at=row["expires_at"],
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=row["redirect_uri_provided_explicitly"],
            resource=row["resource"],
            subject=row["user_id"],
        )

    async def exchange_authorization_code(self, client, authorization_code) -> OAuthToken:
        if not await _t(self.store.consume_auth_code, hash_token(authorization_code.code)):
            raise TokenError("invalid_grant", "authorization code already used or expired")
        return await self._issue(
            authorization_code.subject, client.client_id, authorization_code.scopes,
            authorization_code.resource,
        )

    async def load_refresh_token(self, client, refresh_token: str):
        row = await _t(self.store.get_token, hash_token(refresh_token), "refresh")
        if not row or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token, client_id=row["client_id"], scopes=row["scopes"],
            expires_at=int(row["expires_at"]), resource=row["resource"], subject=row["user_id"],
        )

    async def exchange_refresh_token(self, client, refresh_token, scopes) -> OAuthToken:
        if not await _t(self.store.revoke_token, hash_token(refresh_token.token)):
            raise TokenError("invalid_grant", "refresh token already used or revoked")
        return await self._issue(
            refresh_token.subject, client.client_id, scopes or refresh_token.scopes,
            refresh_token.resource,
        )

    async def load_access_token(self, token: str):
        row = await _t(self.store.get_token, hash_token(token), "access")
        if not row:
            return None
        return AccessToken(
            token=token, client_id=row["client_id"], scopes=row["scopes"],
            expires_at=int(row["expires_at"]), resource=row["resource"], subject=row["user_id"],
        )

    async def revoke_token(self, token) -> None:
        """Revoking any MCP token disconnects the user: Google token revoked and wiped, all
        MCP tokens revoked. A user signs in again to reconnect."""
        if token.subject:
            await self.disconnect(token.subject)

    async def disconnect(self, user_id: str) -> None:
        user = await _t(self.store.get_user, user_id)
        if user and user["refresh_token_enc"]:
            refresh = decrypt(user["refresh_token_enc"], user["email"])
            await _t(self.google.revoke, refresh)
        await _t(self.store.disconnect_user, user_id)

    async def _issue(self, user_id, client_id, scopes, resource) -> OAuthToken:
        access, refresh, now = new_token(), new_token(), time.time()
        for token, kind, ttl in ((access, "access", ACCESS_TTL), (refresh, "refresh", REFRESH_TTL)):
            await _t(
                self.store.save_token, hash_token(token),
                user_id=user_id, client_id=client_id, kind=kind, scopes=scopes,
                resource=resource, expires_at=now + ttl,
            )
        return OAuthToken(
            access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
            scope=" ".join(scopes), refresh_token=refresh,
        )


def _page(message: str, status: int) -> HTMLResponse:
    html = (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>Gmail attachments</title><body style='font-family:sans-serif;max-width:32rem;"
        f"margin:3rem auto;padding:0 1rem'><p>{message}</p></body>"
    )
    return HTMLResponse(html, status_code=status)


def build_routes(provider: GmailOAuthProvider, settings: Settings) -> list[Route]:
    routes = create_auth_routes(
        provider,
        issuer_url=AnyHttpUrl(settings.base_url),
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[MCP_SCOPE], default_scopes=[MCP_SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    routes += create_protected_resource_routes(
        resource_url=AnyHttpUrl(settings.resource_url),
        authorization_servers=[AnyHttpUrl(settings.base_url)],
        scopes_supported=[MCP_SCOPE],
        resource_name="Gmail attachments",
    )

    async def google_callback(request):
        params = request.query_params
        payload = verify_state(params.get("state", ""))
        if payload is None:
            return _page("This sign-in link is invalid or has expired. Start again from your app.", 400)
        if params.get("error"):
            return RedirectResponse(
                construct_redirect_uri(payload["ru"], error="access_denied", state=payload["st"]), 302
            )
        code = params.get("code")
        if not code:
            return _page("Google did not return a sign-in code. Start again from your app.", 400)
        try:
            tokens = await _t(provider.google.exchange_code, code)
            email, verified = await _t(provider.google.email_for, tokens["access_token"])
        except Exception:
            return _page("Could not complete sign-in with Google. Try again in a moment.", 502)
        email = email.lower()
        if not verified or not await _t(provider.store.is_allowed, email):
            return _page("This Google account is not approved for this service.", 403)
        refresh = tokens.get("refresh_token")
        if not refresh:
            return _page(
                "Google did not grant offline access. Remove this app at "
                "myaccount.google.com/permissions, then try again.", 400,
            )
        user_id = await _t(provider.store.upsert_user, email, encrypt(refresh, email))
        auth_code = new_token()
        await _t(
            provider.store.save_auth_code, hash_token(auth_code),
            client_id=payload["cid"], user_id=user_id, code_challenge=payload["cc"],
            redirect_uri=payload["ru"], redirect_uri_provided_explicitly=payload["rue"],
            resource=payload["res"], scopes=payload["sc"], expires_at=time.time() + CODE_TTL,
        )
        return RedirectResponse(
            construct_redirect_uri(payload["ru"], code=auth_code, state=payload["st"]), 302
        )

    routes.append(Route("/google/callback", google_callback, methods=["GET"]))
    return routes
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_oauth.py -q`
Expected: 15 passed. If a test fails on SDK model details (for example `OAuthClientInformationFull` requiring more fields, or `AnyHttpUrl` adding a trailing slash to the issuer), adjust the test fixture or `build_routes`, not the security behaviour.

- [ ] **Step 6: Commit**

```bash
git add src/gmail_attachments_mcp/oauth.py tests/fakes.py tests/test_oauth.py
git commit -m "feat: OAuth 2.1 server with Google sign-in, allowlist and token rotation" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Dispatcher and end-to-end flow (`serverless.py`)

**Files:**
- Modify: `src/gmail_attachments_mcp/serverless.py` (full rewrite)
- Test: `tests/test_flow.py`

**Interfaces:**
- Consumes: `oauth.build_routes`, `oauth.GmailOAuthProvider`, `oauth.GoogleLogin`, `oauth.Settings`, `identity.current_user`, `store.get_store`.
- Produces: `serverless.build_app(store, google, settings) -> ASGI callable` (tests use this); `serverless.app` (lazy, from env; unchanged export for `app.py`).

- [ ] **Step 1: Write the failing tests** (`tests/test_flow.py`)

```python
import base64
import hashlib

import pytest
from starlette.testclient import TestClient

from gmail_attachments_mcp import server, users
from gmail_attachments_mcp.crypto import hash_token
from gmail_attachments_mcp.identity import current_user
from gmail_attachments_mcp.oauth import Settings
from gmail_attachments_mcp.serverless import build_app
from gmail_attachments_mcp.store import MemoryStore, set_store

from .fakes import FakeGoogle

SETTINGS = Settings("https://mcp.example.com", "gid", "gsecret")
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GOOGLE_WEB_CLIENT_ID", "gid")
    monkeypatch.setenv("GOOGLE_WEB_CLIENT_SECRET", "gsecret")
    store, google = MemoryStore(), FakeGoogle()
    set_store(store)
    http = TestClient(build_app(store, google, SETTINGS), follow_redirects=False)
    yield type("Env", (), {"store": store, "google": google, "http": http})
    set_store(None)


def connect(env, email, refresh):
    """Run the full OAuth flow as one user; returns (client_id, access, refresh)."""
    env.google.email, env.google.refresh = email, refresh
    reg = env.http.post("/register", json={
        "redirect_uris": ["http://localhost/cb"], "client_name": "test",
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
    })
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]
    auth = env.http.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": "http://localhost/cb",
        "code_challenge": CHALLENGE, "code_challenge_method": "S256", "state": "s1",
    })
    assert auth.status_code == 302 and "accounts.google.com" in auth.headers["location"]
    google_state = auth.headers["location"].split("state=")[1]
    back = env.http.get("/google/callback", params={"code": "g", "state": google_state})
    assert back.status_code == 302, back.text
    location = back.headers["location"]
    assert location.startswith("http://localhost/cb?") and "state=s1" in location
    code = location.split("code=")[1].split("&")[0]
    tok = env.http.post("/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "redirect_uri": "http://localhost/cb", "code_verifier": VERIFIER,
    })
    assert tok.status_code == 200, tok.text
    body = tok.json()
    return client_id, body["access_token"], body["refresh_token"]


def call(env, token, name, **arguments):
    resp = env.http.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    return resp


def test_health_needs_no_auth(env):
    assert env.http.get("/health").text == "ok"


def test_mcp_without_token_points_at_oauth_discovery(env):
    resp = env.http.post("/mcp", headers=MCP_HEADERS, json={})
    assert resp.status_code == 401
    assert "resource_metadata=" in resp.headers["www-authenticate"]
    assert "/.well-known/oauth-protected-resource/mcp" in resp.headers["www-authenticate"]


def test_discovery_documents(env):
    meta = env.http.get("/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"].rstrip("/") == "https://mcp.example.com/mcp"
    server_meta = env.http.get("/.well-known/oauth-authorization-server").json()
    assert server_meta["code_challenge_methods_supported"] == ["S256"]
    assert server_meta["registration_endpoint"].endswith("/register")


def test_full_flow_then_tool_call(env, monkeypatch):
    env.store.add_allowed("a@example.com")
    _, access, _ = connect(env, "a@example.com", "rt-a")
    monkeypatch.setattr(server, "_gmail_client", lambda uid: object())
    monkeypatch.setattr(server, "_attachments", lambda svc, mid: [])
    resp = call(env, access, "list_attachments", message_id="m1")
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["isError"] is False
    assert result["content"][0]["text"] == "No attachments."


def test_users_only_reach_their_own_mailbox(env, monkeypatch):
    env.store.add_allowed("a@example.com")
    env.store.add_allowed("b@example.com")
    _, access_a, _ = connect(env, "a@example.com", "rt-a")
    _, access_b, _ = connect(env, "b@example.com", "rt-b")
    uid_a = env.store.get_token(hash_token(access_a), "access")["user_id"]
    uid_b = env.store.get_token(hash_token(access_b), "access")["user_id"]
    assert uid_a != uid_b
    seen = []
    monkeypatch.setattr(server, "_gmail_client", lambda uid: seen.append(uid) or object())
    monkeypatch.setattr(server, "_attachments", lambda svc, mid: [])
    call(env, access_a, "list_attachments", message_id="m1")
    call(env, access_b, "list_attachments", message_id="m1")
    call(env, access_a, "list_attachments", message_id="m2")
    assert seen == [uid_a, uid_b, uid_a]
    assert current_user.get() is None


def test_not_allowlisted_account_never_gets_a_token(env):
    env.google.email, env.google.refresh = "stranger@example.com", "rt-x"
    reg = env.http.post("/register", json={
        "redirect_uris": ["http://localhost/cb"], "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
    })
    auth = env.http.get("/authorize", params={
        "response_type": "code", "client_id": reg.json()["client_id"],
        "redirect_uri": "http://localhost/cb", "code_challenge": CHALLENGE,
        "code_challenge_method": "S256", "state": "s1",
    })
    back = env.http.get("/google/callback", params={
        "code": "g", "state": auth.headers["location"].split("state=")[1],
    })
    assert back.status_code == 403
    assert env.store.users == {} and env.store.tokens == {}


def test_expired_or_garbage_token_is_rejected(env):
    for token in ["garbage", ""]:
        resp = env.http.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"}, json={})
        assert resp.status_code == 401


def test_revoke_cuts_off_access(env, monkeypatch):
    env.store.add_allowed("a@example.com")
    client_id, access, _ = connect(env, "a@example.com", "rt-a")
    resp = env.http.post("/revoke", data={"token": access, "client_id": client_id})
    assert resp.status_code == 200
    assert env.google.revoked == ["rt-a"]
    assert call(env, access, "list_attachments", message_id="m1").status_code == 401


def test_refresh_flow_issues_new_tokens(env):
    env.store.add_allowed("a@example.com")
    client_id, access, refresh = connect(env, "a@example.com", "rt-a")
    resp = env.http.post("/token", data={
        "grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"] != access
    again = env.http.post("/token", data={
        "grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id,
    })
    assert again.status_code == 400


def test_dead_google_token_forces_reauth(env, monkeypatch):
    from google.auth.exceptions import RefreshError

    env.store.add_allowed("a@example.com")
    _, access, _ = connect(env, "a@example.com", "rt-a")

    def boom(creds):
        raise RefreshError("invalid_grant")

    monkeypatch.setattr(users, "_refresh", boom)
    first = call(env, access, "list_attachments", message_id="m1")
    result = first.json()["result"]
    assert result["isError"] is True
    assert "Reconnect" in result["content"][0]["text"]
    assert call(env, access, "list_attachments", message_id="m1").status_code == 401


def test_rate_limit_applies_per_user(env, monkeypatch):
    env.store.add_allowed("a@example.com")
    _, access, _ = connect(env, "a@example.com", "rt-a")
    monkeypatch.setenv("GMAIL_ATT_RATE_LIMIT", "1")
    monkeypatch.setattr(server, "_gmail_client", lambda uid: object())
    monkeypatch.setattr(server, "_attachments", lambda svc, mid: [])
    assert call(env, access, "list_attachments", message_id="m1").json()["result"]["isError"] is False
    limited = call(env, access, "list_attachments", message_id="m2").json()["result"]
    assert limited["isError"] is True and "Rate limit" in limited["content"][0]["text"]


def test_static_owner_token_still_works(env, monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", "o" * 40)
    seen = []
    monkeypatch.setattr(server, "_gmail_client", lambda uid: seen.append(uid) or object())
    monkeypatch.setattr(server, "_attachments", lambda svc, mid: [])
    resp = call(env, "o" * 40, "list_attachments", message_id="m1")
    assert resp.status_code == 200
    assert seen == ["owner"]


def test_short_static_token_is_disabled(env, monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", "short")
    assert call(env, "short", "list_attachments", message_id="m1").status_code == 401
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_flow.py -q`
Expected: FAIL, `ImportError: cannot import name 'build_app'`.

- [ ] **Step 3: Rewrite `src/gmail_attachments_mcp/serverless.py`**

```python
"""Serverless entry point (Vercel). Dispatches /mcp (bearer-authenticated) and the OAuth
endpoints. No lifespan events: the MCP session manager is started per request, in stateless
JSON mode, so cold starts and short-lived instances behave."""
import hmac
import os

os.environ["GMAIL_ATT_REMOTE"] = "1"

from mcp.server.auth.routes import build_resource_metadata_url  # noqa: E402
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager  # noqa: E402
from pydantic import AnyHttpUrl  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse, PlainTextResponse  # noqa: E402

from .identity import OWNER, current_user  # noqa: E402
from .oauth import GmailOAuthProvider, GoogleLogin, Settings, build_routes  # noqa: E402
from .server import mcp  # noqa: E402
from .store import get_store  # noqa: E402

REQUIRED = (
    "DATABASE_URL",
    "TOKEN_ENC_KEY",
    "PUBLIC_BASE_URL",
    "GOOGLE_WEB_CLIENT_ID",
    "GOOGLE_WEB_CLIENT_SECRET",
)


def _bearer(scope) -> str:
    given = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
    return given[7:].strip() if given.lower().startswith("bearer ") else ""


def _is_owner_token(token: str) -> bool:
    static = os.environ.get("MCP_AUTH_TOKEN", "")
    return len(static) >= 32 and bool(token) and hmac.compare_digest(token.encode(), static.encode())


def build_app(store, google, settings: Settings):
    provider = GmailOAuthProvider(store, google, settings)
    oauth_app = Starlette(routes=build_routes(provider, settings))
    metadata_url = str(build_resource_metadata_url(AnyHttpUrl(settings.resource_url)))

    def unauthorized(had_token: bool) -> JSONResponse:
        challenge = f'Bearer resource_metadata="{metadata_url}"'
        if had_token:
            challenge = 'Bearer error="invalid_token", ' + challenge[len("Bearer "):]
        return JSONResponse({"error": "unauthorized"}, status_code=401,
                            headers={"WWW-Authenticate": challenge})

    async def resolve_user(token: str):
        if _is_owner_token(token):
            return OWNER
        if not token:
            return None
        access = await provider.load_access_token(token)
        return access.subject if access else None

    async def handle_mcp(scope, receive, send):
        token = _bearer(scope)
        user_id = await resolve_user(token)
        if user_id is None:
            await unauthorized(bool(token))(scope, receive, send)
            return
        marker = current_user.set(user_id)
        try:
            manager = StreamableHTTPSessionManager(app=mcp._mcp_server, stateless=True, json_response=True)
            async with manager.run():
                await manager.handle_request(scope, receive, send)
        finally:
            current_user.reset(marker)

    async def asgi(scope, receive, send):
        if scope["type"] != "http":
            return
        path = scope["path"]
        if path == "/health":
            await PlainTextResponse("ok")(scope, receive, send)
        elif path.rstrip("/") == "/mcp":
            await handle_mcp(scope, receive, send)
        else:
            await oauth_app(scope, receive, send)

    return asgi


_app = None


async def app(scope, receive, send):
    global _app
    if scope["type"] != "http":
        return
    if scope["path"] == "/health":
        await PlainTextResponse("ok")(scope, receive, send)
        return
    missing = [v for v in REQUIRED if not os.environ.get(v)]
    if missing:
        await JSONResponse({"error": f"server misconfigured: {', '.join(missing)} not set"},
                           status_code=500)(scope, receive, send)
        return
    if _app is None:
        settings = Settings.from_env()
        _app = build_app(get_store(), GoogleLogin(settings), settings)
    await _app(scope, receive, send)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_flow.py -q`
Expected: 12 passed. Likely snags and what they mean:
- A tool call returns a protocol error about initialisation: the stateless transport still wants `initialize` first in this SDK build. Send an `initialize` request before the `tools/call` in the `call` helper and keep the same headers.
- `/token` returns 401 `invalid_client` for the public client: the registration body must include `"token_endpoint_auth_method": "none"` (it does); check `register` returned that value.
- Host-header rejection (421/403): the SDK's DNS-rebinding guard is on; construct the manager with `security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False)` in `handle_mcp`, as the deployed server already runs behind Vercel's host.

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -q`
Expected: all tests pass (store Postgres variants skipped unless `TEST_DATABASE_URL` is set).

- [ ] **Step 6: Commit**

```bash
git add src/gmail_attachments_mcp/serverless.py tests/test_flow.py
git commit -m "feat: hosted dispatcher with OAuth discovery and per-user /mcp auth" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Admin CLI, docs, deploy and manual verification

**Files:**
- Create: `src/gmail_attachments_mcp/admin.py`
- Modify: `README.md`, `.vercelignore`
- Test: `tests/test_admin.py`

**Interfaces:**
- Consumes: `store.get_store()` (reads `DATABASE_URL`).
- Produces: `admin.initdb()`, `admin.allow()` CLI entry points (`gmail-attachments-initdb`, `gmail-attachments-allow EMAIL [EMAIL ...]`).

- [ ] **Step 1: Write the failing test** (`tests/test_admin.py`)

```python
import sys

import pytest

from gmail_attachments_mcp import admin
from gmail_attachments_mcp.store import MemoryStore, set_store


def test_allow_adds_each_email(monkeypatch, capsys):
    store = MemoryStore()
    set_store(store)
    monkeypatch.setattr(sys, "argv", ["allow", "A@Example.com", "b@example.com"])
    admin.allow()
    assert store.is_allowed("a@example.com") and store.is_allowed("b@example.com")
    assert "2" in capsys.readouterr().out
    set_store(None)


def test_allow_without_emails_exits_with_usage(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["allow"])
    with pytest.raises(SystemExit) as exc:
        admin.allow()
    assert exc.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_admin.py -q`
Expected: FAIL, `ImportError: cannot import name 'admin'`.

- [ ] **Step 3: Create `src/gmail_attachments_mcp/admin.py`**

```python
"""Admin commands for the hosted multi-user server. Both need DATABASE_URL in the environment."""
import sys

from .store import get_store


def initdb() -> None:
    get_store().init_schema()
    print("Schema ready.")


def allow() -> None:
    emails = sys.argv[1:]
    if not emails:
        print("usage: gmail-attachments-allow EMAIL [EMAIL ...]", file=sys.stderr)
        sys.exit(2)
    store = get_store()
    for email in emails:
        store.add_allowed(email)
    print(f"Allowlisted {len(emails)} address(es).")
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_admin.py -q`
Expected: 2 passed.

- [ ] **Step 5: Slim the deploy bundle.** Append to `.vercelignore`:

```
docs
tests
```

- [ ] **Step 6: Update `README.md`.**

(a) In the intro, replace "Read-only (`gmail.readonly`), runs locally on your machine." with:
`Read-only (`gmail.readonly`). Runs locally on your machine, or hosted on Vercel for yourself or a small allowlist of people.`

(b) In the table under "Which clients can connect to the hosted server", replace the last row with:

```
| Claude.ai web and mobile (custom connectors) | Yes, in multi-user mode (OAuth). Not with the single static token. |
```

and delete the paragraph that begins "Making it work on Claude web and mobile would need".

(c) Replace the paragraph that begins "The server reads one Gmail account" with:
`In single-user mode the server reads one Gmail account: whoever owns the refresh token in its env vars. In multi-user mode (below) each person signs in with their own Google account and only ever sees their own mail.`

(d) Insert this new section before `## Env vars`:

````markdown
## Multi-user hosted mode (allowlist)

The hosted server is also an OAuth 2.1 server. Someone you approve adds your connector URL, signs in with Google once, and then their MCP client can list and read **their own** attachments. Works in Claude web, mobile, Desktop and Code.

How it works: the MCP SDK provides the OAuth endpoints; the server stores each user's Google refresh token encrypted (AES-GCM) in Postgres, issues its own short-lived MCP tokens (1 hour access, 30 day rotating refresh, stored as hashes), and checks the signed-in Google email against an allowlist. Every Gmail call uses the caller's own token. Your static `MCP_AUTH_TOKEN` still works as the owner.

Limits to know about:
- While your Google OAuth app is in "Testing", Google expires each user's refresh token after 7 days. They get a "reconnect" error, then sign in again. Up to 100 test users.
- You hold access to every approved user's mail. Only approve people you trust, keep the database and `TOKEN_ENC_KEY` safe, and tell users they can disconnect any time (revoking the connector wipes their stored token).
- Making this public for anyone needs Google restricted-scope verification and an annual third-party security assessment. Not done.

Setup (once):
1. **Postgres:** Vercel dashboard, Storage, create a Neon database and connect it to the project. This adds `DATABASE_URL`.
2. **Google OAuth client:** in Google Cloud, create an OAuth client of type **Web application** with redirect URI `https://<your-project>.vercel.app/google/callback`. Keep the consent screen on Testing and add each user's Gmail as a test user.
3. **Env vars** (Vercel, production): `PUBLIC_BASE_URL` (`https://<your-project>.vercel.app`, no trailing slash), `GOOGLE_WEB_CLIENT_ID`, `GOOGLE_WEB_CLIENT_SECRET`, `TOKEN_ENC_KEY`. Generate the key without printing it:
   ```
   python3 -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())" | vercel env add TOKEN_ENC_KEY production
   ```
   Losing this key means every user has to reconnect.
4. **Create tables and add people:**
   ```
   vercel env pull .env.local
   uv run --env-file .env.local gmail-attachments-initdb
   uv run --env-file .env.local gmail-attachments-allow friend@gmail.com
   ```
5. `vercel deploy --prod`. Then add `https://<your-project>.vercel.app/mcp` as a custom connector in Claude.

Run the tests with `uv run pytest`. To include the Postgres tests, start a throwaway database and set `TEST_DATABASE_URL`:
```
docker run -d --name gmail-mcp-test-pg -e POSTGRES_PASSWORD=test -p 54329:5432 postgres:16
TEST_DATABASE_URL=postgresql://postgres:test@localhost:54329/postgres uv run pytest
```
````

(e) In `## Env vars`, add after the hosted-mode line: `- Multi-user mode: `DATABASE_URL`, `TOKEN_ENC_KEY`, `PUBLIC_BASE_URL`, `GOOGLE_WEB_CLIENT_ID`, `GOOGLE_WEB_CLIENT_SECRET`, optional `GMAIL_ATT_RATE_LIMIT` (calls per user per minute, default 60)`.

(f) In `## Privacy`, append: `In multi-user hosted mode the operator's database holds each approved user's encrypted Google refresh token and an audit log of tool calls (tool name and message ID only, never attachment content).`

- [ ] **Step 7: Run everything**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit and push the branch**

```bash
git add src/gmail_attachments_mcp/admin.py tests/test_admin.py .vercelignore README.md
git commit -m "feat: admin CLI, multi-user docs and deploy notes" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
git push -u origin multi-user-oauth
```

- [ ] **Step 9: Deploy (manual steps for Sav; secrets stay out of chat).** Do these in order:
  1. Vercel, Storage, create Neon, connect to project (adds `DATABASE_URL`).
  2. Google Cloud: create the **Web application** OAuth client with redirect URI `https://gmail-attachments-mcp.vercel.app/google/callback`; add test users.
  3. `vercel env add PUBLIC_BASE_URL production` (value `https://gmail-attachments-mcp.vercel.app`), then `GOOGLE_WEB_CLIENT_ID` and `GOOGLE_WEB_CLIENT_SECRET`; generate `TOKEN_ENC_KEY` with the piped command from the README.
  4. `vercel env pull .env.local`, then `uv run --env-file .env.local gmail-attachments-initdb`, then `uv run --env-file .env.local gmail-attachments-allow <your second Gmail>`.
  5. `vercel deploy --prod` from the `multi-user-oauth` branch.

- [ ] **Step 10: Verify on the live deployment**
  1. `curl -s https://gmail-attachments-mcp.vercel.app/health` prints `ok`.
  2. `curl -s https://gmail-attachments-mcp.vercel.app/.well-known/oauth-authorization-server` returns JSON with `registration_endpoint` (confirms Vercel deployment protection is not blocking public routes; if it returns an HTML login page, disable protection for production).
  3. `claude mcp list` still shows `gmail-attachments-remote` connected (owner token path).
  4. In Claude web, add a custom connector with URL `https://gmail-attachments-mcp.vercel.app/mcp`, sign in with the allowlisted second Gmail, then ask Claude to list attachments on a message in that mailbox. Repeat on the Claude mobile app.
  5. Sign in with a Gmail that is **not** allowlisted: expect the "not approved" page.
  6. Disconnect the connector in Claude, then confirm a call fails and reconnecting works.
- [ ] **Step 11: Merge**

If everything checks out: `git switch main && git merge --ff-only multi-user-oauth && git push`.

---

## Self-review (done)

- **Spec coverage:** goal and flow (Tasks 6, 7); data model (Task 3: all six tables; the spec's data model has no table for in-flight sign-in state, so it rides in HMAC-signed `state`, which the spec's "OAuth `state` verified" already implies); components (store, crypto, oauth, serverless, server, plus identity, users, admin which the spec's table did not name); security rules (hashes, AES-GCM, exact redirect URIs and PKCE S256 via the SDK, `user_id` only from token, rate limit, 1.5 MB cap, revoke); errors (401 with discovery header, "not approved" page, reconnect error); testing (unit, fake-Google integration, cross-user isolation, manual real-account check in Task 8); stage B hooks (audit log, revoke, allowlist in a table). Open items resolved: reuse SDK `create_auth_routes` with a Postgres provider; Neon steps in Task 8.
- **Spec deviation to confirm:** `revoke` is implemented as "disconnect the user" (revokes Google token, wipes it, revokes all MCP tokens) as the spec says, so signing out of one client signs the user out everywhere.
- **Placeholders:** none; every code step is complete.
- **Type consistency:** store method names and signatures match across Tasks 3, 4, 5, 6, 7 (`get_auth_code`, `consume_auth_code`, `save_token(..., kind, scopes, resource, expires_at)`, `get_token(hash, kind)`, `revoke_token(hash) -> bool`, `disconnect_user`, `count_calls(user_id, since_seconds)`). `build_routes(provider, settings)` is used identically in Tasks 6 and 7. `build_app(store, google, settings)` matches the tests.
- **Review Focus:** all five lines have a named test in the owning task.
