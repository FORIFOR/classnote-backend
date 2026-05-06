"""Structured logging for ``legacy_api_called`` events.

Every legacy alias handler should emit one of these per request so the
deprecation dashboard (``tools/legacy_usage_dashboard.py`` etc.) can
quantify residual traffic on retired paths and decide when to actually
remove them.

Spec lines (from the user's compat-layer brief):

```
{
  "event": "legacy_api_called",
  "method": "GET",
  "path": "/folders",
  "canonicalPath": "/v1/folders",
  "accountId": "...",
  "platform": "ios|desktop|unknown",
  "appVersion": "...",
  "userAgent": "...",
  "removeNotBefore": "2026-12-31"
}
```

This module intentionally writes via ``logging.getLogger`` only — not
Firestore — so an audit DB outage cannot cascade and break user-facing
requests.
"""
from __future__ import annotations

import json
import logging
from typing import Mapping, Optional

logger = logging.getLogger("app.compat.legacy_api")


def _detect_platform(user_agent: str) -> str:
    """Cheap heuristic; we don't need perfect classification, just buckets."""
    ua = (user_agent or "").lower()
    if "classnotex" in ua or "deepnote-ios" in ua or "ios" in ua:
        return "ios"
    if "deepnote-desktop" in ua or "electron" in ua:
        return "desktop"
    return "unknown"


def log_legacy_api_called(
    *,
    method: str,
    path: str,
    canonical_path: str,
    headers: Optional[Mapping[str, str]] = None,
    account_id: Optional[str] = None,
    remove_not_before: Optional[str] = None,
) -> None:
    """Emit a structured ``legacy_api_called`` log line.

    Failure to log MUST NOT raise. Callers can pass either FastAPI
    ``request.headers`` (a Mapping) or a plain dict; both work because
    we read with ``.get(...)``.
    """
    try:
        h = headers or {}
        ua = h.get("user-agent") or h.get("User-Agent") or ""
        app_ver = h.get("x-app-version") or h.get("X-App-Version")
        payload = {
            "event": "legacy_api_called",
            "method": method.upper(),
            "path": path,
            "canonicalPath": canonical_path,
            "accountId": account_id,
            "platform": _detect_platform(ua),
            "appVersion": app_ver,
            "userAgent": ua,
            "removeNotBefore": remove_not_before,
        }
        logger.info(json.dumps(payload, ensure_ascii=False))
    except Exception:
        # Audit must never break a user-facing reply.
        logger.exception("[compat] legacy_api log failed (path=%s)", path)
