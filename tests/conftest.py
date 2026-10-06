import base64

import pytest


@pytest.fixture(autouse=True)
def enc_key(monkeypatch):
    monkeypatch.setenv("TOKEN_ENC_KEY", base64.urlsafe_b64encode(b"k" * 32).decode())
