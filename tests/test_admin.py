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
