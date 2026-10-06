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
    store.disconnect_user(uid)
    assert store.get_user(uid)["revoked_at"] is not None
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


def test_disconnect_user_invalidates_unused_auth_codes(store):
    uid = store.upsert_user("a@example.com", "enc")
    other = store.upsert_user("b@example.com", "encb")
    _code(store, uid, "h-a")
    _code(store, other, "h-b")
    store.disconnect_user(uid)
    assert store.get_auth_code("h-a") is None
    assert store.consume_auth_code("h-a") is False
    assert store.get_auth_code("h-b") is not None
    assert store.consume_auth_code("h-b") is True


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
