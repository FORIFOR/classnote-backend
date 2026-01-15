import logging
import os
import uuid
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from app.services.apple import apple_service
from app.util_models import BillingConfirmRequest
from app.routes.billing import _extract_transaction_fields

logger = logging.getLogger("app.debug.appstore")
router = APIRouter(prefix="/debug", tags=["Debug"], include_in_schema=False)


def _runtime_env() -> str:
    return (
        os.getenv("APP_ENV")
        or os.getenv("ENV")
        or os.getenv("ENVIRONMENT")
        or "development"
    )


def _is_production_runtime() -> bool:
    return _runtime_env().lower() == "production"


def _validate_debug_secret(provided: Optional[str]) -> None:
    if _is_production_runtime():
        raise HTTPException(status_code=404, detail="not found")
    secret = os.getenv("APPSTORE_DEBUG_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail="debug secret not configured")
    if not provided or provided != secret:
        raise HTTPException(status_code=401, detail="unauthorized")


@router.post("/appstore/decode")
async def debug_appstore_decode(
    req: BillingConfirmRequest,
    x_appstore_debug_secret: Optional[str] = Header(None, alias="X-Appstore-Debug-Secret"),
):
    _validate_debug_secret(x_appstore_debug_secret)

    if not apple_service.verifier:
        raise HTTPException(status_code=503, detail="App Store verification not configured")

    request_id = str(uuid.uuid4())
    transaction_info, verify_error = apple_service.verify_jws_detailed(req.signedTransaction)
    if not transaction_info:
        logger.warning(
            "debug.appstore.decode.verify_failed requestId=%s error=%s",
            request_id,
            verify_error,
        )
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "requestId": request_id,
                "errorType": (verify_error or {}).get("error_type"),
                "stage": (verify_error or {}).get("stage"),
            },
        )

    fields = _extract_transaction_fields(transaction_info)
    response = {
        "ok": True,
        "requestId": request_id,
        "bundleId": fields.get("bundleId"),
        "environment": fields.get("environment"),
        "productId": fields.get("productId"),
        "transactionId": fields.get("transactionId"),
        "originalTransactionId": fields.get("originalTransactionId"),
        "expiresDate": fields.get("expiresDateMs"),
        "appAccountToken": fields.get("appAccountToken"),
    }
    logger.info("debug.appstore.decode.ok requestId=%s", request_id)
    return response
