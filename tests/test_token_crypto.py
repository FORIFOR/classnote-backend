"""Round-trip + rotation tests for token_crypto."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def reload_module(monkeypatch):
    def _reload(env: dict):
        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)
        from app.services import token_crypto
        importlib.reload(token_crypto)
        return token_crypto
    return _reload


def _gen_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode("ascii")


def test_encrypt_decrypt_round_trip(reload_module):
    key = _gen_key()
    tc = reload_module({"TOKEN_ENCRYPTION_KEY": key, "TOKEN_ENCRYPTION_KEY_PREVIOUS": None})
    assert tc.is_configured() is True
    cipher = tc.encrypt("hello-world")
    assert cipher and cipher != "hello-world"
    assert tc.decrypt(cipher) == "hello-world"


def test_decrypt_rejects_when_not_configured(reload_module):
    tc = reload_module({"TOKEN_ENCRYPTION_KEY": None, "TOKEN_ENCRYPTION_KEY_PREVIOUS": None})
    assert tc.is_configured() is False
    with pytest.raises(RuntimeError):
        tc.encrypt("x")
    with pytest.raises(RuntimeError):
        tc.decrypt("x")


def test_decrypt_falls_back_to_previous_key(reload_module):
    old_key = _gen_key()
    new_key = _gen_key()
    tc_old = reload_module({"TOKEN_ENCRYPTION_KEY": old_key, "TOKEN_ENCRYPTION_KEY_PREVIOUS": None})
    cipher = tc_old.encrypt("rotate-me")
    tc_new = reload_module({"TOKEN_ENCRYPTION_KEY": new_key, "TOKEN_ENCRYPTION_KEY_PREVIOUS": old_key})
    assert tc_new.decrypt(cipher) == "rotate-me"
    rotated = tc_new.rotate(cipher)
    assert tc_new.decrypt(rotated) == "rotate-me"


def test_decrypt_fails_when_no_fallback(reload_module):
    old_key = _gen_key()
    new_key = _gen_key()
    tc_old = reload_module({"TOKEN_ENCRYPTION_KEY": old_key, "TOKEN_ENCRYPTION_KEY_PREVIOUS": None})
    cipher = tc_old.encrypt("orphan")
    tc_new = reload_module({"TOKEN_ENCRYPTION_KEY": new_key, "TOKEN_ENCRYPTION_KEY_PREVIOUS": None})
    with pytest.raises(Exception):
        tc_new.decrypt(cipher)
