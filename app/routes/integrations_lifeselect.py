"""
routes/integrations_lifeselect.py — PR3 partner order/cancel endpoints.

Mounted at /v1/integrations/lifeselect/*. The classnote Firebase JWT flow
is intentionally NOT involved; partners authenticate via HTTP Basic against
credentials stored in Cloud Run env vars (see `integrations_auth.py`).

Every request is audit-logged to Firestore `integration_requests` with the
source IP, request body, response body, status, and error_code. The
Authorization header is never persisted.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from app.services.integrations_auth import (
    ensure_lifeselect_enabled,
    get_source_ip,
    verify_lifeselect_basic_auth,
    verify_lifeselect_ip_allowlist,
)
from app.services.integrations_lifeselect import (
    ERR_FORMAT,
    ERR_INTERNAL,
    LifeselectService,
    LifeselectStore,
    make_error,
)
from app.util_models import (
    LifeselectCancelRequest,
    LifeselectCancelResponse,
    LifeselectIssueObject,
    LifeselectOrderRequest,
    LifeselectOrderResponse,
)

logger = logging.getLogger("app.integrations_lifeselect_routes")

router = APIRouter(
    prefix="/v1/integrations/lifeselect",
    tags=["integrations-lifeselect"],
)

# Service/store are stateless aside from Firestore client cache — safe to
# share at module scope.
_service = LifeselectService()
_store = LifeselectStore()


def _log_request(
    *,
    request_type: str,
    request_key: str,
    source_ip: str,
    request_body: Dict[str, Any],
    response_body: Dict[str, Any],
    status_label: str,
    error_code: str,
) -> None:
    """Audit log one request. Authorization header is NOT included."""
    try:
        _store.create_request_log({
            "partner_code": "lifeselect",
            "request_type": request_type,
            "request_key": request_key,
            "request_body": request_body,
            "response_body": response_body,
            "auth_ok": True,
            "source_ip": source_ip,
            "status": status_label,
            "error_code": error_code,
            "created_at": _store._now(),
            "processed_at": _store._now(),
        })
    except Exception as exc:
        # Never fail the partner request because audit log writes failed.
        logger.error("[lifeselect] audit log write failed: %s", exc)


# ---------------------------------------------------------------------------
# POST /orders
# ---------------------------------------------------------------------------

@router.post(
    "/orders",
    response_model=LifeselectOrderResponse,
    dependencies=[
        Depends(ensure_lifeselect_enabled),
        Depends(verify_lifeselect_basic_auth),
    ],
)
async def create_order(request: Request) -> LifeselectOrderResponse:
    verify_lifeselect_ip_allowlist(request)
    source_ip = get_source_ip(request)

    raw_body: Dict[str, Any] = {}
    try:
        raw_body = await request.json()
    except Exception:
        raw_body = {}

    # Explicit schema validation so we can map errors to E002.
    try:
        payload = LifeselectOrderRequest(**(raw_body or {}))
    except ValidationError as exc:
        response = LifeselectOrderResponse(
            rtn=False,
            issue=LifeselectIssueObject(),
            error=make_error(ERR_FORMAT, detail=str(exc.errors()[:1])),
        )
        _log_request(
            request_type="order",
            request_key=str(
                (raw_body or {}).get("identifier", "")
            ) + ":" + str((raw_body or {}).get("link_mng_id", "")),
            source_ip=source_ip,
            request_body=raw_body,
            response_body=response.model_dump(),
            status_label="validation_error",
            error_code=response.error.code,
        )
        return response

    request_key = _service.build_request_key(
        payload.identifier, payload.link_mng_id,
    )
    try:
        response = _service.order(payload)
        _log_request(
            request_type="order",
            request_key=request_key,
            source_ip=source_ip,
            request_body=payload.model_dump(),
            response_body=response.model_dump(),
            status_label="success" if response.rtn else "business_error",
            error_code=response.error.code,
        )
        return response
    except Exception as exc:
        logger.exception("[lifeselect] order failed: %s", exc)
        response = LifeselectOrderResponse(
            rtn=False,
            issue=LifeselectIssueObject(),
            error=make_error(ERR_INTERNAL, detail=str(exc)[:200]),
        )
        _log_request(
            request_type="order",
            request_key=request_key,
            source_ip=source_ip,
            request_body=payload.model_dump(),
            response_body=response.model_dump(),
            status_label="system_error",
            error_code=response.error.code,
        )
        return response


# ---------------------------------------------------------------------------
# POST /cancellations
# ---------------------------------------------------------------------------

@router.post(
    "/cancellations",
    response_model=LifeselectCancelResponse,
    dependencies=[
        Depends(ensure_lifeselect_enabled),
        Depends(verify_lifeselect_basic_auth),
    ],
)
async def cancel_order(request: Request) -> LifeselectCancelResponse:
    verify_lifeselect_ip_allowlist(request)
    source_ip = get_source_ip(request)

    raw_body: Dict[str, Any] = {}
    try:
        raw_body = await request.json()
    except Exception:
        raw_body = {}

    try:
        payload = LifeselectCancelRequest(**(raw_body or {}))
    except ValidationError as exc:
        response = LifeselectCancelResponse(
            rtn=False,
            error=make_error(ERR_FORMAT, detail=str(exc.errors()[:1])),
        )
        _log_request(
            request_type="cancel",
            request_key=str(
                (raw_body or {}).get("identifier", "")
            ) + ":" + str((raw_body or {}).get("link_mng_id", "")),
            source_ip=source_ip,
            request_body=raw_body,
            response_body=response.model_dump(),
            status_label="validation_error",
            error_code=response.error.code,
        )
        return response

    request_key = _service.build_request_key(
        payload.identifier, payload.link_mng_id,
    )
    try:
        response = _service.cancel(payload)
        _log_request(
            request_type="cancel",
            request_key=request_key,
            source_ip=source_ip,
            request_body=payload.model_dump(),
            response_body=response.model_dump(),
            status_label="success" if response.rtn else "business_error",
            error_code=response.error.code,
        )
        return response
    except Exception as exc:
        logger.exception("[lifeselect] cancel failed: %s", exc)
        response = LifeselectCancelResponse(
            rtn=False,
            error=make_error(ERR_INTERNAL, detail=str(exc)[:200]),
        )
        _log_request(
            request_type="cancel",
            request_key=request_key,
            source_ip=source_ip,
            request_body=payload.model_dump(),
            response_body=response.model_dump(),
            status_label="system_error",
            error_code=response.error.code,
        )
        return response
