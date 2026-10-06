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


class _BrokenLogStore(MemoryStore):
    def log_call(self, *args, **kwargs):
        raise RuntimeError("db down")


def test_audit_failure_does_not_change_the_outcome():
    set_store(_BrokenLogStore())

    @server.audited
    def good(message_id: str):
        return "ok"

    @server.audited
    def bad(message_id: str):
        raise ValueError("nope")

    token = _as("u1")
    try:
        assert good("m1") == "ok"
        with pytest.raises(ValueError):
            bad("m1")
    finally:
        current_user.reset(token)
        set_store(None)


def test_rate_limit_env_is_sanitised(monkeypatch):
    monkeypatch.setenv("GMAIL_ATT_RATE_LIMIT", "garbage")
    assert server._rate_limit() == 60
    monkeypatch.setenv("GMAIL_ATT_RATE_LIMIT", "0")
    assert server._rate_limit() == 1
    monkeypatch.setenv("GMAIL_ATT_RATE_LIMIT", "-5")
    assert server._rate_limit() == 1
    monkeypatch.setenv("GMAIL_ATT_RATE_LIMIT", "5")
    assert server._rate_limit() == 5


class _ExplodingStore(MemoryStore):
    def count_calls(self, *args, **kwargs):
        raise AssertionError("store touched")

    def log_call(self, *args, **kwargs):
        raise AssertionError("store touched")


def test_audited_skips_explicit_owner():
    set_store(_ExplodingStore())

    @server.audited
    def fake(message_id: str):
        return "ok"

    token = _as(OWNER)
    try:
        assert fake("m1") == "ok"
    finally:
        current_user.reset(token)
        set_store(None)
