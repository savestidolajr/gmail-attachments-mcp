import pytest
from cryptography.exceptions import InvalidTag
from google.auth.exceptions import RefreshError, TransportError

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


def test_undecryptable_token_disconnects_the_user(store):
    # Store a user whose token was encrypted under a different context (different email)
    uid = store.upsert_user("a@example.com", encrypt("rt", "someone-else@example.com"))
    with pytest.raises(NotConnected, match="Reconnect"):
        users.credentials_for(uid)
    assert store.get_user(uid)["refresh_token_enc"] is None


def test_transient_refresh_failure_does_not_disconnect(store, monkeypatch):
    def boom(creds):
        raise TransportError("network down")

    monkeypatch.setattr(users, "_refresh", boom)
    uid = store.upsert_user("a@example.com", encrypt("rt", "a@example.com"))
    original_token = store.get_user(uid)["refresh_token_enc"]
    with pytest.raises(TransportError):
        users.credentials_for(uid)
    # User should remain connected (token not cleared)
    assert store.get_user(uid)["refresh_token_enc"] == original_token
