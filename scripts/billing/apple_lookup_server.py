#!/usr/bin/env python3
"""
Look up Apple subscription using the App Store Server API.
This script calls the deployed Cloud Run service to use its Apple credentials.

Usage:
    python scripts/apple_lookup_server.py --uid <uid>
"""

import argparse
import os
import sys
import json
import subprocess

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_id_token():
    """Get an ID token for the current user to authenticate with Cloud Run."""
    result = subprocess.run(
        ["gcloud", "auth", "print-identity-token"],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        print(f"Failed to get ID token: {result.stderr}")
        return None
    return result.stdout.strip()


def call_admin_api(endpoint: str, method: str = "GET", data: dict = None):
    """Call the Cloud Run admin API."""
    base_url = "https://deepnote-api-900324644592.asia-northeast1.run.app"
    url = f"{base_url}{endpoint}"

    token = get_id_token()
    if not token:
        return None

    cmd = ["curl", "-s", "-X", method, url, "-H", f"Authorization: Bearer {token}"]
    if data:
        cmd.extend(["-H", "Content-Type: application/json", "-d", json.dumps(data)])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"API call failed: {result.stderr}")
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"Invalid JSON response: {result.stdout}")
        return None


def lookup_subscription_via_api(uid: str):
    """Look up subscription data via the admin API."""
    # This would need an admin endpoint to be implemented
    # For now, we'll use the local Firestore lookup
    print("Note: Direct Apple API lookup requires an admin endpoint.")
    print("Using local Firestore lookup instead...")

    from google.cloud import firestore
    db = firestore.Client(project="classnote-x-dev")

    # Get user
    user_doc = db.collection("users").document(uid).get()
    if not user_doc.exists:
        print(f"User {uid} not found")
        return

    user_data = user_doc.to_dict()
    app_account_token = user_data.get("appleAppAccountToken")
    account_id = user_data.get("accountId")

    print(f"\nUser: {uid}")
    print(f"Account ID: {account_id}")
    print(f"App Account Token: {app_account_token}")

    # Check if there's a way to query Apple directly
    # The App Store Server API has get_transaction_history which needs an originalTransactionId
    # It also has look_up_order_id which needs an orderId
    # And get_all_subscription_statuses which needs a transaction ID

    print("\n--- Options for manual fix ---")
    print("1. Ask user to retry the claim from the app (recommended)")
    print("2. Ask user for their originalTransactionId from their App Store purchase")
    print("3. Use App Store Connect to look up the transaction")

    if app_account_token:
        print(f"\nApp Account Token can be used to verify ownership once we have a transaction.")


def main():
    parser = argparse.ArgumentParser(description="Look up Apple subscription via Server API")
    parser.add_argument("--uid", required=True, help="User UID to look up")
    args = parser.parse_args()

    lookup_subscription_via_api(args.uid)


if __name__ == "__main__":
    main()
