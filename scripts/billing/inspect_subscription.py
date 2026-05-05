#!/usr/bin/env python3
"""
Inspect subscription state for a user.
Usage: python tools/inspect_subscription.py [uid]
If no uid provided, lists recent users to choose from.
"""
import firebase_admin
from firebase_admin import auth, firestore
import sys
from datetime import datetime, timezone

def init():
    try:
        firebase_admin.get_app()
    except ValueError:
        firebase_admin.initialize_app()
    return firestore.client()

def list_recent_users():
    """List recent users for selection."""
    print("\n=== Recent Users ===")
    page = auth.list_users(max_results=10)
    users = []
    for user in page.users:
        users.append((user.user_metadata.last_sign_in_timestamp or 0, user))
    users.sort(key=lambda x: x[0], reverse=True)

    for i, (_, u) in enumerate(users):
        provider = u.provider_data[0].provider_id if u.provider_data else "unknown"
        last_in = "Never"
        if u.user_metadata.last_sign_in_timestamp:
            dt = datetime.fromtimestamp(u.user_metadata.last_sign_in_timestamp / 1000)
            last_in = dt.strftime("%Y-%m-%d %H:%M")
        print(f"{i+1}. {u.uid[:20]}... | {u.phone_number or 'N/A':<15} | {provider:<10} | {last_in}")
        users[i] = (users[i][0], u)

    return [u for _, u in users]

def inspect_user(db, uid: str):
    """Inspect subscription state for a user."""
    print(f"\n{'='*60}")
    print(f"Inspecting UID: {uid}")
    print('='*60)

    # 1. Check users/{uid} document
    print("\n[1] users/{uid} document:")
    user_doc = db.collection("users").document(uid).get()
    if not user_doc.exists:
        print("   ❌ NOT FOUND")
        return
    user_data = user_doc.to_dict()
    print(f"   appleEntitlementId: {user_data.get('appleEntitlementId', 'NOT SET')}")
    print(f"   appleAppAccountToken: {user_data.get('appleAppAccountToken', 'NOT SET')}")
    print(f"   planUpdatedAt: {user_data.get('planUpdatedAt', 'NOT SET')}")

    # 2. Check uid_links/{uid}
    print("\n[2] uid_links/{uid} document:")
    link_doc = db.collection("uid_links").document(uid).get()
    if not link_doc.exists:
        print("   ❌ NOT FOUND - User has no account link!")
        print("   ⚠️  This is likely the root cause - claim requires account link")
        return
    link_data = link_doc.to_dict()
    account_id = link_data.get("accountId")
    print(f"   accountId: {account_id}")
    print(f"   linkedAt: {link_data.get('linkedAt', 'NOT SET')}")

    if not account_id:
        print("   ❌ accountId is missing in link!")
        return

    # 3. Check accounts/{accountId}
    print(f"\n[3] accounts/{account_id} document:")
    acc_doc = db.collection("accounts").document(account_id).get()
    if not acc_doc.exists:
        print("   ❌ NOT FOUND - Account document missing!")
        return
    acc_data = acc_doc.to_dict()
    print(f"   plan: {acc_data.get('plan', 'NOT SET')}")
    print(f"   appleEntitlementId: {acc_data.get('appleEntitlementId', 'NOT SET')}")
    print(f"   phoneE164: {acc_data.get('phoneE164', 'NOT SET')}")
    print(f"   planUpdatedAt: {acc_data.get('planUpdatedAt', 'NOT SET')}")

    apple_ent_id = acc_data.get("appleEntitlementId")

    # 4. Check entitlements/{appleEntitlementId}
    if not apple_ent_id:
        print("\n[4] entitlements check:")
        print("   ⚠️  No appleEntitlementId in account - subscription not claimed yet")

        # Check if there are any entitlements for this account
        print("\n   Searching for any entitlements with this accountId...")
        ents = db.collection("entitlements").where("ownerAccountId", "==", account_id).stream()
        found = False
        for ent in ents:
            found = True
            print(f"   Found: {ent.id}")
            ent_data = ent.to_dict()
            print(f"      status: {ent_data.get('status')}")
            print(f"      plan: {ent_data.get('plan')}")
        if not found:
            print("   No entitlements found for this accountId")
        return

    print(f"\n[4] entitlements/{apple_ent_id} document:")
    ent_doc = db.collection("entitlements").document(apple_ent_id).get()
    if not ent_doc.exists:
        print("   ❌ NOT FOUND - Entitlement document missing!")
        return
    ent_data = ent_doc.to_dict()
    print(f"   ownerAccountId: {ent_data.get('ownerAccountId', 'NOT SET')}")
    print(f"   ownerUserId: {ent_data.get('ownerUserId', 'NOT SET')}")
    print(f"   status: {ent_data.get('status', 'NOT SET')}")
    print(f"   plan: {ent_data.get('plan', 'NOT SET')}")
    print(f"   productId: {ent_data.get('productId', 'NOT SET')}")
    print(f"   environment: {ent_data.get('environment', 'NOT SET')}")
    current_period_end = ent_data.get('currentPeriodEnd')
    if current_period_end:
        if hasattr(current_period_end, 'tzinfo') and current_period_end.tzinfo is None:
            current_period_end = current_period_end.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        expired = current_period_end < now
        print(f"   currentPeriodEnd: {current_period_end} ({'EXPIRED' if expired else 'VALID'})")
    else:
        print(f"   currentPeriodEnd: NOT SET")
    print(f"   appAccountToken: {ent_data.get('appAccountToken', 'NOT SET')}")

    # 5. Verification summary
    print("\n" + "="*60)
    print("VERIFICATION SUMMARY:")
    print("="*60)

    issues = []

    # Check ownership
    if ent_data.get('ownerAccountId') != account_id:
        issues.append(f"❌ Ownership mismatch: entitlement.ownerAccountId={ent_data.get('ownerAccountId')} != account={account_id}")
    else:
        print("✅ Ownership match: entitlement.ownerAccountId == accountId")

    # Check status
    valid_statuses = ["active", "grace", "billing_retry", "active_lifetime"]
    status = ent_data.get('status', 'unknown')
    if status not in valid_statuses:
        issues.append(f"❌ Invalid status: {status} (expected one of {valid_statuses})")
    else:
        print(f"✅ Status is valid: {status}")

    # Check expiry
    if current_period_end:
        now = datetime.now(timezone.utc)
        if current_period_end < now and status != "active_lifetime":
            issues.append(f"❌ Subscription expired: {current_period_end}")
        else:
            print(f"✅ Subscription not expired")

    if issues:
        print("\nISSUES FOUND:")
        for issue in issues:
            print(f"  {issue}")
        print("\n⚠️  /users/me will return plan='free' due to above issues")
    else:
        print(f"\n✅ All checks passed - /users/me should return plan='{ent_data.get('plan', 'free')}'")

def main():
    db = init()

    if len(sys.argv) > 1:
        uid = sys.argv[1]
    else:
        users = list_recent_users()
        print("\nEnter number to inspect (or paste UID directly):")
        choice = input("> ").strip()

        if choice.isdigit() and 1 <= int(choice) <= len(users):
            uid = users[int(choice) - 1].uid
        else:
            uid = choice

    inspect_user(db, uid)

if __name__ == "__main__":
    main()
