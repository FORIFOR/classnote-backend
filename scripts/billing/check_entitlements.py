#!/usr/bin/env python3
"""
Check entitlement details for taka and enden
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "classnote-x-dev")
os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "classnote-api-key.json"))

from app.firebase import db


def check_entitlements():
    entitlement_ids = [
        "apple:140003413008751",    # taka - active
        "apple:2000001103481086",   # taka - expired
        "apple:2000001109433585",   # enden - expired
        "apple:430002840799023",    # enden - active
    ]

    print("=" * 80)
    print("Entitlement Details")
    print("=" * 80)

    for eid in entitlement_ids:
        doc = db.collection("entitlements").document(eid).get()
        if doc.exists:
            data = doc.to_dict()
            print(f"\n{eid}")
            print(f"  Environment: {data.get('environment', 'UNKNOWN')}")
            print(f"  Status: {data.get('status')}")
            print(f"  AccountId: {data.get('accountId')}")
            print(f"  ProductId: {data.get('productId')}")
            print(f"  OriginalTransactionId: {data.get('originalTransactionId')}")
            print(f"  CurrentPeriodEnd: {data.get('currentPeriodEnd')}")
            print(f"  CreatedAt: {data.get('createdAt')}")
            print(f"  UpdatedAt: {data.get('updatedAt')}")

            # Check if it's a Sandbox transaction
            orig_tx = data.get('originalTransactionId', '')
            if orig_tx.startswith('2000'):
                print(f"  >>> SANDBOX TRANSACTION (ID starts with 2000)")
            else:
                print(f"  >>> PRODUCTION TRANSACTION")
        else:
            print(f"\n{eid}: NOT FOUND")

    # Check account linked entitlements
    print("\n" + "=" * 80)
    print("Account -> Entitlement Links")
    print("=" * 80)

    accounts = ["RkN7aI28EAMCQCx3cRUc", "iuxeETSsFfP5dCrOEgbt"]
    for acc_id in accounts:
        doc = db.collection("accounts").document(acc_id).get()
        if doc.exists:
            data = doc.to_dict()
            print(f"\nAccount: {acc_id}")
            print(f"  Plan: {data.get('plan')}")
            print(f"  AppleEntitlementId: {data.get('appleEntitlementId')}")


if __name__ == "__main__":
    check_entitlements()
