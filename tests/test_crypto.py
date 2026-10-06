import base64
import hashlib
import hmac

import pytest
from cryptography.exceptions import InvalidTag

from gmail_attachments_mcp import crypto
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


def test_verify_state_rejects_non_ascii_in_signature():
    """Non-ASCII in signature should return None, not raise TypeError."""
    assert verify_state("abc.é") is None


def test_verify_state_rejects_lone_surrogate():
    """Lone surrogate in signature should return None, not raise UnicodeEncodeError."""
    assert verify_state("abc.\ud800") is None


def test_verify_state_rejects_json_array():
    """Validly signed JSON array should return None (must be dict)."""
    body = base64.urlsafe_b64encode(b"[1,2]").decode().rstrip("=")
    sig = hmac.new(crypto._state_key(), body.encode(), hashlib.sha256).hexdigest()
    assert verify_state(f"{body}.{sig}") is None


def test_verify_state_rejects_dict_without_exp():
    """Validly signed dict without exp should return None."""
    body = base64.urlsafe_b64encode(b'{"a":1}').decode().rstrip("=")
    sig = hmac.new(crypto._state_key(), body.encode(), hashlib.sha256).hexdigest()
    assert verify_state(f"{body}.{sig}") is None
