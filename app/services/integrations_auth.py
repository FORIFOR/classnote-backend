"""
integrations_auth.py — PR3 Basic auth + IP allowlist + enabled gate.

Used by external partner integrations (e.g. Lifeselect) mounted under
/v1/integrations/*. Keeps the classnote Firebase-auth flow completely
untouched; partners use HTTP Basic against credentials stored in Cloud
Run environment variables.

Env contract:
    LIFESELECT_ENABLED         "1" | "true" to serve real 2xx/4xx; anything
                               else → routes return 404 Not Found
    LIFESELECT_BASIC_USER      Basic-auth username
    LIFESELECT_BASIC_PASS      Basic-auth password (long random string)
    LIFESELECT_IP_ALLOWLIST    Comma-separated IPv4 allowlist.
                               Empty string ⇒ allowlist disabled (Basic only).

Notes:
- `hmac.compare_digest` for all credential comparisons (timing-attack safe).
- `X-Forwarded-For` first-hop is trusted on Cloud Run; spec §5 acknowledges
  this is an auxiliary control. Basic auth is the primary line of defense.
- Authorization header is deliberately never persisted by callers — audit
  logs omit it entirely.
"""
from __future__ import annotations

import base64
import hmac
import logging
import os
from typing import Optional

from fastapi import HTTPException, Request, status
from fastapi import Header

logger = logging.getLogger("app.integrations_auth")


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


def lifeselect_enabled() -> bool:
    raw = _env("LIFESELECT_ENABLED", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Enabled gate (routes 404 when disabled so scanners see nothing)
# ---------------------------------------------------------------------------

def ensure_lifeselect_enabled() -> None:
    if not lifeselect_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


# ---------------------------------------------------------------------------
# Basic auth
# ---------------------------------------------------------------------------

def _unauthorized(detail: str = "Unauthorized") -> None:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Basic"},
    )


def verify_lifeselect_basic_auth(
    authorization: Optional[str] = Header(default=None),
) -> None:
    """FastAPI dependency: validate HTTP Basic against Lifeselect credentials."""
    expected_user = _env("LIFESELECT_BASIC_USER")
    expected_pass = _env("LIFESELECT_BASIC_PASS")
    if not expected_user or not expected_pass:
        # Fail closed — do not accept if credentials aren't configured.
        logger.warning("[lifeselect_auth] credentials not configured in env")
        _unauthorized()

    if not authorization or not authorization.startswith("Basic "):
        _unauthorized()
    try:
        encoded = authorization.split(" ", 1)[1]
        decoded = base64.b64decode(encoded).decode("utf-8")
        user, pw = decoded.split(":", 1)
    except Exception:
        _unauthorized("Invalid Authorization header")

    # hmac.compare_digest for constant-time comparison.
    if not hmac.compare_digest(user, expected_user):
        _unauthorized()
    if not hmac.compare_digest(pw, expected_pass):
        _unauthorized()


# ---------------------------------------------------------------------------
# IP allowlist (auxiliary; X-Forwarded-For trusted under Cloud Run)
# ---------------------------------------------------------------------------

def _source_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for") or ""
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host or ""
    return ""


def verify_lifeselect_ip_allowlist(request: Request) -> None:
    """Optional allowlist. Empty / unset env → no filtering (Basic alone)."""
    raw = _env("LIFESELECT_IP_ALLOWLIST", "").strip()
    if not raw:
        return
    allowed = {ip.strip() for ip in raw.split(",") if ip.strip()}
    if not allowed:
        return
    ip = _source_ip(request)
    if ip not in allowed:
        logger.info(
            "[lifeselect_auth] ip %s rejected (allowlist size=%d)",
            ip, len(allowed),
        )
        raise HTTPException(status_code=403, detail="Forbidden")


def get_source_ip(request: Request) -> str:
    """Public helper so route handlers log the same IP used for the check."""
    return _source_ip(request)
