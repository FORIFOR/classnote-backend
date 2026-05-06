"""P0 compatibility helpers — legacy alias logging + request/response shims.

See `deepnote-contracts/migration/backend-p0-compat-fix-plan.md` for the
authoritative scope. Routes that delegate from a legacy path to a
canonical `/v1/*` handler should call ``log_legacy_api_called(request, ...)``
to emit a structured ``legacy_api_called`` event for the deprecation
dashboard.
"""
from app.adapters.compat.legacy_log import log_legacy_api_called

__all__ = ["log_legacy_api_called"]
