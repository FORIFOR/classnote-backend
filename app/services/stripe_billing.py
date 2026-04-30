"""
Stripe Billing Service
Handles Stripe <-> Firestore I/O for subscription management.

Entitlement pattern mirrors Apple exactly:
  entitlements/stripe:{subscriptionId} -> same fields as apple:{originalTransactionId}

This ensures /users/me plan determination works without modification.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional, Tuple

import stripe
from google.cloud import firestore

from app.firebase import db
from app.services.plans import plan_from_product_id

logger = logging.getLogger("app.stripe_billing")

# ---------------------------------------------------------------------------
# Stripe SDK configuration
# ---------------------------------------------------------------------------
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID_BASIC_MONTHLY = os.environ.get(
    "STRIPE_PRICE_ID_BASIC_MONTHLY", "price_1T4VGoJ6POTmwGaCtgbxbFe1"
)
APP_URL = os.environ.get("APP_URL", "https://deepnote-billing-ui.vercel.app")


# ---------------------------------------------------------------------------
# Stripe Status -> Entitlement Status mapping
# Must produce values accepted by _find_best_production_entitlement filter:
#   ["active", "grace", "billing_retry", "active_lifetime"]
# ---------------------------------------------------------------------------
_STRIPE_STATUS_MAP = {
    "active": "active",
    "trialing": "active",
    "past_due": "billing_retry",
    "canceled": "expired",
    "unpaid": "expired",
    "incomplete": "expired",
    "incomplete_expired": "expired",
    "paused": "expired",
}


def map_stripe_status(stripe_status: str) -> str:
    """Map a Stripe subscription status to the entitlement status used internally."""
    return _STRIPE_STATUS_MAP.get(stripe_status, "expired")


# ---------------------------------------------------------------------------
# Stripe Customer <-> Account mapping
# ---------------------------------------------------------------------------
def get_or_create_stripe_customer(
    account_id: str,
    uid: str,
    email: Optional[str] = None,
) -> str:
    """
    Get existing Stripe customer ID for the account, or create a new one.
    Also stores a reverse-lookup doc in stripe_customers/{customerId}.

    Returns:
        Stripe customer ID (cus_xxx)
    """
    # Check account doc for existing customer
    account_ref = db.collection("accounts").document(account_id)
    account_doc = account_ref.get()
    if account_doc.exists:
        data = account_doc.to_dict() or {}
        existing_cid = (data.get("stripe") or {}).get("customerId")
        if existing_cid:
            return existing_cid

    # Create new Stripe customer
    create_params = {
        "metadata": {"accountId": account_id, "uid": uid},
    }
    if email:
        create_params["email"] = email

    customer = stripe.Customer.create(**create_params)
    customer_id = customer["id"]

    # Persist on account doc (merge – safe for existing fields)
    account_ref.set(
        {"stripe": {"customerId": customer_id}},
        merge=True,
    )

    # Reverse-lookup collection for webhook resolution
    db.collection("stripe_customers").document(customer_id).set(
        {
            "accountId": account_id,
            "uid": uid,
            "createdAt": firestore.SERVER_TIMESTAMP,
        }
    )

    logger.info(
        "stripe_customer_created",
        extra={
            "accountId": account_id,
            "uid": uid,
            "customerId": customer_id,
        },
    )

    return customer_id


def resolve_account_from_customer(
    customer_id: str,
) -> Optional[Tuple[str, str]]:
    """
    Reverse-lookup: Stripe customer ID -> (account_id, uid).
    Returns None if not found.
    """
    doc = db.collection("stripe_customers").document(customer_id).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    account_id = data.get("accountId")
    uid = data.get("uid")
    if not account_id or not uid:
        return None
    return account_id, uid


# ---------------------------------------------------------------------------
# Entitlement CRUD (mirrors Apple pattern in billing.py lines 362-400)
# ---------------------------------------------------------------------------
def upsert_stripe_entitlement(
    subscription_id: str,
    price_id: str,
    stripe_status: str,
    current_period_end: int,
    cancel_at_period_end: bool,
    account_id: str,
    uid: str,
) -> str:
    """
    Create or update an entitlement document for a Stripe subscription.
    Follows the exact same schema as Apple entitlements so that
    _find_best_production_entitlement() picks it up automatically.

    Args:
        subscription_id: Stripe subscription ID (sub_xxx)
        price_id: Stripe price ID
        stripe_status: Stripe subscription.status value
        current_period_end: Unix timestamp (seconds)
        cancel_at_period_end: Whether user has scheduled cancellation
        account_id: Internal account ID
        uid: Firebase UID

    Returns:
        Entitlement document ID (stripe:{subscriptionId})
    """
    entitlement_id = f"stripe:{subscription_id}"
    entitlement_ref = db.collection("entitlements").document(entitlement_id)
    existing = entitlement_ref.get()

    plan = plan_from_product_id(price_id)
    status = map_stripe_status(stripe_status)

    entitlement_data = {
        "status": status,
        "plan": plan,
        "productId": price_id,
        "currentPeriodEnd": datetime.fromtimestamp(
            current_period_end, tz=timezone.utc
        ),
        "provider": "stripe",
        "providerEntitlementId": subscription_id,
        "environment": "Production",
        "autoRenewStatus": not cancel_at_period_end,
        "updatedAt": firestore.SERVER_TIMESTAMP,
        "updatedBy": "stripe_webhook",
    }

    if not existing.exists:
        entitlement_data["ownerAccountId"] = account_id
        entitlement_data["ownerUserId"] = uid
        entitlement_data["createdAt"] = firestore.SERVER_TIMESTAMP
    else:
        # Verify ownership (same pattern as Apple at billing.py line 383)
        existing_owner = (existing.to_dict() or {}).get("ownerAccountId")
        if existing_owner and existing_owner != account_id:
            logger.warning(
                "stripe_entitlement_ownership_conflict",
                extra={
                    "entitlementId": entitlement_id,
                    "existingOwner": existing_owner,
                    "requestingAccount": account_id,
                },
            )
            return entitlement_id

    entitlement_ref.set(entitlement_data, merge=True)

    logger.info(
        "stripe_entitlement_upserted",
        extra={
            "entitlementId": entitlement_id,
            "accountId": account_id,
            "status": status,
            "plan": plan,
        },
    )

    return entitlement_id


def update_account_plan(
    account_id: str,
    uid: str,
    plan: str,
    subscription_id: str,
    current_period_end: int,
    cancel_at_period_end: bool,
    customer_id: str,
) -> None:
    """
    Update account and user documents with the current Stripe plan.
    Mirrors the Apple pattern at billing.py lines 417-428.
    """
    expires_dt = datetime.fromtimestamp(current_period_end, tz=timezone.utc)

    # Update account (merge – preserves Apple fields)
    db.collection("accounts").document(account_id).set(
        {
            "plan": plan,
            "planExpiresAt": expires_dt,
            "planUpdatedAt": firestore.SERVER_TIMESTAMP,
            "subscriptionPlatform": "stripe",
            "subscriptionAutoRenews": not cancel_at_period_end,
            "stripe": {
                "customerId": customer_id,
                "subscriptionId": subscription_id,
            },
            "stripeEntitlementId": f"stripe:{subscription_id}",
        },
        merge=True,
    )

    # Update user doc (mirrors billing.py line 425-428)
    try:
        db.collection("users").document(uid).update(
            {
                "plan": plan,
                "subscriptionPlatform": "stripe",
                "planUpdatedAt": firestore.SERVER_TIMESTAMP,
            }
        )
    except Exception as e:
        # User doc may not exist yet; log but don't fail
        logger.warning(
            "stripe_user_plan_update_failed",
            extra={"uid": uid, "error": str(e)},
        )

    logger.info(
        "stripe_account_plan_updated",
        extra={
            "accountId": account_id,
            "uid": uid,
            "plan": plan,
            "subscriptionId": subscription_id,
        },
    )


def expire_stripe_entitlement(
    subscription_id: str,
    account_id: str,
    uid: str,
) -> None:
    """
    Mark a Stripe entitlement as expired and downgrade the account to free.
    Only downgrades if no other active entitlements exist (Apple or Stripe).
    """
    entitlement_id = f"stripe:{subscription_id}"

    # Expire the entitlement
    db.collection("entitlements").document(entitlement_id).set(
        {
            "status": "expired",
            "autoRenewStatus": False,
            "updatedAt": firestore.SERVER_TIMESTAMP,
            "updatedBy": "stripe_webhook",
        },
        merge=True,
    )

    # Check if there are other active entitlements before downgrading
    # (User might have an Apple subscription too)
    now = datetime.now(timezone.utc)
    ents = (
        db.collection("entitlements")
        .where("ownerAccountId", "==", account_id)
        .stream()
    )

    has_other_active = False
    for doc in ents:
        if doc.id == entitlement_id:
            continue
        data = doc.to_dict() or {}
        if data.get("environment") != "Production":
            continue
        status = data.get("status", "")
        if status not in ["active", "grace", "billing_retry", "active_lifetime"]:
            continue
        cpe = data.get("currentPeriodEnd")
        if cpe:
            if not cpe.tzinfo:
                cpe = cpe.replace(tzinfo=timezone.utc)
            if cpe < now and status != "active_lifetime":
                continue
        has_other_active = True
        break

    if has_other_active:
        logger.info(
            "stripe_entitlement_expired_but_other_active",
            extra={
                "entitlementId": entitlement_id,
                "accountId": account_id,
            },
        )
        return

    # No other active entitlements -> downgrade to free
    db.collection("accounts").document(account_id).set(
        {
            "plan": "free",
            "planUpdatedAt": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )

    try:
        db.collection("users").document(uid).update(
            {
                "plan": "free",
                "planUpdatedAt": firestore.SERVER_TIMESTAMP,
            }
        )
    except Exception:
        pass

    logger.info(
        "stripe_entitlement_expired_downgraded",
        extra={
            "entitlementId": entitlement_id,
            "accountId": account_id,
        },
    )
