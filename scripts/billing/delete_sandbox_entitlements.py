#!/usr/bin/env python3
"""
Delete expired Sandbox entitlements
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "classnote-x-dev")
os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "classnote-api-key.json"))

from app.firebase import db


def delete_sandbox_entitlements():
    sandbox_entitlements = [
        "apple:2000001103481086",   # taka - Sandbox expired
        "apple:2000001109433585",   # enden - Sandbox expired
    ]

    print("=" * 60)
    print("Deleting Sandbox Entitlements")
    print("=" * 60)

    for eid in sandbox_entitlements:
        doc_ref = db.collection("entitlements").document(eid)
        doc = doc_ref.get()

        if doc.exists:
            data = doc.to_dict()
            env = data.get("environment", "Unknown")
            status = data.get("status", "Unknown")

            print(f"\n{eid}")
            print(f"  Environment: {env}")
            print(f"  Status: {status}")

            # Safety check: only delete if Sandbox
            if env == "Sandbox":
                doc_ref.delete()
                print(f"  >>> DELETED")
            else:
                print(f"  >>> SKIPPED (not Sandbox)")
        else:
            print(f"\n{eid}: NOT FOUND")

    print("\n" + "=" * 60)
    print("Verification: Remaining entitlements for accounts")
    print("=" * 60)

    # Verify remaining entitlements
    from google.cloud.firestore_v1.base_query import FieldFilter

    for acc_id in ["RkN7aI28EAMCQCx3cRUc", "iuxeETSsFfP5dCrOEgbt"]:
        print(f"\nAccount: {acc_id}")

        # Get account info
        acc_doc = db.collection("accounts").document(acc_id).get()
        if acc_doc.exists:
            acc_data = acc_doc.to_dict()
            print(f"  Plan: {acc_data.get('plan')}")
            print(f"  LinkedEntitlement: {acc_data.get('appleEntitlementId')}")

        # Find all entitlements that might reference this account
        ents = list(db.collection("entitlements").stream())
        for e in ents:
            ed = e.to_dict()
            # Check if this entitlement belongs to this account (by checking the linked ID)
            if acc_data.get('appleEntitlementId') == e.id:
                print(f"  Entitlement: {e.id}")
                print(f"    Environment: {ed.get('environment')}")
                print(f"    Status: {ed.get('status')}")


if __name__ == "__main__":
    delete_sandbox_entitlements()
