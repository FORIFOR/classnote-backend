import logging
import os
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, List

from fastapi import APIRouter, Depends, HTTPException, Response
from google.cloud import firestore
from pydantic import BaseModel, Field

from app.dependencies import get_current_user, CurrentUser, CurrentUser
from app.firebase import db
from app.services.apple import apple_service
from app.services.app_config import is_feature_enabled, get_maintenance_error_response
from app.util_models import BillingConfirmRequest, AppStoreNotificationRequest, BillingConfirmResponse
from app.utils.idempotency import idempotency, ResourceAlreadyProcessed


# =============================================================================
# Request/Response Models for Entitlements Sync
# =============================================================================

class EntitlementItem(BaseModel):
    """Single entitlement item from iOS app."""
    product_id: str = Field(..., alias="productId")
    original_transaction_id: str = Field(..., alias="originalTransactionId")
    transaction_id: Optional[str] = Field(None, alias="transactionId")
    expiration_date: Optional[str] = Field(None, alias="expirationDate")

    class Config:
        populate_by_name = True


class EntitlementsSyncRequest(BaseModel):
    """Request to sync entitlements from iOS app."""
    items: List[EntitlementItem]


class EntitlementsSyncResponse(BaseModel):
    """Response after syncing entitlements."""
    ok: bool
    plan: str
    entitled: bool
    synced_count: int = Field(..., alias="syncedCount")
    expires_at: Optional[int] = Field(None, alias="expiresAt")
    details: Optional[List[dict]] = None

    class Config:
        populate_by_name = True


logger = logging.getLogger("app.billing")
router = APIRouter(prefix="/billing")


def _normalize_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    return value


def _get_field(obj: Any, key: str) -> Optional[Any]:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return _normalize_value(obj.get(key))
    return _normalize_value(getattr(obj, key, None))


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ms_to_datetime(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _plan_for_product_id(product_id: Optional[str]) -> str:
    # [SECURITY FIX] Return "free" instead of "basic" when product_id is missing
    if not product_id:
        return "free"
    lowered = product_id.lower()
    if "basic" in lowered or "standard" in lowered:
        return "basic"
    # Default to free for unknown product IDs
    return "free"


def _resolve_status(
    transaction_info: Any,
    renewal_info: Any,
    notification_type: Optional[str],
) -> str:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    revoked_ms = _coerce_int(_get_field(transaction_info, "revocationDate"))
    expires_ms = _coerce_int(_get_field(transaction_info, "expiresDate"))
    grace_ms = _coerce_int(_get_field(renewal_info, "gracePeriodExpiresDate"))
    billing_retry = _get_field(renewal_info, "isInBillingRetryPeriod")

    if notification_type in {"DID_REVOKE", "REFUND"}:
        return "revoked"
    if notification_type in {"EXPIRED", "GRACE_PERIOD_EXPIRED"}:
        return "expired"
    if revoked_ms:
        return "revoked"
    if grace_ms and grace_ms > now_ms:
        return "grace_period"
    if expires_ms and expires_ms <= now_ms:
        return "expired"
    if notification_type == "DID_FAIL_TO_RENEW" or billing_retry:
        return "billing_retry"
    return "active"


def _is_entitled(status: str, expires_ms: Optional[int]) -> bool:
    if status in {"revoked", "expired"}:
        return False
    if expires_ms and expires_ms <= int(datetime.now(timezone.utc).timestamp() * 1000):
        return False
    return True


def _extract_transaction_fields(transaction_info: Any) -> dict:
    purchase_ms = _coerce_int(_get_field(transaction_info, "purchaseDate"))
    expires_ms = _coerce_int(_get_field(transaction_info, "expiresDate"))
    revoked_ms = _coerce_int(_get_field(transaction_info, "revocationDate"))

    return {
        "bundleId": _get_field(transaction_info, "bundleId"),
        "transactionId": _get_field(transaction_info, "transactionId"),
        "originalTransactionId": _get_field(transaction_info, "originalTransactionId"),
        "productId": _get_field(transaction_info, "productId"),
        "appAccountToken": _get_field(transaction_info, "appAccountToken"),
        "environment": _get_field(transaction_info, "environment"),
        "ownershipType": _get_field(transaction_info, "inAppOwnershipType"),
        "transactionReason": _get_field(transaction_info, "transactionReason"),
        "type": _get_field(transaction_info, "type"),
        "purchaseDateMs": purchase_ms,
        "purchaseAt": _ms_to_datetime(purchase_ms),
        "expiresDateMs": expires_ms,
        "expiresAt": _ms_to_datetime(expires_ms),
        "revocationDateMs": revoked_ms,
        "revocationAt": _ms_to_datetime(revoked_ms),
    }


def _extract_renewal_fields(renewal_info: Any) -> dict:
    grace_ms = _coerce_int(_get_field(renewal_info, "gracePeriodExpiresDate"))
    return {
        "autoRenewStatus": _get_field(renewal_info, "autoRenewStatus"),
        "expirationIntent": _get_field(renewal_info, "expirationIntent"),
        "gracePeriodExpiresDateMs": grace_ms,
        "gracePeriodExpiresAt": _ms_to_datetime(grace_ms),
        "isInBillingRetryPeriod": _get_field(renewal_info, "isInBillingRetryPeriod"),
        "offerType": _get_field(renewal_info, "offerType"),
    }


def _lookup_uid(app_account_token: Optional[str], original_transaction_id: Optional[str]) -> Optional[str]:
    uid = None
    if app_account_token:
        token_doc = db.collection("apple_app_account_tokens").document(app_account_token).get()
        if token_doc.exists:
            uid = token_doc.to_dict().get("uid")
    if not uid and original_transaction_id:
        txn_doc = db.collection("apple_transactions").document(original_transaction_id).get()
        if txn_doc.exists:
            uid = txn_doc.to_dict().get("uid")
    return uid


def _runtime_env() -> str:
    return (
        os.getenv("APP_ENV")
        or os.getenv("ENV")
        or os.getenv("ENVIRONMENT")
        or "development"
    )


def _is_production_runtime() -> bool:
    return _runtime_env().lower() == "production"


def _debug_verify_enabled() -> bool:
    flag = os.getenv("APPLE_DEBUG_VERIFY_TRANSACTION_INFO", "false")
    return flag.lower() in {"1", "true", "yes"}


def _jws_meta(signed_payload: str, head_len: int = 24, tail_len: int = 24) -> dict:
    payload = signed_payload or ""
    return {
        "jws_len": len(payload),
        "jws_head": payload[:head_len],
        "jws_tail": payload[-tail_len:] if payload else "",
    }


def _diff_transaction_info(primary: Any, secondary: Any) -> dict:
    fields = [
        "bundleId",
        "environment",
        "productId",
        "transactionId",
        "originalTransactionId",
        "expiresDate",
        "appAccountToken",
    ]
    diff = {}
    for field in fields:
        left = _get_field(primary, field)
        right = _get_field(secondary, field)
        if left != right:
            diff[field] = {"app": left, "server": right}
    return diff


# =============================================================================
# POST /billing/ios/entitlements/sync - Sync entitlements from iOS app
# =============================================================================

@router.post("/ios/entitlements/sync", response_model=EntitlementsSyncResponse)
async def sync_ios_entitlements(
    req: EntitlementsSyncRequest,
    current_user: CurrentUser = Depends(get_current_user),
    response: Response = None,
):
    """
    Sync entitlements from iOS app to server.

    This endpoint is called by the iOS app when:
    - App launches and detects entitlements via Transaction.currentEntitlements
    - User redeems an Offer Code (in-app or externally)
    - Transaction.updates receives a new transaction

    The server will:
    1. Link user_id <-> original_transaction_id in DB
    2. Call Apple's Get All Subscription Statuses API to verify
    3. Update the user's plan in DB
    4. Optionally set appAccountToken if not already set

    This ensures the server is always in sync even if:
    - Offer Code was redeemed outside the app
    - App Store Server Notifications were missed
    """
    request_id = str(uuid.uuid4())
    if response is not None:
        response.headers["X-Request-Id"] = request_id

    if not is_feature_enabled("payment"):
        raise HTTPException(
            status_code=503,
            detail=get_maintenance_error_response("payment"),
        )

    if not req.items:
        return EntitlementsSyncResponse(
            ok=True,
            plan="free",
            entitled=False,
            synced_count=0,
        )

    # Get account ID
    link_doc = db.collection("uid_links").document(current_user.uid).get()
    account_id = link_doc.to_dict().get("accountId") if link_doc.exists else None

    # Generate appAccountToken from account_id (UUID format required by Apple)
    app_account_token = None
    if account_id:
        # Convert account_id to UUID format if not already
        try:
            app_account_token = str(uuid.UUID(account_id))
        except ValueError:
            # If account_id is not UUID, create a deterministic UUID from it
            app_account_token = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"classnote.{account_id}"))

    synced_details = []
    best_plan = "free"
    best_expires_at = None
    is_entitled = False

    for item in req.items:
        original_transaction_id = item.original_transaction_id
        product_id = item.product_id

        logger.info(
            "entitlements_sync_item",
            extra={
                "uid": current_user.uid,
                "accountId": account_id,
                "originalTransactionId": original_transaction_id,
                "productId": product_id,
            }
        )

        # 1. Store user_id <-> original_transaction_id mapping
        db.collection("apple_transactions").document(original_transaction_id).set({
            "uid": current_user.uid,
            "accountId": account_id,
            "productId": product_id,
            "source": "entitlements_sync",
            "lastSyncAt": firestore.SERVER_TIMESTAMP,
        }, merge=True)

        # 2. Call Apple API to get authoritative subscription status
        apple_status = apple_service.get_subscription_status_for_account(
            original_transaction_id,
            expected_app_account_token=app_account_token
        )

        if not apple_status.get("found"):
            logger.warning(
                "entitlements_sync_not_found_in_apple",
                extra={
                    "originalTransactionId": original_transaction_id,
                    "uid": current_user.uid,
                }
            )
            synced_details.append({
                "originalTransactionId": original_transaction_id,
                "status": "not_found",
                "error": "Subscription not found in Apple API",
            })
            continue

        status = apple_status.get("status", "unknown")
        active = apple_status.get("active", False)
        expires_date = apple_status.get("expires_date")
        actual_product_id = apple_status.get("product_id") or product_id
        existing_token = apple_status.get("app_account_token")

        # Determine plan from product_id
        plan = _plan_for_product_id(actual_product_id)
        if not active:
            plan = "free"

        # 3. Set appAccountToken if not already set (links subscription to our user)
        if app_account_token and not existing_token:
            token_set = apple_service.set_app_account_token(
                original_transaction_id,
                app_account_token
            )
            if token_set:
                logger.info(
                    "entitlements_sync_token_set",
                    extra={
                        "originalTransactionId": original_transaction_id,
                        "appAccountToken": app_account_token,
                    }
                )

        # 4. Update entitlements collection
        entitlement_id = f"apple:{original_transaction_id}"
        entitlement_ref = db.collection("entitlements").document(entitlement_id)
        existing_entitlement = entitlement_ref.get()

        entitlement_data = {
            "status": status,
            "plan": plan,
            "productId": actual_product_id,
            "currentPeriodEnd": _ms_to_datetime(expires_date) if expires_date else None,
            "provider": "apple",
            "providerEntitlementId": original_transaction_id,
            "updatedAt": firestore.SERVER_TIMESTAMP,
            "updatedBy": "entitlements_sync",
        }

        if not existing_entitlement.exists:
            entitlement_data["ownerAccountId"] = account_id
            entitlement_data["ownerUserId"] = current_user.uid
            entitlement_data["createdAt"] = firestore.SERVER_TIMESTAMP
        else:
            # Verify ownership
            existing_owner = existing_entitlement.to_dict().get("ownerAccountId")
            if existing_owner and existing_owner != account_id:
                logger.warning(
                    "entitlements_sync_ownership_conflict",
                    extra={
                        "entitlementId": entitlement_id,
                        "existingOwner": existing_owner,
                        "requestingAccount": account_id,
                    }
                )
                synced_details.append({
                    "originalTransactionId": original_transaction_id,
                    "status": "ownership_conflict",
                    "error": "Subscription owned by another account",
                })
                continue

        entitlement_ref.set(entitlement_data, merge=True)

        # 5. Update account plan if this is the best entitlement
        if active and plan != "free":
            is_entitled = True
            if plan == "basic":  # or compare priority
                best_plan = plan
                best_expires_at = expires_date

        synced_details.append({
            "originalTransactionId": original_transaction_id,
            "status": status,
            "plan": plan,
            "active": active,
            "expiresAt": expires_date,
        })

    # 6. Update account with best plan
    if account_id and is_entitled:
        db.collection("accounts").document(account_id).set({
            "plan": best_plan,
            "planExpiresAt": _ms_to_datetime(best_expires_at) if best_expires_at else None,
            "planUpdatedAt": firestore.SERVER_TIMESTAMP,
        }, merge=True)

        db.collection("users").document(current_user.uid).update({
            "plan": best_plan,
            "planUpdatedAt": firestore.SERVER_TIMESTAMP,
        })

    logger.info(
        "entitlements_sync_completed",
        extra={
            "uid": current_user.uid,
            "accountId": account_id,
            "syncedCount": len(synced_details),
            "resultPlan": best_plan,
            "entitled": is_entitled,
        }
    )

    return EntitlementsSyncResponse(
        ok=True,
        plan=best_plan,
        entitled=is_entitled,
        synced_count=len(synced_details),
        expires_at=best_expires_at,
        details=synced_details,
    )


@router.post("/ios/confirm", response_model=BillingConfirmResponse)
async def confirm_ios_purchase(
    req: BillingConfirmRequest,
    current_user: CurrentUser = Depends(get_current_user),
    response: Response = None,
):
    request_id = str(uuid.uuid4())
    if response is not None:
        response.headers["X-Request-Id"] = request_id

    # [FeatureGate] Check if payment feature is enabled
    if not is_feature_enabled("payment"):
        raise HTTPException(
            status_code=503,
            detail=get_maintenance_error_response("payment"),
            headers={"X-Request-Id": request_id},
        )

    if not apple_service.verifier:
        raise HTTPException(
            status_code=503,
            detail="App Store verification not configured",
            headers={"X-Request-Id": request_id},
        )
    log_context = {
        "requestId": request_id,
        "userId": current_user.uid,
        "env_config": apple_service.environment.name if apple_service.environment else None,
    }
    log_context.update(_jws_meta(req.signedTransaction))
    logger.info("billing.ios.confirm.request %s", log_context)

    transaction_info, verify_error = apple_service.verify_jws_detailed(req.signedTransaction)
    if not transaction_info:
        logger.warning(
            "billing.ios.confirm.verify_failed %s",
            {
                **log_context,
                "stage": (verify_error or {}).get("stage"),
                "error_type": (verify_error or {}).get("error_type"),
                "error_message": (verify_error or {}).get("error_message"),
            },
        )
        raise HTTPException(
            status_code=400,
            detail="invalid signedTransaction",
            headers={"X-Request-Id": request_id},
        )

    fields = _extract_transaction_fields(transaction_info)
    original_transaction_id = fields.get("originalTransactionId")
    app_account_token = fields.get("appAccountToken")
    product_id = fields.get("productId")

    logger.info(
        "billing.ios.confirm.verified %s",
        {
            **log_context,
            "bundleId": fields.get("bundleId"),
            "environment": fields.get("environment"),
            "productId": product_id,
            "transactionId": fields.get("transactionId"),
            "originalTransactionId": original_transaction_id,
            "expiresDate": fields.get("expiresDateMs"),
            "appAccountToken": app_account_token,
        },
    )

    if _debug_verify_enabled() and not _is_production_runtime():
        transaction_id = fields.get("transactionId")
        if transaction_id:
            server_info = apple_service.get_transaction_info(transaction_id)
            if server_info:
                diff = _diff_transaction_info(transaction_info, server_info)
                if diff:
                    logger.warning(
                        "billing.ios.confirm.transaction_info_mismatch %s",
                        {**log_context, "diff": diff},
                    )
            else:
                logger.warning(
                    "billing.ios.confirm.transaction_info_unavailable %s",
                    log_context,
                )

    status = _resolve_status(transaction_info, None, None)
    plan = _plan_for_product_id(product_id)
    entitled = _is_entitled(status, fields.get("expiresDateMs"))
    if not entitled:
        plan = "free"

    # [Explicit Check] Bundle ID and Environment
    # Note: verify_jws_detailed typically checks this if configured, but we double check here.
    if apple_service.bundle_id and fields.get("bundleId") != apple_service.bundle_id:
        logger.error(
            "billing.ios.confirm.bundle_id_mismatch expected=%s actual=%s",
            apple_service.bundle_id,
            fields.get("bundleId")
        )
        raise HTTPException(status_code=400, detail="Bundle ID mismatch")
    
    # Environment check (Sandbox vs Production)
    # Only Production transactions should update account.plan and account.appleEntitlementId
    jws_env = fields.get("environment", "Production")
    is_production = jws_env == "Production"

    if not is_production:
        logger.info(
            "billing.ios.confirm.sandbox_transaction",
            extra={
                "uid": current_user.uid,
                "environment": jws_env,
                "originalTransactionId": original_transaction_id,
                "note": "Sandbox transactions are recorded but do not affect account plan"
            }
        )

    subscription_data = {
        **fields,
        "status": status,
        "plan": plan,
        "entitled": entitled,
        "source": "app_confirm",
        "updatedAt": firestore.SERVER_TIMESTAMP,
    }

    if app_account_token:
        token_ref = db.collection("apple_app_account_tokens").document(app_account_token)
        existing_token = token_ref.get()
        if existing_token.exists:
            mapped_uid = existing_token.to_dict().get("uid")
            if mapped_uid and mapped_uid != current_user.uid:
                raise HTTPException(status_code=409, detail="appAccountToken already linked")
        token_ref.set({
            "uid": current_user.uid,
            "originalTransactionId": original_transaction_id,
            "updatedAt": firestore.SERVER_TIMESTAMP,
            "createdAt": firestore.SERVER_TIMESTAMP,
        }, merge=True)

    if original_transaction_id:
        db.collection("apple_transactions").document(original_transaction_id).set({
            **subscription_data,
            "uid": current_user.uid,
            "lastEventAt": firestore.SERVER_TIMESTAMP,
        }, merge=True)

    db.collection("users").document(current_user.uid).collection("subscriptions").document("apple").set(
        subscription_data, merge=True
    )

    # [FIX] Create entitlement ID for linking
    entitlement_id = f"apple:{original_transaction_id}" if original_transaction_id else None

    # [FIX] Only update user/account plan for Production transactions
    # Sandbox transactions are recorded in entitlements but don't affect plan
    if is_production:
        db.collection("users").document(current_user.uid).update({
            "plan": plan,
            "subscriptionPlatform": "ios",
            "planUpdatedAt": firestore.SERVER_TIMESTAMP,
            "appleEntitlementId": entitlement_id,
        })

    # [Unified Account] Sync Plan to Account (Production only)
    link_ref = db.collection("uid_links").document(current_user.uid)
    link_doc = link_ref.get()
    account_id = None
    if link_doc.exists:
        account_id = link_doc.to_dict().get("accountId")
        if account_id and is_production:
             # We should store expiresAt on the account for JIT checks
             update_data = {
                 "plan": plan,
                 "planExpiresAt": _ms_to_datetime(fields.get("expiresDateMs")),
                 "planUpdatedAt": firestore.SERVER_TIMESTAMP,
                 "lastTransactionId": fields.get("transactionId"),
                 "originalTransactionId": original_transaction_id,
                 "appleEntitlementId": entitlement_id,
             }

             db.collection("accounts").document(account_id).set(update_data, merge=True)

             # Log transition
             logger.info(
                 "subscription_state_transition",
                 extra={
                     "uid": current_user.uid,
                     "accountId": account_id,
                     "fromPlan": "unknown",
                     "toPlan": plan,
                     "reason": "purchase_confirm",
                     "transactionId": fields.get("transactionId"),
                     "originalTransactionId": original_transaction_id,
                     "expiresAt": fields.get("expiresDateMs")
                 }
             )

    # [FIX] Create entitlements document (CRITICAL - /users/me checks this!)
    if original_transaction_id and entitlement_id:
        entitlement_ref = db.collection("entitlements").document(entitlement_id)
        existing_entitlement = entitlement_ref.get()

        entitlement_data = {
            "status": status,
            "plan": plan,
            "productId": product_id,
            "currentPeriodEnd": _ms_to_datetime(fields.get("expiresDateMs")),
            "environment": fields.get("environment"),
            "provider": "apple",
            "providerEntitlementId": original_transaction_id,
            "updatedAt": firestore.SERVER_TIMESTAMP,
            "updatedBy": "app_confirm",
        }

        if not existing_entitlement.exists:
            # New entitlement - set owner
            entitlement_data["ownerAccountId"] = account_id
            entitlement_data["ownerUserId"] = current_user.uid
            entitlement_data["createdAt"] = firestore.SERVER_TIMESTAMP
            logger.info(
                "entitlement_created",
                extra={
                    "entitlementId": entitlement_id,
                    "ownerAccountId": account_id,
                    "ownerUserId": current_user.uid,
                    "plan": plan,
                }
            )
        else:
            # Existing entitlement - verify ownership
            existing_data = existing_entitlement.to_dict()
            existing_owner = existing_data.get("ownerAccountId")
            if existing_owner and existing_owner != account_id:
                logger.warning(
                    "entitlement_ownership_conflict",
                    extra={
                        "entitlementId": entitlement_id,
                        "existingOwner": existing_owner,
                        "requestingAccount": account_id,
                        "requestingUid": current_user.uid,
                    }
                )
                # [FIX] Return 409 Conflict instead of silently accepting
                # This prevents one account from claiming another's subscription
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "entitlement_owned_by_another_account",
                        "message": "This subscription is already linked to a different account",
                        "ownerAccountId": existing_owner,
                    }
                )

        entitlement_ref.set(entitlement_data, merge=True)

    return BillingConfirmResponse(
        ok=True,
        plan=plan,
        status=status,
        entitled=entitled,
        expiresAt=fields.get("expiresDateMs"),
        originalTransactionId=original_transaction_id,
        transactionId=fields.get("transactionId"),
        productId=product_id,
        requestId=request_id,
    )


@router.post("/apple/reconcile")
async def reconcile_apple_subscriptions():
    """
    Daily reconciliation task: Verify all active Apple subscriptions against Apple API.

    This endpoint should be called by Cloud Scheduler (cron job) once per day.
    It fetches all subscriptions marked as active/billing_retry/grace_period and
    verifies their current status with Apple's Get All Subscription Statuses API.

    Returns:
        Summary of reconciliation results
    """
    if not apple_service.client:
        raise HTTPException(status_code=503, detail="App Store client not configured")

    logger.info("subscription_reconciliation_started")

    # 1. Get all active subscriptions from our database
    active_statuses = ["active", "billing_retry", "grace_period"]

    try:
        entitlements_query = db.collection("entitlements")\
            .where("provider", "==", "apple")\
            .where("status", "in", active_statuses)

        entitlements = list(entitlements_query.stream())
    except Exception as e:
        # Firestore may require a composite index for this query
        # Fallback: query without status filter
        logger.warning(f"Composite index may be missing, falling back: {e}")
        entitlements_query = db.collection("entitlements").where("provider", "==", "apple")
        entitlements = [
            doc for doc in entitlements_query.stream()
            if doc.to_dict().get("status") in active_statuses
        ]

    results = {
        "checked": 0,
        "unchanged": 0,
        "updated": 0,
        "expired": 0,
        "revoked": 0,
        "errors": 0,
        "details": []
    }

    for entitlement_doc in entitlements:
        entitlement_id = entitlement_doc.id
        entitlement_data = entitlement_doc.to_dict()
        original_transaction_id = entitlement_data.get("providerEntitlementId")
        owner_account_id = entitlement_data.get("ownerAccountId")
        current_status = entitlement_data.get("status")

        if not original_transaction_id:
            continue

        results["checked"] += 1

        try:
            # 2. Call Apple API
            apple_status = apple_service.get_subscription_status_for_account(original_transaction_id)

            if not apple_status.get("found"):
                logger.warning(
                    "reconciliation_subscription_not_found",
                    extra={
                        "entitlementId": entitlement_id,
                        "originalTransactionId": original_transaction_id,
                    }
                )
                results["errors"] += 1
                continue

            new_status = apple_status.get("status", "unknown")
            is_active = apple_status.get("active", False)
            new_plan = "basic" if is_active else "free"

            # 3. Compare and update if different
            if new_status != current_status:
                logger.info(
                    "reconciliation_status_changed",
                    extra={
                        "entitlementId": entitlement_id,
                        "originalTransactionId": original_transaction_id,
                        "ownerAccountId": owner_account_id,
                        "oldStatus": current_status,
                        "newStatus": new_status,
                    }
                )

                # Update entitlement
                expires_date = apple_status.get("expires_date")
                db.collection("entitlements").document(entitlement_id).update({
                    "status": new_status,
                    "plan": new_plan,
                    "currentPeriodEnd": _ms_to_datetime(expires_date) if expires_date else None,
                    "updatedAt": firestore.SERVER_TIMESTAMP,
                    "updatedBy": "reconciliation",
                    "lastReconciliationAt": firestore.SERVER_TIMESTAMP,
                })

                # Update account plan if needed
                if owner_account_id:
                    db.collection("accounts").document(owner_account_id).update({
                        "plan": new_plan,
                        "planExpiresAt": _ms_to_datetime(expires_date) if expires_date else None,
                        "planUpdatedAt": firestore.SERVER_TIMESTAMP,
                    })

                results["updated"] += 1

                if new_status == "expired":
                    results["expired"] += 1
                elif new_status == "revoked":
                    results["revoked"] += 1

                results["details"].append({
                    "entitlementId": entitlement_id,
                    "originalTransactionId": original_transaction_id,
                    "change": f"{current_status} -> {new_status}",
                })
            else:
                # Update lastReconciliationAt even if unchanged
                db.collection("entitlements").document(entitlement_id).update({
                    "lastReconciliationAt": firestore.SERVER_TIMESTAMP,
                })
                results["unchanged"] += 1

        except Exception as e:
            logger.error(
                "reconciliation_error",
                extra={
                    "entitlementId": entitlement_id,
                    "originalTransactionId": original_transaction_id,
                    "error": str(e),
                }
            )
            results["errors"] += 1

    logger.info(
        "subscription_reconciliation_completed",
        extra={
            "checked": results["checked"],
            "updated": results["updated"],
            "unchanged": results["unchanged"],
            "errors": results["errors"],
        }
    )

    return results


@router.post("/apple/notifications")
async def handle_app_store_notifications(req: AppStoreNotificationRequest):
    if not apple_service.verifier:
        raise HTTPException(status_code=503, detail="App Store verification not configured")

    decoded_notification = apple_service.verify_notification(req.signedPayload)
    if not decoded_notification:
        raise HTTPException(status_code=400, detail="invalid signedPayload")

    notification_type = _get_field(decoded_notification, "notificationType")
    subtype = _get_field(decoded_notification, "subtype")
    notification_uuid = _get_field(decoded_notification, "notificationUUID")
    data = _get_field(decoded_notification, "data")

    lock_acquired = False
    if notification_uuid:
        try:
            await idempotency.check_and_lock(notification_uuid, "app_store_notification", ttl_seconds=86400)
            lock_acquired = True
        except ResourceAlreadyProcessed:
            return {"status": "duplicate"}

    try:
        signed_transaction_info = _get_field(data, "signedTransactionInfo")
        signed_renewal_info = _get_field(data, "signedRenewalInfo")

        transaction_info = None
        if signed_transaction_info:
            transaction_info = apple_service.verify_jws(signed_transaction_info)

        renewal_info = None
        if signed_renewal_info:
            renewal_info = apple_service.verify_renewal_info(signed_renewal_info)

        if not transaction_info:
            raise HTTPException(status_code=400, detail="missing signedTransactionInfo")

        fields = _extract_transaction_fields(transaction_info)
        original_transaction_id = fields.get("originalTransactionId")
        app_account_token = fields.get("appAccountToken")
        product_id = fields.get("productId")

        status = _resolve_status(transaction_info, renewal_info, notification_type)
        plan = _plan_for_product_id(product_id)
        if not _is_entitled(status, fields.get("expiresDateMs")):
            plan = "free"

        uid = _lookup_uid(app_account_token, original_transaction_id)

        summary_data = {
            **fields,
            "status": status,
            "plan": plan,
            "uid": uid,
            "lastNotificationType": notification_type,
            "lastNotificationSubtype": subtype,
            "lastNotificationUUID": notification_uuid,
            "lastEventAt": firestore.SERVER_TIMESTAMP,
        }

        if renewal_info:
            summary_data["renewalInfo"] = _extract_renewal_fields(renewal_info)

        if original_transaction_id:
            db.collection("apple_transactions").document(original_transaction_id).set(
                summary_data, merge=True
            )

        if uid:
            # [FIX] Only Production transactions should update account.plan
            webhook_env = fields.get("environment", "Production")
            is_production_webhook = webhook_env == "Production"

            if not is_production_webhook:
                logger.info(
                    "billing.webhook.sandbox_transaction",
                    extra={
                        "uid": uid,
                        "environment": webhook_env,
                        "originalTransactionId": original_transaction_id,
                        "notificationType": notification_type,
                        "note": "Sandbox transactions are recorded but do not affect account plan"
                    }
                )

            # [Unified Account] Sync to Account (Production only)
            link_ref = db.collection("uid_links").document(uid)
            link_doc = link_ref.get()
            account_id = None
            if link_doc.exists:
                account_id = link_doc.to_dict().get("accountId")
                if account_id and is_production_webhook:
                    db.collection("accounts").document(account_id).set({
                        "plan": plan,
                        "planExpiresAt": _ms_to_datetime(fields.get("expiresDateMs")),
                        "planUpdatedAt": firestore.SERVER_TIMESTAMP,
                        "lastTransactionId": fields.get("transactionId"),
                        "originalTransactionId": original_transaction_id,
                        "appleEntitlementId": f"apple:{original_transaction_id}" if original_transaction_id else None,
                    }, merge=True)

                    logger.info(
                         "subscription_state_transition",
                         extra={
                             "uid": uid,
                             "accountId": account_id,
                             "toPlan": plan,
                             "reason": f"notification_{notification_type}",
                             "transactionId": fields.get("transactionId"),
                             "expiresAt": fields.get("expiresDateMs")
                         }
                     )

            # Update entitlements collection (record both Production and Sandbox for auditing)
            if original_transaction_id:
                entitlement_id = f"apple:{original_transaction_id}"
                entitlement_ref = db.collection("entitlements").document(entitlement_id)
                entitlement_update = {
                    "status": status,
                    "plan": plan,
                    "productId": product_id,
                    "currentPeriodEnd": _ms_to_datetime(fields.get("expiresDateMs")),
                    "environment": webhook_env,
                    "lastNotificationType": notification_type,
                    "updatedAt": firestore.SERVER_TIMESTAMP,
                    "updatedBy": "webhook",
                }
                # Only set ownerAccountId/ownerUserId if not already set (don't overwrite)
                existing_entitlement = entitlement_ref.get()
                if not existing_entitlement.exists:
                    entitlement_update["provider"] = "apple"
                    entitlement_update["providerEntitlementId"] = original_transaction_id
                    entitlement_update["ownerAccountId"] = account_id
                    entitlement_update["ownerUserId"] = uid
                    entitlement_update["createdAt"] = firestore.SERVER_TIMESTAMP
                entitlement_ref.set(entitlement_update, merge=True)

            # Update user subscription record (always, for history)
            db.collection("users").document(uid).collection("subscriptions").document("apple").set(
                {
                    **summary_data,
                    "source": "app_store_notification",
                    "updatedAt": firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )

            # Update user plan (Production only)
            if is_production_webhook:
                db.collection("users").document(uid).update({
                    "plan": plan,
                    "subscriptionPlatform": "ios",
                    "planUpdatedAt": firestore.SERVER_TIMESTAMP,
                    "appleEntitlementId": f"apple:{original_transaction_id}" if original_transaction_id else None,
                })
        else:
            logger.warning(
                "Notification received without user mapping: originalTransactionId=%s",
                original_transaction_id,
            )

        if notification_uuid:
            db.collection("apple_notifications").document(notification_uuid).set(
                {
                    "notificationType": notification_type,
                    "subtype": subtype,
                    "environment": fields.get("environment"),
                    "originalTransactionId": original_transaction_id,
                    "transactionId": fields.get("transactionId"),
                    "uid": uid,
                    "receivedAt": firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )

        if lock_acquired:
            await idempotency.mark_completed(notification_uuid, result={"status": status})

        return {"status": "ok"}
    except HTTPException:
        if lock_acquired:
            await idempotency.mark_failed(notification_uuid, "http_exception")
        raise
    except Exception as e:
        logger.exception("Notification processing failed: %s", e)
        if lock_acquired:
            await idempotency.mark_failed(notification_uuid, str(e))
        raise HTTPException(status_code=500, detail="notification processing failed")
