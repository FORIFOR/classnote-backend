#!/usr/bin/env python3
"""
Look up Apple subscription status for a user.

Usage:
    python scripts/lookup_apple_subscription.py --uid <uid>
    python scripts/lookup_apple_subscription.py --token <app_account_token>
    python scripts/lookup_apple_subscription.py --transaction-id <original_transaction_id>
"""

import argparse
import os
import sys
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.cloud import firestore


def get_db():
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT", "classnote-x-dev")
    return firestore.Client(project=project_id)


def lookup_by_uid(uid: str):
    """Look up subscription data for a user by UID."""
    db = get_db()

    print(f"\n=== Looking up user: {uid} ===\n")

    # Get user doc
    user_doc = db.collection("users").document(uid).get()
    if not user_doc.exists:
        print(f"User {uid} not found")
        return

    user_data = user_doc.to_dict()
    account_id = user_data.get("accountId")
    app_account_token = user_data.get("appleAppAccountToken")

    print(f"Account ID: {account_id}")
    print(f"Apple App Account Token: {app_account_token}")

    if account_id:
        # Get account
        acc_doc = db.collection("accounts").document(account_id).get()
        if acc_doc.exists:
            acc_data = acc_doc.to_dict()
            print(f"\nAccount Plan: {acc_data.get('plan', 'free')}")
            print(f"Plan Expires At: {acc_data.get('planExpiresAt', '-')}")

        # Get entitlements
        print("\n--- Entitlements ---")
        ents = db.collection("accounts").document(account_id).collection("entitlements").stream()
        found = False
        for ent in ents:
            found = True
            data = ent.to_dict()
            print(f"  [{ent.id}]")
            print(f"    productId: {data.get('productId')}")
            print(f"    status: {data.get('status')}")
            print(f"    expiresAt: {data.get('expiresAt')}")
            print(f"    originalTransactionId: {data.get('originalTransactionId')}")
        if not found:
            print("  (none)")

    # Check apple_transactions
    print("\n--- Apple Transactions ---")
    if app_account_token:
        # Look up by appAccountToken
        txns = db.collection("apple_transactions").where("appAccountToken", "==", app_account_token).limit(10).stream()
        found = False
        for txn in txns:
            found = True
            data = txn.to_dict()
            print(f"  [{txn.id}]")
            print(f"    productId: {data.get('productId')}")
            print(f"    status: {data.get('status')}")
            print(f"    expiresAt: {data.get('expiresAt')}")
            print(f"    environment: {data.get('environment')}")
        if not found:
            print("  (none found by appAccountToken)")
    else:
        print("  (no appAccountToken set)")


def create_entitlement(uid: str, product_id: str, original_transaction_id: str, expires_at: datetime):
    """Manually create an entitlement for a user."""
    db = get_db()

    # Get user's account
    user_doc = db.collection("users").document(uid).get()
    if not user_doc.exists:
        print(f"User {uid} not found")
        return

    user_data = user_doc.to_dict()
    account_id = user_data.get("accountId")

    if not account_id:
        print("User has no account ID")
        return

    now = datetime.now(timezone.utc)

    # Determine plan from product_id
    plan = "basic" if "standard" in product_id.lower() or "basic" in product_id.lower() else "free"

    # Create entitlement
    ent_ref = db.collection("accounts").document(account_id).collection("entitlements").document("apple")
    ent_ref.set({
        "source": "apple",
        "productId": product_id,
        "originalTransactionId": original_transaction_id,
        "status": "active",
        "plan": plan,
        "expiresAt": expires_at,
        "createdAt": now,
        "updatedAt": now,
        "manuallyCreated": True,
        "manuallyCreatedAt": now,
        "manuallyCreatedReason": "admin_fix_subscription",
    })

    # Update account plan
    acc_ref = db.collection("accounts").document(account_id)
    acc_ref.update({
        "plan": plan,
        "planExpiresAt": expires_at,
        "planUpdatedAt": now,
        "updatedAt": now,
    })

    print(f"\n=== Entitlement Created ===")
    print(f"Account ID: {account_id}")
    print(f"Plan: {plan}")
    print(f"Product ID: {product_id}")
    print(f"Expires At: {expires_at}")
    print(f"Original Transaction ID: {original_transaction_id}")


def main():
    parser = argparse.ArgumentParser(description="Look up Apple subscription status")
    parser.add_argument("--uid", help="User UID to look up")
    parser.add_argument("--token", help="App Account Token to look up")
    parser.add_argument("--transaction-id", help="Original Transaction ID to look up")

    # For manual fix
    parser.add_argument("--fix", action="store_true", help="Create entitlement manually")
    parser.add_argument("--product-id", help="Product ID for manual fix")
    parser.add_argument("--expires", help="Expiration date (ISO format) for manual fix")

    args = parser.parse_args()

    if args.uid:
        lookup_by_uid(args.uid)

        if args.fix:
            if not args.product_id or not args.expires or not args.transaction_id:
                print("\nFor --fix, you must provide --product-id, --expires, and --transaction-id")
                return

            expires_at = datetime.fromisoformat(args.expires.replace("Z", "+00:00"))
            create_entitlement(args.uid, args.product_id, args.transaction_id, expires_at)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
