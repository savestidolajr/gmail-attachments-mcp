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
    resp = env.http.post("/revoke", data={"token": access, "client_id": client_id, "client_secret": ""})
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
