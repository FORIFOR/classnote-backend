"""
Stripe Billing Routes
- POST /billing/stripe/checkout   (authenticated)
- POST /billing/stripe/portal     (authenticated)
- POST /billing/stripe/webhook    (Stripe signature verification only)
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.dependencies import get_current_user, CurrentUser
from app.firebase import db
from app.services.plans import plan_from_product_id
from app.services.stripe_billing import (
    get_or_create_stripe_customer,
    resolve_account_from_customer,
    upsert_stripe_entitlement,
    update_account_plan,
    expire_stripe_entitlement,
    map_stripe_status,
    STRIPE_WEBHOOK_SECRET,
    STRIPE_PRICE_ID_BASIC_MONTHLY,
    APP_URL,
)
from app.utils.idempotency import idempotency, ResourceAlreadyProcessed

logger = logging.getLogger("app.billing.stripe")
router = APIRouter(prefix="/billing/stripe")

ACTIVE_STATUSES = ["active", "grace", "billing_retry", "active_lifetime"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_current_period_end(sub: dict) -> int:
    """
    Extract current_period_end from a Stripe subscription object.
    Stripe SDK v8+ moved this field from subscription level to items.data[0].
    Falls back to subscription-level for backwards compatibility.
    """
    # Try items.data[0] first (Stripe SDK v8+)
    items = sub.get("items", {})
    data = items.get("data", []) if isinstance(items, dict) else []
    if data:
        val = data[0].get("current_period_end")
        if val:
            return val

    # Fallback to subscription-level (older SDK versions)
    return sub.get("current_period_end", 0)


def _find_active_entitlement(account_id: str) -> Optional[dict]:
    """
    Check if the account already has an active Production entitlement
    (Apple IAP or Stripe). Used to prevent double subscriptions.
    Same logic as _find_best_production_entitlement in users.py.
    """
    try:
        now = datetime.now(timezone.utc)
        ents = db.collection("entitlements") \
            .where("ownerAccountId", "==", account_id) \
            .stream()

        for doc in ents:
            data = doc.to_dict() or {}

            if data.get("environment") != "Production":
                continue

            status = data.get("status", "unknown")
            if status not in ACTIVE_STATUSES:
                continue

            current_period_end = data.get("currentPeriodEnd")
            if current_period_end:
                if not current_period_end.tzinfo:
                    current_period_end = current_period_end.replace(tzinfo=timezone.utc)
                if current_period_end < now and status != "active_lifetime":
                    continue

            plan = data.get("plan", "free")
            if plan != "free":
                return data

        return None
    except Exception as e:
        logger.error(f"_find_active_entitlement failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------
class CheckoutRequest(BaseModel):
    priceId: Optional[str] = None
    successUrl: Optional[str] = None
    cancelUrl: Optional[str] = None


class CheckoutResponse(BaseModel):
    url: str
    sessionId: str


class PortalRequest(BaseModel):
    returnUrl: Optional[str] = None


class PortalResponse(BaseModel):
    url: str


# ---------------------------------------------------------------------------
# POST /billing/stripe/checkout
# ---------------------------------------------------------------------------
@router.post("/checkout", response_model=CheckoutResponse)
async def create_checkout_session(
    body: CheckoutRequest = CheckoutRequest(),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Create a Stripe Checkout Session for the Standard plan.
    Returns a URL the client should redirect to.
    Rejects if user already has an active subscription (Apple IAP or Stripe).
    """
    account_id = current_user.account_id
    price_id = body.priceId or STRIPE_PRICE_ID_BASIC_MONTHLY
    success_url = body.successUrl or f"{APP_URL}/billing/success"
    cancel_url = body.cancelUrl or f"{APP_URL}/billing/cancel"

    # Guard: Prevent double subscription (Apple IAP + Stripe)
    active_ent = _find_active_entitlement(account_id)
    if active_ent:
        provider = active_ent.get("provider", "unknown")
        raise HTTPException(
            status_code=409,
            detail={
                "error": "already_subscribed",
                "provider": provider,
                "message": f"Active subscription already exists via {provider}. "
                           "Please cancel the existing subscription first.",
            },
        )

    # Ensure Stripe customer exists
    customer_id = get_or_create_stripe_customer(
        account_id=account_id,
        uid=current_user.uid,
        email=current_user.email,
    )

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=f"{success_url}?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=cancel_url,
            client_reference_id=account_id,
            metadata={"accountId": account_id, "uid": current_user.uid},
            subscription_data={
                "metadata": {"accountId": account_id, "uid": current_user.uid},
            },
            allow_promotion_codes=True,
        )
    except stripe.StripeError as e:
        logger.error(
            "stripe_checkout_create_failed",
            extra={"accountId": account_id, "error": str(e)},
        )
        raise HTTPException(status_code=502, detail="Failed to create checkout session")

    logger.info(
        "stripe_checkout_created",
        extra={
            "accountId": account_id,
            "uid": current_user.uid,
            "sessionId": session["id"],
        },
    )

    return CheckoutResponse(url=session["url"], sessionId=session["id"])


# ---------------------------------------------------------------------------
# POST /billing/stripe/portal
# ---------------------------------------------------------------------------
@router.post("/portal", response_model=PortalResponse)
async def create_portal_session(
    body: PortalRequest = PortalRequest(),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Create a Stripe Customer Portal session for managing subscription
    (cancel, change payment method, view invoices).
    """
    account_id = current_user.account_id

    # Get existing Stripe customer
    customer_id = get_or_create_stripe_customer(
        account_id=account_id,
        uid=current_user.uid,
        email=current_user.email,
    )

    return_url = body.returnUrl or f"{APP_URL}/account/billing"

    try:
        portal_session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url,
        )
    except stripe.StripeError as e:
        logger.error(
            "stripe_portal_create_failed",
            extra={"accountId": account_id, "error": str(e)},
        )
        raise HTTPException(status_code=502, detail="Failed to create portal session")

    logger.info(
        "stripe_portal_created",
        extra={"accountId": account_id, "uid": current_user.uid},
    )

    return PortalResponse(url=portal_session["url"])


# ---------------------------------------------------------------------------
# POST /billing/stripe/webhook  (NO authentication – Stripe signature only)
# ---------------------------------------------------------------------------
@router.post("/webhook")
async def stripe_webhook(request: Request):
    """
    Handle Stripe webhook events.
    Signature verification replaces authentication.
    Events handled:
      - checkout.session.completed
      - customer.subscription.updated
      - customer.subscription.deleted
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    # Verify webhook signature
    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except stripe.SignatureVerificationError as e:
        logger.warning("stripe_webhook_signature_invalid", extra={"error": str(e)})
        raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception as e:
        logger.error("stripe_webhook_construct_failed", extra={"error": str(e)})
        raise HTTPException(status_code=400, detail="Invalid payload")

    event_id = event.get("id", "")
    event_type = event.get("type", "")
    event_obj = event.get("data", {}).get("object", {})

    # Idempotency check (same pattern as Apple at billing.py line 887)
    try:
        await idempotency.check_and_lock(
            event_id, "stripe_webhook", ttl_seconds=86400
        )
    except ResourceAlreadyProcessed:
        return {"status": "duplicate"}

    logger.info(
        "stripe_webhook_received",
        extra={"eventId": event_id, "eventType": event_type},
    )

    try:
        if event_type == "checkout.session.completed":
            await _handle_checkout_completed(event_obj)
        elif event_type == "customer.subscription.updated":
            await _handle_subscription_updated(event_obj)
        elif event_type == "customer.subscription.deleted":
            await _handle_subscription_deleted(event_obj)
        else:
            logger.debug(
                "stripe_webhook_unhandled_event",
                extra={"eventType": event_type},
            )
    except Exception as e:
        logger.exception(
            "stripe_webhook_handler_error",
            extra={"eventType": event_type, "eventId": event_id},
        )
        # Release the idempotency lock so Stripe's retry can re-process this event.
        # Without this, a transient failure (e.g. Firestore blip) would permanently
        # stick: the lock prevents re-processing but the plan would never update —
        # user paid but plan stays "free".
        try:
            await idempotency.release(event_id, "stripe_webhook")
        except Exception as release_err:
            logger.warning(
                "stripe_webhook_release_lock_failed",
                extra={"eventId": event_id, "error": str(release_err)},
            )
        # Return 500 so Stripe retries.
        raise HTTPException(status_code=500, detail="webhook_processing_failed")

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Webhook event handlers
# ---------------------------------------------------------------------------
async def _handle_checkout_completed(session: dict) -> None:
    """
    Handle checkout.session.completed:
    - Link Stripe customer to account
    - Create entitlement from the new subscription
    - Update account plan
    """
    subscription_id = session.get("subscription")
    customer_id = session.get("customer")

    if not subscription_id:
        logger.warning("stripe_checkout_no_subscription", extra={"session": session.get("id")})
        return

    # Resolve account from metadata or customer reverse-lookup
    account_id = (session.get("metadata") or {}).get("accountId")
    uid = (session.get("metadata") or {}).get("uid")

    if not account_id and customer_id:
        result = resolve_account_from_customer(customer_id)
        if result:
            account_id, uid = result

    if not account_id:
        account_id = session.get("client_reference_id")

    if not account_id or not uid:
        logger.error(
            "stripe_checkout_no_account",
            extra={"sessionId": session.get("id"), "customerId": customer_id},
        )
        return

    # Retrieve full subscription details
    sub = stripe.Subscription.retrieve(subscription_id)
    price_id = sub["items"]["data"][0]["price"]["id"] if sub["items"]["data"] else ""
    plan = plan_from_product_id(price_id)

    period_end = _get_current_period_end(sub)

    upsert_stripe_entitlement(
        subscription_id=subscription_id,
        price_id=price_id,
        stripe_status=sub["status"],
        current_period_end=period_end,
        cancel_at_period_end=sub.get("cancel_at_period_end", False),
        account_id=account_id,
        uid=uid,
    )

    if plan != "free":
        update_account_plan(
            account_id=account_id,
            uid=uid,
            plan=plan,
            subscription_id=subscription_id,
            current_period_end=period_end,
            cancel_at_period_end=sub.get("cancel_at_period_end", False),
            customer_id=customer_id,
        )

    logger.info(
        "stripe_checkout_processed",
        extra={
            "accountId": account_id,
            "subscriptionId": subscription_id,
            "plan": plan,
        },
    )


async def _handle_subscription_updated(sub: dict) -> None:
    """
    Handle customer.subscription.updated:
    - Update entitlement status and period
    - Update account plan
    """
    subscription_id = sub.get("id")
    customer_id = sub.get("customer")

    # Resolve account
    account_id = (sub.get("metadata") or {}).get("accountId")
    uid = (sub.get("metadata") or {}).get("uid")

    if not account_id and customer_id:
        result = resolve_account_from_customer(customer_id)
        if result:
            account_id, uid = result

    if not account_id or not uid:
        logger.warning(
            "stripe_sub_updated_no_account",
            extra={"subscriptionId": subscription_id, "customerId": customer_id},
        )
        return

    price_id = sub["items"]["data"][0]["price"]["id"] if sub.get("items", {}).get("data") else ""
    plan = plan_from_product_id(price_id)
    status = map_stripe_status(sub.get("status", ""))

    period_end = _get_current_period_end(sub)

    upsert_stripe_entitlement(
        subscription_id=subscription_id,
        price_id=price_id,
        stripe_status=sub.get("status", ""),
        current_period_end=period_end,
        cancel_at_period_end=sub.get("cancel_at_period_end", False),
        account_id=account_id,
        uid=uid,
    )

    if status in ("active", "billing_retry") and plan != "free":
        update_account_plan(
            account_id=account_id,
            uid=uid,
            plan=plan,
            subscription_id=subscription_id,
            current_period_end=period_end,
            cancel_at_period_end=sub.get("cancel_at_period_end", False),
            customer_id=customer_id,
        )
    elif status == "expired":
        expire_stripe_entitlement(
            subscription_id=subscription_id,
            account_id=account_id,
            uid=uid,
        )

    logger.info(
        "stripe_sub_updated_processed",
        extra={
            "accountId": account_id,
            "subscriptionId": subscription_id,
            "status": status,
            "plan": plan,
        },
    )


async def _handle_subscription_deleted(sub: dict) -> None:
    """
    Handle customer.subscription.deleted:
    - Mark entitlement as expired
    - Downgrade to free if no other active entitlements
    """
    subscription_id = sub.get("id")
    customer_id = sub.get("customer")

    account_id = (sub.get("metadata") or {}).get("accountId")
    uid = (sub.get("metadata") or {}).get("uid")

    if not account_id and customer_id:
        result = resolve_account_from_customer(customer_id)
        if result:
            account_id, uid = result

    if not account_id or not uid:
        logger.warning(
            "stripe_sub_deleted_no_account",
            extra={"subscriptionId": subscription_id, "customerId": customer_id},
        )
        return

    expire_stripe_entitlement(
        subscription_id=subscription_id,
        account_id=account_id,
        uid=uid,
    )

    logger.info(
        "stripe_sub_deleted_processed",
        extra={
            "accountId": account_id,
            "subscriptionId": subscription_id,
        },
    )
