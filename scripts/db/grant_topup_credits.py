"""Grant topup AI credits to an account/user.

Usage:
    python scripts/db/grant_topup_credits.py <id> <amount>

The <id> may be an accountId or a uid. We first try accounts/<id>; if
that does not exist we look up users/<id>.accountId.

Writes are transactional. Prints before/after values.
"""

import os
import sys

import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore as fb_firestore
from google.cloud import firestore


def _init():
    key_path = "classnote-api-key.json"
    if os.path.exists(key_path):
        cred = credentials.Certificate(key_path)
    else:
        cred = credentials.ApplicationDefault()
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return fb_firestore.client()


def _resolve_account_id(db, raw_id: str) -> str:
    acc_ref = db.collection("accounts").document(raw_id)
    if acc_ref.get().exists:
        return raw_id
    user_ref = db.collection("users").document(raw_id)
    snap = user_ref.get()
    if not snap.exists:
        raise SystemExit(f"Neither accounts/{raw_id} nor users/{raw_id} exists")
    data = snap.to_dict() or {}
    aid = data.get("accountId")
    if not aid:
        raise SystemExit(f"users/{raw_id} has no accountId field")
    print(f"Resolved uid={raw_id} -> accountId={aid}")
    return aid


def main():
    if len(sys.argv) != 3:
        raise SystemExit("Usage: grant_topup_credits.py <id> <amount>")
    raw_id = sys.argv[1]
    amount = int(sys.argv[2])
    if amount <= 0:
        raise SystemExit("amount must be positive")

    db = _init()
    account_id = _resolve_account_id(db, raw_id)
    acc_ref = db.collection("accounts").document(account_id)

    @firestore.transactional
    def _txn(transaction):
        snap = acc_ref.get(transaction=transaction)
        data = snap.to_dict() or {}
        before = int(data.get("topupCredits", 0))
        after = before + amount
        transaction.update(acc_ref, {"topupCredits": firestore.Increment(amount)})
        return before, after

    before, after = _txn(db.transaction())
    print(f"accounts/{account_id}.topupCredits: {before} -> {after} (+{amount})")


if __name__ == "__main__":
    main()
