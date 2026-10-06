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
