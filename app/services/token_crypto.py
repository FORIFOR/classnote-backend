"""Symmetric token encryption for OAuth access/refresh tokens.

Reads TOKEN_ENCRYPTION_KEY (preferred) or falls back to TOKEN_ENCRYPTION_KEY_BASE64.
The key must be a Fernet-compatible 32-byte url-safe base64 value
(`openssl rand -base64 32`).

Provides:
  - is_configured()  : True if key is loaded
  - encrypt(plain)   : str -> str (Fernet ciphertext)
  - decrypt(cipher)  : str -> str
  - rotate(cipher)   : decrypt with secondary key (TOKEN_ENCRYPTION_KEY_PREVIOUS),
                        then re-encrypt with primary

If the key is not configured, encrypt/decrypt raise RuntimeError so callers
can return 503 cleanly.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger("app.services.token_crypto")

_KEY_ENV = "TOKEN_ENCRYPTION_KEY"
_PREV_KEY_ENV = "TOKEN_ENCRYPTION_KEY_PREVIOUS"


def _load_key(name: str) -> Optional[bytes]:
    raw = os.environ.get(name)
    if not raw:
        return None
    return raw.strip().encode("utf-8")


def _fernet(key: bytes):
    from cryptography.fernet import Fernet  # local import to avoid hard dep at startup

    return Fernet(key)


def is_configured() -> bool:
    return _load_key(_KEY_ENV) is not None


def encrypt(plain: str) -> str:
    key = _load_key(_KEY_ENV)
    if not key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not configured")
    return _fernet(key).encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(cipher: str) -> str:
    if not cipher:
        return ""
    key = _load_key(_KEY_ENV)
    if not key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not configured")
    try:
        return _fernet(key).decrypt(cipher.encode("ascii")).decode("utf-8")
    except Exception:
        prev = _load_key(_PREV_KEY_ENV)
        if not prev:
            raise
        # Try previous key (rotation window)
        return _fernet(prev).decrypt(cipher.encode("ascii")).decode("utf-8")


def rotate(cipher: str) -> str:
    """Decrypt with previous key, re-encrypt with primary key."""
    return encrypt(decrypt(cipher))
