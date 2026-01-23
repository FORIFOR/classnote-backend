"""
Phone verification and account unification endpoints.

Flow:
1. POST /phone/verification:start - Send SMS OTP
2. POST /phone/verification:confirm - Verify OTP and auto-merge accounts if phone exists

When a user verifies their phone number:
- If the phone is NOT in phone_index: Register it with current account
- If the phone IS in phone_index: Merge current account into existing account
- Always return firebaseCustomToken for canonical (unified) account
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone, timedelta
from google.cloud import firestore
import logging
import secrets
import hashlib
import os

from firebase_admin import auth as fb_auth

from app.dependencies import get_current_user, CurrentUser
from app.firebase import db

router = APIRouter()
logger = logging.getLogger("app.phone")


# ============================================================
# Models
# ============================================================

class PhoneStartRequest(BaseModel):
    phoneE164: str  # e.g., "+819012345678"


class PhoneStartResponse(BaseModel):
    challengeId: str
    ttlSec: int = 300
    message: str = "OTP sent"


class PhoneConfirmRequest(BaseModel):
    challengeId: str
    code: str


class PhoneConfirmResponse(BaseModel):
    ok: bool
    verified: bool
    merged: bool = False
    fromAccountId: Optional[str] = None
    toAccountId: Optional[str] = None
    canonicalAccountId: str
    firebaseCustomToken: str  # Always return for re-auth
    message: str


# ============================================================
# Helpers
# ============================================================

def _generate_otp() -> str:
    """Generate a 6-digit OTP."""
    return f"{secrets.randbelow(1000000):06d}"


def _hash_otp(otp: str) -> str:
    """Hash OTP for storage (never store plain OTP)."""
    return hashlib.sha256(otp.encode()).hexdigest()


async def _send_sms(phone_e164: str, otp: str) -> bool:
    """
    Send SMS via Twilio or other provider.
    Returns True if successful.
    """
    # Check if we're in dev mode (skip actual SMS)
    if os.environ.get("SKIP_SMS_VERIFICATION") == "true":
        logger.info(f"[DEV MODE] OTP for {phone_e164}: {otp}")
        return True

    twilio_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    twilio_token = os.environ.get("TWILIO_AUTH_TOKEN")
    twilio_from = os.environ.get("TWILIO_PHONE_NUMBER")

    if not all([twilio_sid, twilio_token, twilio_from]):
        logger.warning("Twilio credentials not configured, skipping SMS")
        # In production, you might want to fail here
        logger.info(f"[NO TWILIO] OTP for {phone_e164}: {otp}")
        return True

    try:
        from twilio.rest import Client
        client = Client(twilio_sid, twilio_token)
        message = client.messages.create(
            body=f"Your ClassNote verification code is: {otp}",
            from_=twilio_from,
            to=phone_e164
        )
        logger.info(f"SMS sent to {phone_e164}, SID: {message.sid}")
        return True
    except Exception as e:
        logger.error(f"Failed to send SMS to {phone_e164}: {e}")
        return False


def _get_account_id_for_uid(uid: str) -> Optional[str]:
    """Get accountId from uid_links or users collection."""
    link_doc = db.collection("uid_links").document(uid).get()
    if link_doc.exists:
        return link_doc.to_dict().get("accountId")

    user_doc = db.collection("users").document(uid).get()
    if user_doc.exists:
        return user_doc.to_dict().get("accountId")

    return None


def _create_firebase_custom_token(account_id: str, extra_claims: dict = None) -> str:
    """Create Firebase custom token with uid = accountId."""
    claims = extra_claims or {}
    claims["accountId"] = account_id
    token_bytes = fb_auth.create_custom_token(account_id, claims)
    return token_bytes.decode("utf-8")


# ============================================================
# Endpoints
# ============================================================

@router.post("/phone/verification:start", response_model=PhoneStartResponse)
async def start_phone_verification(
    req: PhoneStartRequest,
    current_user: CurrentUser = Depends(get_current_user)
):
    """
    Start phone verification by sending SMS OTP.

    Rate limits:
    - 3 attempts per phone per hour
    - 5 attempts per user per hour
    """
    phone = req.phoneE164.strip()

    # Basic validation
    if not phone.startswith("+") or len(phone) < 10:
        raise HTTPException(400, "Invalid phone number format. Use E.164 format (e.g., +819012345678)")

    uid = current_user.uid
    now = datetime.now(timezone.utc)

    # Rate limiting (simple version - check recent challenges)
    one_hour_ago = now - timedelta(hours=1)
    recent_by_phone = list(
        db.collection("phone_challenges")
        .where("phoneE164", "==", phone)
        .where("createdAt", ">=", one_hour_ago)
        .limit(4)
        .stream()
    )
    if len(recent_by_phone) >= 3:
        raise HTTPException(429, "Too many verification attempts for this phone number")

    recent_by_user = list(
        db.collection("phone_challenges")
        .where("uid", "==", uid)
        .where("createdAt", ">=", one_hour_ago)
        .limit(6)
        .stream()
    )
    if len(recent_by_user) >= 5:
        raise HTTPException(429, "Too many verification attempts. Please wait.")

    # Generate OTP and challenge
    otp = _generate_otp()
    otp_hash = _hash_otp(otp)
    challenge_id = secrets.token_urlsafe(32)
    expires_at = now + timedelta(seconds=300)

    # Store challenge
    challenge_ref = db.collection("phone_challenges").document(challenge_id)
    challenge_ref.set({
        "challengeId": challenge_id,
        "uid": uid,
        "phoneE164": phone,
        "otpHash": otp_hash,
        "attempts": 0,
        "maxAttempts": 5,
        "status": "pending",
        "createdAt": now,
        "expiresAt": expires_at
    })

    # Send SMS
    sent = await _send_sms(phone, otp)
    if not sent:
        challenge_ref.update({"status": "send_failed"})
        raise HTTPException(500, "Failed to send SMS")

    logger.info(f"[phone:start] Challenge created for {uid}, phone={phone[:6]}***")

    return PhoneStartResponse(
        challengeId=challenge_id,
        ttlSec=300,
        message="Verification code sent"
    )


@router.post("/phone/verification:confirm", response_model=PhoneConfirmResponse)
async def confirm_phone_verification(
    req: PhoneConfirmRequest,
    current_user: CurrentUser = Depends(get_current_user)
):
    """
    Confirm phone verification with OTP.

    If phone is already registered to another account:
    - Merge current user into that account
    - Return firebaseCustomToken for the merged (canonical) account

    If phone is new:
    - Register phone with current account
    - Return firebaseCustomToken for current account
    """
    challenge_id = req.challengeId
    code = req.code.strip()
    uid = current_user.uid
    now = datetime.now(timezone.utc)

    # Fetch challenge
    challenge_ref = db.collection("phone_challenges").document(challenge_id)
    challenge_doc = challenge_ref.get()

    if not challenge_doc.exists:
        raise HTTPException(404, "Challenge not found or expired")

    challenge = challenge_doc.to_dict()

    # Validate challenge
    if challenge.get("uid") != uid:
        raise HTTPException(403, "Challenge belongs to another user")

    if challenge.get("status") != "pending":
        raise HTTPException(400, f"Challenge is {challenge.get('status')}")

    if challenge.get("expiresAt") < now:
        challenge_ref.update({"status": "expired"})
        raise HTTPException(400, "Challenge expired")

    attempts = challenge.get("attempts", 0)
    max_attempts = challenge.get("maxAttempts", 5)
    if attempts >= max_attempts:
        challenge_ref.update({"status": "max_attempts"})
        raise HTTPException(400, "Too many attempts")

    # Verify OTP
    expected_hash = challenge.get("otpHash")
    provided_hash = _hash_otp(code)

    if provided_hash != expected_hash:
        challenge_ref.update({"attempts": attempts + 1})
        remaining = max_attempts - attempts - 1
        raise HTTPException(401, f"Invalid code. {remaining} attempts remaining.")

    # OTP verified!
    phone = challenge.get("phoneE164")
    challenge_ref.update({"status": "verified", "verifiedAt": now})

    logger.info(f"[phone:confirm] OTP verified for {uid}, phone={phone[:6]}***")

    # ============================================================
    # Core Logic: Check phone_index and merge if needed
    # ============================================================

    # Get current user's accountId
    current_account_id = _get_account_id_for_uid(uid)

    # Run merge logic in transaction
    @firestore.transactional
    def confirm_and_merge(transaction):
        phone_ref = db.collection("phone_numbers").document(phone)
        phone_doc = phone_ref.get(transaction=transaction)

        merged = False
        from_account_id = None
        to_account_id = None
        target_account_id = current_account_id

        if not phone_doc.exists:
            # ============================================================
            # Case 1: Phone is NEW - Register with current account
            # ============================================================
            logger.info(f"[phone:confirm] Phone {phone[:6]}*** is new, registering with account {current_account_id}")

            # If no account exists, create one
            if not target_account_id:
                new_acc_ref = db.collection("accounts").document()
                target_account_id = new_acc_ref.id
                transaction.set(new_acc_ref, {
                    "phoneE164": phone,
                    "phoneVerified": True,
                    "primaryUid": uid,
                    "memberUids": [uid],
                    "plan": "free",
                    "createdAt": now,
                    "updatedAt": now
                })
                # Link uid to new account
                transaction.set(db.collection("uid_links").document(uid), {
                    "uid": uid,
                    "accountId": target_account_id,
                    "linkedAt": now
                })
            else:
                # Update existing account with phone
                acc_ref = db.collection("accounts").document(target_account_id)
                transaction.update(acc_ref, {
                    "phoneE164": phone,
                    "phoneVerified": True,
                    "updatedAt": now
                })

            # Register phone in index
            transaction.set(phone_ref, {
                "accountId": target_account_id,
                "verified": True,
                "standardOwnerUid": uid,
                "createdAt": now,
                "updatedAt": now
            })

            # Update user doc
            transaction.set(db.collection("users").document(uid), {
                "phoneE164": phone,
                "phoneVerified": True,
                "accountId": target_account_id,
                "updatedAt": now
            }, merge=True)

        else:
            # ============================================================
            # Case 2: Phone EXISTS - Check if merge needed
            # ============================================================
            phone_data = phone_doc.to_dict()
            existing_account_id = phone_data.get("accountId")

            if existing_account_id == current_account_id:
                # Already linked to same account - just update verification
                logger.info(f"[phone:confirm] Phone already linked to current account {current_account_id}")
                transaction.update(phone_ref, {"verified": True, "updatedAt": now})
                transaction.set(db.collection("users").document(uid), {
                    "phoneE164": phone,
                    "phoneVerified": True,
                    "updatedAt": now
                }, merge=True)
                target_account_id = current_account_id

            else:
                # ============================================================
                # Case 3: Phone belongs to DIFFERENT account - MERGE!
                # ============================================================
                logger.info(f"[phone:confirm] MERGE: {current_account_id} -> {existing_account_id}")

                merged = True
                from_account_id = current_account_id
                to_account_id = existing_account_id
                target_account_id = existing_account_id

                # 1. Update uid_links: point current uid to target account
                transaction.set(db.collection("uid_links").document(uid), {
                    "uid": uid,
                    "accountId": target_account_id,
                    "linkedAt": now,
                    "mergedFrom": from_account_id,
                    "mergeReason": "phone_verification"
                }, merge=True)

                # 2. Add uid to target account's memberUids
                target_acc_ref = db.collection("accounts").document(target_account_id)
                target_acc_doc = target_acc_ref.get(transaction=transaction)
                if target_acc_doc.exists:
                    target_data = target_acc_doc.to_dict()
                    member_uids = set(target_data.get("memberUids", []))
                    member_uids.add(uid)
                    transaction.update(target_acc_ref, {
                        "memberUids": list(member_uids),
                        "phoneVerified": True,
                        "updatedAt": now
                    })

                # 3. Mark old account as merged (if it existed)
                if from_account_id:
                    from_acc_ref = db.collection("accounts").document(from_account_id)
                    from_acc_doc = from_acc_ref.get(transaction=transaction)
                    if from_acc_doc.exists:
                        from_data = from_acc_doc.to_dict()
                        from_members = [m for m in from_data.get("memberUids", []) if m != uid]
                        if len(from_members) == 0:
                            transaction.update(from_acc_ref, {
                                "mergedInto": target_account_id,
                                "mergedAt": now,
                                "memberUids": [],
                                "updatedAt": now
                            })
                        else:
                            transaction.update(from_acc_ref, {
                                "memberUids": from_members,
                                "updatedAt": now
                            })

                # 4. Update user doc
                transaction.set(db.collection("users").document(uid), {
                    "phoneE164": phone,
                    "phoneVerified": True,
                    "accountId": target_account_id,
                    "mergedAt": now,
                    "previousAccountId": from_account_id,
                    "updatedAt": now
                }, merge=True)

                # 5. Update phone index
                transaction.update(phone_ref, {
                    "verified": True,
                    "updatedAt": now
                })

        return {
            "merged": merged,
            "fromAccountId": from_account_id,
            "toAccountId": to_account_id,
            "targetAccountId": target_account_id
        }

    # Execute transaction
    try:
        transaction = db.transaction()
        result = confirm_and_merge(transaction)
    except Exception as e:
        logger.error(f"[phone:confirm] Transaction failed: {e}")
        raise HTTPException(500, f"Failed to process verification: {str(e)}")

    # ============================================================
    # Create Firebase Custom Token for canonical account
    # ============================================================
    target_account_id = result["targetAccountId"]

    try:
        custom_token = _create_firebase_custom_token(
            target_account_id,
            {
                "provider": "phone_verified",
                "phoneE164": phone,
                "mergedFrom": result.get("fromAccountId")
            }
        )
    except Exception as e:
        logger.error(f"[phone:confirm] Failed to create custom token: {e}")
        raise HTTPException(500, "Failed to create authentication token")

    # Enqueue session migration if merged
    if result["merged"] and result["fromAccountId"]:
        try:
            from app.task_queue import enqueue_account_migration_task
            enqueue_account_migration_task(
                from_account_id=result["fromAccountId"],
                to_account_id=result["targetAccountId"]
            )
        except Exception as e:
            logger.warning(f"[phone:confirm] Failed to enqueue migration: {e}")
            # Non-fatal - merge is complete, migration can be retried

    logger.info(f"[phone:confirm] Complete: merged={result['merged']}, canonicalAccountId={target_account_id}")

    return PhoneConfirmResponse(
        ok=True,
        verified=True,
        merged=result["merged"],
        fromAccountId=result.get("fromAccountId"),
        toAccountId=result.get("toAccountId"),
        canonicalAccountId=target_account_id,
        firebaseCustomToken=custom_token,
        message="Account unified via phone verification" if result["merged"] else "Phone verified"
    )


# ============================================================
# Link Phone (for users who already have phone but need to link)
# ============================================================

class LinkPhoneRequest(BaseModel):
    phoneE164: str


class LinkPhoneResponse(BaseModel):
    ok: bool
    accountId: str
    message: str


@router.post("/phone/link", response_model=LinkPhoneResponse)
async def link_phone(
    req: LinkPhoneRequest,
    current_user: CurrentUser = Depends(get_current_user)
):
    """
    Link a phone number directly (for users who authenticated via Firebase Phone Auth).
    This bypasses SMS OTP since Firebase already verified the phone.

    Only use this when current_user.phone_number matches the request.
    """
    phone = req.phoneE164.strip()
    uid = current_user.uid
    token_phone = current_user.phone_number

    # Security: Only allow if Firebase token already has this phone
    if not token_phone:
        raise HTTPException(400, "Your session does not have a verified phone number")

    if token_phone != phone:
        raise HTTPException(403, "Phone number does not match your session")

    now = datetime.now(timezone.utc)

    # Same logic as confirm but without OTP verification
    current_account_id = _get_account_id_for_uid(uid)

    @firestore.transactional
    def link_and_merge(transaction):
        phone_ref = db.collection("phone_numbers").document(phone)
        phone_doc = phone_ref.get(transaction=transaction)

        target_account_id = current_account_id

        if not phone_doc.exists:
            # New phone - register
            if not target_account_id:
                new_acc_ref = db.collection("accounts").document()
                target_account_id = new_acc_ref.id
                transaction.set(new_acc_ref, {
                    "phoneE164": phone,
                    "phoneVerified": True,
                    "primaryUid": uid,
                    "memberUids": [uid],
                    "plan": "free",
                    "createdAt": now,
                    "updatedAt": now
                })
                transaction.set(db.collection("uid_links").document(uid), {
                    "uid": uid,
                    "accountId": target_account_id,
                    "linkedAt": now
                })

            transaction.set(phone_ref, {
                "accountId": target_account_id,
                "verified": True,
                "standardOwnerUid": uid,
                "createdAt": now,
                "updatedAt": now
            })
        else:
            phone_data = phone_doc.to_dict()
            target_account_id = phone_data.get("accountId")

            # Update uid_links to point to phone's account
            transaction.set(db.collection("uid_links").document(uid), {
                "uid": uid,
                "accountId": target_account_id,
                "linkedAt": now,
                "mergedFrom": current_account_id if current_account_id != target_account_id else None,
                "mergeReason": "phone_link"
            }, merge=True)

        # Update user
        transaction.set(db.collection("users").document(uid), {
            "phoneE164": phone,
            "phoneVerified": True,
            "accountId": target_account_id,
            "updatedAt": now
        }, merge=True)

        return target_account_id

    try:
        transaction = db.transaction()
        final_account_id = link_and_merge(transaction)
    except Exception as e:
        logger.error(f"[phone/link] Failed: {e}")
        raise HTTPException(500, f"Failed to link phone: {str(e)}")

    return LinkPhoneResponse(
        ok=True,
        accountId=final_account_id,
        message="Phone linked successfully"
    )
