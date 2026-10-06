import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.provider import AuthorizationParams, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from starlette.applications import Starlette
from starlette.testclient import TestClient

from gmail_attachments_mcp.crypto import decrypt, encrypt, hash_token, sign_state, verify_state
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


def query_from(url):
    return parse_qs(urlparse(url).query)


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


def _assert_disconnected(env, uid, tokens):
    assert env.store.get_user(uid)["refresh_token_enc"] is None
    assert asyncio.run(env.provider.load_access_token(tokens.access_token)) is None
    assert asyncio.run(env.provider.load_refresh_token(env.client, tokens.refresh_token)) is None


def test_revoke_still_wipes_when_google_revoke_fails(env):
    _, auth_code, tokens = exchange(env)
    access = asyncio.run(env.provider.load_access_token(tokens.access_token))

    def boom(refresh_token):
        raise RuntimeError("boom")

    env.google.revoke = boom
    asyncio.run(env.provider.revoke_token(access))
    _assert_disconnected(env, auth_code.subject, tokens)


def test_revoke_still_wipes_when_stored_token_is_undecryptable(env):
    _, auth_code, tokens = exchange(env)
    access = asyncio.run(env.provider.load_access_token(tokens.access_token))
    env.store.users[auth_code.subject]["refresh_token_enc"] = encrypt("x", "other@example.com")
    asyncio.run(env.provider.revoke_token(access))
    assert env.google.revoked == []
    _assert_disconnected(env, auth_code.subject, tokens)


# --- redirect URI allowlist (open dynamic registration must not accept arbitrary callbacks)
from mcp.server.auth.provider import RegistrationError  # noqa: E402


def _register(env, *uris):
    client = OAuthClientInformationFull(
        client_id="reg", redirect_uris=list(uris), token_endpoint_auth_method="none"
    )
    asyncio.run(env.provider.register_client(client))


@pytest.mark.parametrize("uri", [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost/cb",
    "http://localhost:8080/some/path",
    "http://127.0.0.1:33418/callback",
    "http://[::1]:5000/cb",
])
def test_allowed_redirect_uris_register(env, uri):
    _register(env, uri)
    assert env.store.get_client("reg") is not None


@pytest.mark.parametrize("uri", [
    "https://evil.example/cb",
    "https://claude.ai/api/mcp/auth_callback/",
    "https://claude.ai/api/mcp/auth_callback?x=1",
    "http://claude.ai/api/mcp/auth_callback",
    "http://localhost.evil.example/cb",
    "http://localhost@evil.example/cb",
    "https://localhost/cb",
    "http://127.0.0.1.evil.example/",
    "http://evil.example/cb",
    "javascript:alert(1)",
])
def test_bad_redirect_uris_rejected(env, uri):
    with pytest.raises(RegistrationError) as exc:
        _register(env, uri)
    assert exc.value.error == "invalid_redirect_uri"
    assert env.store.get_client("reg") is None


def test_one_bad_uri_rejects_the_whole_registration(env):
    with pytest.raises(RegistrationError):
        _register(env, "http://localhost/cb", "https://evil.example/cb")
    assert env.store.get_client("reg") is None


def test_env_allowlist_adds_exact_entries(env, monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_REDIRECT_URIS", " https://app.example/cb , ,https://other.example/cb")
    _register(env, "https://app.example/cb")
    with pytest.raises(RegistrationError):
        _register(env, "https://app.example/cb/extra")
