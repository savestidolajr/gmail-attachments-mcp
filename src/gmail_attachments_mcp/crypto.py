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
