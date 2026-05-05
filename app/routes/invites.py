"""
Invite Code Routes - Referral system for bonus quota
"""
import os
import secrets
import string
from datetime import datetime, timezone
from typing import Optional, Dict
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from google.cloud import firestore

from app.firebase import db
from app.dependencies import get_current_user, CurrentUser
from app.services.app_config import get_app_config

router = APIRouter(prefix="/invites", tags=["Invites"])

# Characters for invite code (exclude confusing: O/0/I/1/L)
_CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 6

BONUS_SUMMARY = 3
BONUS_QUIZ = 3

# Base URL for invite share links (configurable via env)
INVITE_BASE_URL = os.environ.get("INVITE_BASE_URL", "https://app.deepnote.jp")


class RedeemRequest(BaseModel):
    code: str


class RedeemResponse(BaseModel):
    bonusSummary: int
    bonusQuiz: int
    message: str


class InviteCodeResponse(BaseModel):
    inviteCode: str
    shareUrl: str
    bonus: Dict[str, int]


def _generate_code() -> str:
    """Generate a unique invite code."""
    return "".join(secrets.choice(_CODE_CHARS) for _ in range(_CODE_LENGTH))


def _build_response(code: str) -> InviteCodeResponse:
    """Build invite response with share URL and bonus info."""
    return InviteCodeResponse(
        inviteCode=code,
        shareUrl=f"{INVITE_BASE_URL}/invite?c={code}",
        bonus={
            "summary": BONUS_SUMMARY,
            "quiz": BONUS_QUIZ,
        },
    )


@router.get("/me", response_model=InviteCodeResponse)
async def get_my_invite_code(current_user: CurrentUser = Depends(get_current_user)):
    """
    Get the current user's invite code, share URL, and bonus info.
    Auto-generates a code if one doesn't exist yet.
    """
    uid = current_user.uid
    user_ref = db.collection("users").document(uid)
    user_snap = user_ref.get(["inviteCode"])
    user_data = user_snap.to_dict() if user_snap.exists else {}

    existing_code = user_data.get("inviteCode")
    if existing_code:
        return _build_response(existing_code)

    # Generate a unique code (retry on collision)
    for _ in range(10):
        code = _generate_code()
        code_ref = db.collection("inviteCodes").document(code)
        if not code_ref.get().exists:
            # Store in both locations
            code_ref.set({
                "userId": uid,
                "createdAt": firestore.SERVER_TIMESTAMP,
            })
            user_ref.set({"inviteCode": code}, merge=True)
            return _build_response(code)

    raise HTTPException(status_code=500, detail="Failed to generate unique invite code")


@router.post("/redeem", response_model=RedeemResponse)
async def redeem_invite_code(
    body: RedeemRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Redeem an invite code for bonus summary/quiz quota.
    - Each user can redeem exactly once.
    - Cannot redeem own code.
    """
    uid = current_user.uid
    code = body.code.strip().upper()

    if not code or len(code) != _CODE_LENGTH:
        raise HTTPException(status_code=400, detail="Invalid invite code format")

    # 1. Check if user already redeemed
    user_ref = db.collection("users").document(uid)
    user_snap = user_ref.get(["invite", "accountId"])
    user_data = user_snap.to_dict() if user_snap.exists else {}

    existing_invite = user_data.get("invite") or {}
    if existing_invite.get("redeemedCode"):
        raise HTTPException(status_code=409, detail="Already redeemed an invite code")

    account_id = user_data.get("accountId")

    # 2. Look up invite code
    code_ref = db.collection("inviteCodes").document(code)
    code_snap = code_ref.get()
    if not code_snap.exists:
        raise HTTPException(status_code=404, detail="Invite code not found")

    code_data = code_snap.to_dict()
    inviter_uid = code_data.get("userId")

    # 3. Self-invite check
    if inviter_uid == uid:
        raise HTTPException(status_code=400, detail="Cannot redeem your own invite code")

    # 4. Get configurable bonus amounts (fallback to hardcoded defaults)
    config = get_app_config()
    invite_bonus = getattr(config, 'inviteBonus', None) or {}
    bonus_summary = invite_bonus.get("summary", BONUS_SUMMARY)
    bonus_quiz = invite_bonus.get("quiz", BONUS_QUIZ)

    # 4b. Look up inviter's accountId for bonus target
    inviter_ref = db.collection("users").document(inviter_uid)
    inviter_snap = inviter_ref.get(["accountId"])
    inviter_data = inviter_snap.to_dict() if inviter_snap.exists else {}
    inviter_account_id = inviter_data.get("accountId")

    # 5. Apply bonus (transaction for atomicity)
    @firestore.transactional
    def txn_redeem(transaction):
        # Re-check redeem status inside transaction
        u_snap = user_ref.get(transaction=transaction)
        u_data = u_snap.to_dict() if u_snap.exists else {}
        if (u_data.get("invite") or {}).get("redeemedCode"):
            return False

        # Mark redeemed on user
        transaction.set(user_ref, {
            "invite": {
                "redeemedCode": code,
                "redeemedAt": datetime.now(timezone.utc),
                "inviterUid": inviter_uid,
            }
        }, merge=True)

        # Add bonus to redeemer — always write to users/{uid}
        bonus_fields = {
            "inviteBonusSummary": firestore.Increment(bonus_summary),
            "inviteBonusQuiz": firestore.Increment(bonus_quiz),
        }
        transaction.set(user_ref, bonus_fields, merge=True)
        # Also write to accounts/{accountId} if available (CostGuard reads from here)
        if account_id:
            transaction.set(
                db.collection("accounts").document(account_id),
                bonus_fields, merge=True,
            )

        # Add bonus to inviter — always write to users/{inviterUid}
        transaction.set(inviter_ref, bonus_fields, merge=True)
        # Also write to inviter's accounts doc if available
        if inviter_account_id:
            transaction.set(
                db.collection("accounts").document(inviter_account_id),
                bonus_fields, merge=True,
            )

        return True

    transaction = db.transaction()
    success = txn_redeem(transaction)

    if not success:
        raise HTTPException(status_code=409, detail="Already redeemed an invite code")

    return RedeemResponse(
        bonusSummary=bonus_summary,
        bonusQuiz=bonus_quiz,
        message="招待コードを適用しました！",
    )
