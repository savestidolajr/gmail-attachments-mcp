import pytest
from starlette.testclient import TestClient

from gmail_attachments_mcp import server, serverless
from gmail_attachments_mcp.identity import current_user

TOKEN = "o" * 40
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
MULTI_VARS = ("DATABASE_URL", "TOKEN_ENC_KEY", "PUBLIC_BASE_URL",
              "GOOGLE_WEB_CLIENT_ID", "GOOGLE_WEB_CLIENT_SECRET")


def call(http, token=None, scheme="Bearer"):
    headers = dict(MCP_HEADERS)
    if token is not None:
        headers["Authorization"] = f"{scheme} {token}"
    return http.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "list_attachments", "arguments": {"message_id": "m1"}},
    })


@pytest.fixture
def owner_http(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)
    return TestClient(serverless.build_owner_app(), follow_redirects=False)


def test_owner_token_reaches_tool_as_owner(owner_http, monkeypatch):
    seen = []
    monkeypatch.setattr(server, "_gmail_client", lambda uid: seen.append(uid) or object())
    monkeypatch.setattr(server, "_attachments", lambda svc, mid: [])
    resp = call(owner_http, TOKEN)
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["content"][0]["text"] == "No attachments."
    assert seen == ["owner"]
    assert current_user.get() is None


@pytest.mark.parametrize("token", [None, "wrong" * 10])
def test_missing_or_wrong_token_is_401(owner_http, token):
    resp = call(owner_http, token)
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}
    assert resp.headers["www-authenticate"] == "Bearer"


def test_short_static_token_is_disabled(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", "short")
    http = TestClient(serverless.build_owner_app())
    assert call(http, "short").status_code == 401


def test_non_bearer_scheme_is_401(owner_http):
    assert call(owner_http, TOKEN, scheme="Basic").status_code == 401


@pytest.mark.parametrize("path", [
    "/authorize", "/register", "/token", "/revoke",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource/mcp", "/google/callback",
])
def test_no_oauth_routes_in_owner_mode(owner_http, path):
    resp = owner_http.get(path)
    assert resp.status_code == 404
    assert resp.json() == {"error": "not found"}


def test_health_ok(owner_http):
    assert owner_http.get("/health").text == "ok"


@pytest.fixture
def top(monkeypatch):
    for v in MULTI_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(serverless, "_app", None)
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)
    yield TestClient(serverless.app, follow_redirects=False)
    serverless._app = None


def test_top_level_app_is_owner_only_without_database_url(top, monkeypatch):
    monkeypatch.setattr(server, "_gmail_client", lambda uid: object())
    monkeypatch.setattr(server, "_attachments", lambda svc, mid: [])
    assert call(top, TOKEN).status_code == 200
    assert call(top, "nope").status_code == 401
    assert top.get("/register").status_code == 404
    assert top.get("/health").text == "ok"


def test_top_level_app_multi_user_mode_lists_missing_vars(top, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    resp = call(top, TOKEN)
    assert resp.status_code == 500
    err = resp.json()["error"]
    for name in MULTI_VARS[1:]:
        assert name in err
    assert "DATABASE_URL" not in err
    assert top.get("/health").text == "ok"
