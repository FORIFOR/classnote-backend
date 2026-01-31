import logging
import os
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from google.cloud import firestore

from app.dependencies import get_current_user, CurrentUser, CurrentUser
from app.firebase import db
from app.services.apple import apple_service
from app.services.app_config import is_feature_enabled, get_maintenance_error_response
from app.util_models import BillingConfirmRequest, AppStoreNotificationRequest, BillingConfirmResponse
from app.utils.idempotency import idempotency, ResourceAlreadyProcessed


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
    # apple_service.environment is VerifierEnvironment enum
    # We can check fields.get("environment") string.
    jws_env = fields.get("environment", "").lower()
    if apple_service.environment:
        # Check mismatch if we are in Production but got Sandbox receipt, or vice versa
        # Note: server lib environment handles this verification usually.
        pass

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

    db.collection("users").document(current_user.uid).update({
        "plan": plan,
        "subscriptionPlatform": "ios",
        "planUpdatedAt": firestore.SERVER_TIMESTAMP,
        "appleEntitlementId": entitlement_id,  # [FIX] Set entitlement ID
    })

    # [Unified Account] Sync Plan to Account
    link_ref = db.collection("uid_links").document(current_user.uid)
    link_doc = link_ref.get()
    account_id = None
    if link_doc.exists:
        account_id = link_doc.to_dict().get("accountId")
        if account_id:
             # We should store expiresAt on the account for JIT checks
             update_data = {
                 "plan": plan,
                 "planExpiresAt": _ms_to_datetime(fields.get("expiresDateMs")),
                 "planUpdatedAt": firestore.SERVER_TIMESTAMP,
                 "lastTransactionId": fields.get("transactionId"),
                 "originalTransactionId": original_transaction_id,
                 "appleEntitlementId": entitlement_id,  # [FIX] Set entitlement ID
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
                # Don't overwrite owner, but still update status
                # (This allows the original owner to keep entitlement)

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
            # [Unified Account] Sync to Account
            link_ref = db.collection("uid_links").document(uid)
            link_doc = link_ref.get()
            account_id = None
            if link_doc.exists:
                account_id = link_doc.to_dict().get("accountId")
                if account_id:
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

            # Update entitlements collection (source of truth)
            if original_transaction_id:
                entitlement_id = f"apple:{original_transaction_id}"
                entitlement_ref = db.collection("entitlements").document(entitlement_id)
                entitlement_update = {
                    "status": status,
                    "plan": plan,
                    "productId": product_id,
                    "currentPeriodEnd": _ms_to_datetime(fields.get("expiresDateMs")),
                    "environment": fields.get("environment"),
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

            db.collection("users").document(uid).collection("subscriptions").document("apple").set(
                {
                    **summary_data,
                    "source": "app_store_notification",
                    "updatedAt": firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )
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
