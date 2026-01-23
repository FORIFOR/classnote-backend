from fastapi import APIRouter, Depends, Query, HTTPException, BackgroundTasks
from typing import List, Optional
import random
import string
import re
import logging
from datetime import datetime, timezone, timedelta
from google.cloud import firestore
from pydantic import BaseModel
from app.dependencies import get_current_user, CurrentUser
from app.util_models import (
    PublicUser,
    MeResponse,
    MeUpdateRequest,
    UserProfileResponse,
    UserProfileUpdateRequest,
    ShareCodeResponse,
    ShareLookupRequest, # Still needed for ShareCode flow if we keep it
    ClaimUsernameRequest,
    ShareLookupResponse,
    ShareCodeLookupResponse,
    CapabilitiesResponse,
    SubscriptionVerifyRequest,
    EntitlementResponse,
)
from app.firebase import db
from app.services.account_deletion import (
    LOCKS_COLLECTION,
    REQUESTS_COLLECTION,
    deletion_lock_id,
    deletion_schedule_at,
)
from app.services.cost_guard import cost_guard, FREE_LIMITS, BASIC_LIMITS, PREMIUM_LIMITS
import uuid

from app.services.plans import plan_from_product_id
from app.services.apple_entitlements import parse_ms_to_dt, is_active_from_expires_ms
from app.services.effective_plan import compute_effective_plan_for_user
from app.services.apple import apple_service

# [FIX] Use consistent Dependency
# from app.deps import get_current_user as get_current_user_v2, CurrentUser

logger = logging.getLogger("app.users")

# [NEW] Plan Mapping
PRODUCT_TO_PLAN = {
    "com.classnote.app.standard.monthly": "basic",
    "com.classnote.app.standard.yearly": "basic", # Assumption
    "com.classnote.app.premium.monthly": "premium",
    "com.classnote.app.premium.yearly": "premium", # Assumption
}

# [NEW] Custom Error for Transaction
class EntitlementConflictError(Exception):
    def __init__(self, message: str):
        self.message = message

USERNAME_RE = re.compile(r"^[a-z0-9_]{3,20}$")


# [NEW] Safe JIT Downgrade Logic
def downgrade_account_if_expired(account_id: str, uid: str):
    """
    Transactionally checks if account is expired and downgrades if necessary.
    Idempotent: logs and updates only if transition actually happens.
    """
    acc_ref = db.collection("accounts").document(account_id)
    
    @firestore.transactional
    def txn(transaction):
        snapshot = acc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return False, None
            
        data = snapshot.to_dict()
        current_plan = data.get("plan", "free")
        
        if current_plan == "free":
            return False, None # Already free
            
        expires_at = data.get("planExpiresAt")
        if not expires_at:
            return False, None # No expiration set
            
        # Timezone safety
        if not expires_at.tzinfo:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
            
        now = datetime.now(timezone.utc)
        
        if expires_at < now:
            transaction.update(acc_ref, {
                "plan": "free",
                "planUpdatedAt": firestore.SERVER_TIMESTAMP
            })
            return True, {
                "fromPlan": current_plan,
                "expiresAt": expires_at,
                "now": now
            }
        
        return False, None

    try:
        transaction = db.transaction()
        did_downgrade, info = txn(transaction)
        
        if did_downgrade and info:
            logger.info(
                "subscription_state_transition",
                extra={
                    "uid": uid,
                    "accountId": account_id,
                    "fromPlan": info["fromPlan"],
                    "toPlan": "free",
                    "reason": "expired_jit",
                    "expiresAt": info["expiresAt"].isoformat(),
                    "now": info["now"].isoformat()
                }
            )
    except Exception as e:
        logger.error(f"JIT downgrade failed for account {account_id}: {e}")


router = APIRouter(prefix="/users")

@router.get("/search", response_model=List[PublicUser])
async def search_users(
    q: str = Query(..., min_length=1, description="Search query (email or display name)"),
    current_user: CurrentUser = Depends(get_current_user)
):
    col = db.collection("users")
    q_lower = q.lower()
    results = []
    
    # 1. Email 完全一致（小文字化して保存している emailLower を優先）
    email_docs = list(col.where("emailLower", "==", q_lower).limit(5).stream())
    if not email_docs:
        email_docs = list(col.where("email", "==", q).limit(5).stream())
        
    seen_uids = set()
    
    for doc in email_docs:
        d = doc.to_dict()
        uid = doc.id
        if uid in seen_uids: continue
        if d.get("isShareable", d.get("allowSearch", True)) is False:
            continue
        
        results.append(PublicUser(
            uid=uid,
            displayName=d.get("displayName"),
            email=d.get("email"),
            photoUrl=d.get("photoUrl"),
            providers=d.get("providers"),
            allowSearch=d.get("allowSearch", True),
        ))
        seen_uids.add(uid)
        
    # 2. DisplayName Prefix Match
    name_docs = list(col.where("displayName", ">=", q)
                        .where("displayName", "<=", q + "\uf8ff")
                        .limit(5).stream())
                        
    for doc in name_docs:
        uid = doc.id
        if uid in seen_uids: continue
        
        d = doc.to_dict()
        if d.get("isShareable", d.get("allowSearch", True)) is False:
            continue
        results.append(PublicUser(
            uid=uid,
            displayName=d.get("displayName"),
            email=d.get("email"),
            photoUrl=d.get("photoUrl"),
            providers=d.get("providers"),
            allowSearch=d.get("allowSearch", True),
        ))
        seen_uids.add(uid)
        
    return results[:10]


class AppleTokenReq(BaseModel):
    appAccountToken: str

class AppleTokenResponse(BaseModel):
    ok: bool
    merged: bool = False
    accountId: Optional[str] = None
    previousAccountId: Optional[str] = None
    message: Optional[str] = None


def _merge_uid_into_account(transaction, uid: str, target_account_id: str, source_account_id: Optional[str] = None) -> dict:
    """
    [Account Unification] Merge a uid into target_account_id.
    Updates uid_links, accounts.memberUids, and optionally marks old account as merged.
    """
    now = datetime.now(timezone.utc)

    # 1. Update uid_links to point to target account
    link_ref = db.collection("uid_links").document(uid)
    transaction.set(link_ref, {
        "uid": uid,
        "accountId": target_account_id,
        "linkedAt": now,
        "mergedFrom": source_account_id,
        "mergeReason": "app_account_token_match"
    }, merge=True)

    # 2. Add uid to target account's memberUids
    target_acc_ref = db.collection("accounts").document(target_account_id)
    target_acc_snap = target_acc_ref.get(transaction=transaction)
    if target_acc_snap.exists:
        target_data = target_acc_snap.to_dict() or {}
        member_uids = set(target_data.get("memberUids", []))
        member_uids.add(uid)
        transaction.update(target_acc_ref, {
            "memberUids": list(member_uids),
            "updatedAt": now
        })
    else:
        # Create target account if it doesn't exist
        transaction.set(target_acc_ref, {
            "memberUids": [uid],
            "primaryUid": uid,
            "plan": "free",
            "createdAt": now,
            "updatedAt": now
        })

    # 3. Remove uid from source account's memberUids (if different)
    if source_account_id and source_account_id != target_account_id:
        source_acc_ref = db.collection("accounts").document(source_account_id)
        source_acc_snap = source_acc_ref.get(transaction=transaction)
        if source_acc_snap.exists:
            source_data = source_acc_snap.to_dict() or {}
            source_members = [m for m in source_data.get("memberUids", []) if m != uid]
            if len(source_members) == 0:
                # Mark as merged if no members left
                transaction.update(source_acc_ref, {
                    "memberUids": [],
                    "mergedInto": target_account_id,
                    "mergedAt": now,
                    "updatedAt": now
                })
            else:
                transaction.update(source_acc_ref, {
                    "memberUids": source_members,
                    "updatedAt": now
                })

    # 4. Update users/{uid}.accountId
    user_ref = db.collection("users").document(uid)
    transaction.set(user_ref, {
        "accountId": target_account_id,
        "updatedAt": now
    }, merge=True)

    return {
        "changed": True,
        "from": source_account_id,
        "to": target_account_id
    }


@router.post("/me/apple_app_account_token", response_model=AppleTokenResponse)
async def set_apple_token(req: AppleTokenReq, current_user: CurrentUser = Depends(get_current_user)):
    """
    Registers the appAccountToken (UUID) generated by the iOS client.

    [Account Unification] If this token is already linked to a different accountId,
    this endpoint will automatically merge the current uid into that account.
    This enables cross-provider account unification (Google + LINE on same device).
    """
    if not re.match(r"^[0-9a-fA-F-]{32,36}$", req.appAccountToken):
        raise HTTPException(status_code=400, detail="INVALID_APP_ACCOUNT_TOKEN")

    token = req.appAccountToken
    uid = current_user.uid
    now = datetime.now(timezone.utc)

    # Get current user's accountId
    link_ref = db.collection("uid_links").document(uid)
    link_snap = link_ref.get()
    current_account_id = None
    if link_snap.exists:
        current_account_id = link_snap.to_dict().get("accountId")

    # If no accountId yet, check users/{uid}
    if not current_account_id:
        user_snap = db.collection("users").document(uid).get()
        if user_snap.exists:
            current_account_id = user_snap.to_dict().get("accountId")

    # Check if token is already registered
    token_ref = db.collection("apple_app_account_tokens").document(token)
    token_snap = token_ref.get()

    if not token_snap.exists:
        # Token not registered - register it with current accountId
        if not current_account_id:
            # No account yet - create one
            new_acc_ref = db.collection("accounts").document()
            current_account_id = new_acc_ref.id
            new_acc_ref.set({
                "primaryUid": uid,
                "memberUids": [uid],
                "plan": "free",
                "createdAt": now,
                "updatedAt": now
            })
            link_ref.set({
                "uid": uid,
                "accountId": current_account_id,
                "linkedAt": now
            })
            db.collection("users").document(uid).set({
                "accountId": current_account_id,
                "updatedAt": now
            }, merge=True)

        # Register token
        token_ref.set({
            "accountId": current_account_id,
            "uid": uid,  # Keep for backward compat with billing.py
            "createdAt": now,
            "lastSeenAt": now
        })

        # Save token to user doc
        db.collection("users").document(uid).set({
            "appleAppAccountToken": token,
            "updatedAt": now
        }, merge=True)

        logger.info(f"[AppAccountToken] Registered token for uid={uid}, accountId={current_account_id}")
        return AppleTokenResponse(
            ok=True,
            merged=False,
            accountId=current_account_id,
            message="Token registered successfully"
        )

    # Token exists - check if we need to merge
    token_data = token_snap.to_dict() or {}
    mapped_account_id = token_data.get("accountId")

    # Update lastSeenAt
    token_ref.update({"lastSeenAt": now})

    # Save token to user doc
    db.collection("users").document(uid).set({
        "appleAppAccountToken": token,
        "updatedAt": now
    }, merge=True)

    if not mapped_account_id:
        # Token exists but no accountId - update it
        if current_account_id:
            token_ref.update({"accountId": current_account_id})
        logger.info(f"[AppAccountToken] Updated token with accountId={current_account_id}")
        return AppleTokenResponse(
            ok=True,
            merged=False,
            accountId=current_account_id,
            message="Token updated with accountId"
        )

    if mapped_account_id == current_account_id:
        # Already unified - nothing to do
        logger.info(f"[AppAccountToken] Already unified: uid={uid}, accountId={current_account_id}")
        return AppleTokenResponse(
            ok=True,
            merged=False,
            accountId=current_account_id,
            message="Already unified"
        )

    # Different accountId - need to merge!
    # Merge current uid into the mapped_account_id (the "original" account)
    logger.info(f"[AppAccountToken] Merging uid={uid} from {current_account_id} to {mapped_account_id}")

    @firestore.transactional
    def merge_transaction(transaction):
        return _merge_uid_into_account(transaction, uid, mapped_account_id, current_account_id)

    try:
        merge_result = merge_transaction(db.transaction())
        logger.info(f"[AppAccountToken] Merge complete: {merge_result}")
        return AppleTokenResponse(
            ok=True,
            merged=True,
            accountId=mapped_account_id,
            previousAccountId=current_account_id,
            message="Account unified successfully"
        )
    except Exception as e:
        logger.error(f"[AppAccountToken] Merge failed: {e}")
        raise HTTPException(status_code=500, detail=f"Account merge failed: {str(e)}")


@router.get("/me", response_model=MeResponse)
async def get_me(
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_user)
):
    # [NEW] Resolve User Profile early (needed for display values in all paths)
    user_doc_ref = db.collection("users").document(current_user.uid)
    user_doc_snap = user_doc_ref.get()
    user_profile = user_doc_snap.to_dict() if user_doc_snap.exists else {}

    final_username = user_profile.get("username")
    final_has_username = user_profile.get("hasUsername", False)
    final_display_name = user_profile.get("displayName") or current_user.display_name or "New User"

    # Step A: Extract Token Data safely
    now = datetime.now(timezone.utc)
    token_provider = current_user.provider # e.g. "google.com", "apple.com", "phone", "custom" (LINE)
    token_phone = current_user.phone_number

    # [NEW] Account Architecture Check
    # 1. Resolve AccountID from Link (Primary Truth)
    link_ref = db.collection("uid_links").document(current_user.uid)
    link_doc = link_ref.get()
    link_data = link_doc.to_dict() if link_doc.exists else {}
    account_id = link_data.get("accountId")

    # 2. Fallback to Users (Legacy / Self-Repair)
    if not account_id:
        legacy_account_id = user_profile.get("accountId")
        if legacy_account_id:
            logger.info(f"Self-repairing link for {current_user.uid} -> {legacy_account_id}")
            account_id = legacy_account_id
            
            # Create Link
            try:
                link_ref.set({
                    "uid": current_user.uid,
                    "accountId": account_id,
                    "linkedAt": now,
                    "reason": "repair_from_users",
                    "repairedAt": now
                }, merge=True)
                # We will set 'action' later if not overridden
            except Exception as e:
                logger.warning(f"Failed link repair: {e}")


    # Step B: Account Resolution & JIT Logic
    # -----------------------------------------------------
    # Resolve the "Canonical Account" based on Token Claims.
    # Logic: Phone > Provider > Email (UI hint)
    
    resolved_account_id = None
    resolution_action = "none"
    resolution_reason = None
    
    # 1. Resolve by Phone (Strongest)
    if not resolved_account_id and token_phone:
        # Check phone index
        p_idx_ref = db.collection("phone_numbers").document(token_phone)
        p_idx = p_idx_ref.get()
        if p_idx.exists:
            resolved_account_id = p_idx.to_dict().get("accountId")
            if resolved_account_id:
                resolution_reason = "phone_match"

            if resolved_account_id:
                resolution_reason = "phone_match"

    # 2. Resolve by Provider (Optional Enhancement)
    # if not resolved_account_id and ...:

    # -----------------------------------------------------
    # JIT / Auto-Attach Logic (Reject-less)
    # -----------------------------------------------------
    
    # If we found a canonical account (e.g. from Phone), use it.
    if resolved_account_id:
        if account_id != resolved_account_id:
            # Current user is pointing to wrong/missing account, BUT phone matches existing.
            # ACTION: Auto-Attach to that account.
            logger.info(f"Auto-Attaching user {current_user.uid} to existing account {resolved_account_id} (Reason: {resolution_reason})")
            
            try:
                batch = db.batch()
                # 1. Update Link
                batch.set(link_ref, {
                    "uid": current_user.uid,
                    "accountId": resolved_account_id,
                    "linkedAt": now,
                    "previousAccountId": account_id,
                    "attachReason": resolution_reason,
                    "updatedAt": now
                }, merge=True)
                
                # 2. Update User Profile
                batch.update(db.collection("users").document(current_user.uid), {
                    "accountId": resolved_account_id, 
                    "updatedAt": now
                })
                
                # 3. Ensure Account knows this UID provided a phone match (optional but good for graph)
                # If we want to strictly add to `linkedUids` map if you strictly maintain it
                
                batch.commit()
                
                # Update local context for response
                account_id = resolved_account_id
                resolution_action = "attached"
            except Exception as e:
                logger.error(f"Failed to auto-attach: {e}")
                # We don't fail, we just fall back (or partial fail), essentially 'none' action
    
    # If NO account found yet, proceed to creation (JIT) logic below...
                
    # If STILL no account_id, perform JIT Creation
    if not account_id:
        try:
             # Use the previous JIT logic...
             if token_phone:
                 # [CASE A-1] Auto-link Phone User (Legacy check or if phoneIndex was missing)
                 # ... existing logic ...
                 # Actually, if we are here, it means phoneIndex didn't exist or didn't have accountId.
                 # So we create NEW.
                 logger.info(f"JIT creating Phone account for {current_user.uid}")
                 new_ref = db.collection("accounts").document() # Create NEW
                 # We calculate ID from phone for consistency?
                 # No, if phone index missing, maybe it's new. 
                 # But wait, account_id_from_phone is deterministic. 
                 # If we use that, we might find an existing doc?
                 # Let's use the deterministic generator if phone present to be safe.
                 from app.services.account import account_id_from_phone
                 det_id = account_id_from_phone(token_phone)
                 
                 # Check if it exists really?
                 det_ref = db.collection("accounts").document(det_id)
                 det_snap = det_ref.get()
                 
                 acct_id = det_id
                 if not det_snap.exists:
                     det_ref.set({
                         "phoneE164": token_phone,
                         "createdAt": now,
                         "plan": "free",
                         "providers": [token_provider] if token_provider else [],
                         "credits": {
                             "cloudSecondsRemaining": 1800, # 30 min
                             "summaryRemaining": 3,
                             "quizRemaining": 3,
                         }
                     })
                 
                 # Ensure index
                 db.collection("phone_numbers").document(token_phone).set({
                     "accountId": acct_id,
                     "standardOwnerUid": current_user.uid,
                     "updatedAt": now
                 }, merge=True)
                 
                 # Link
                 db.collection("uid_links").document(current_user.uid).set({
                     "uid": current_user.uid, "accountId": acct_id, "linkedAt": now
                 })
                 
                 account_id = acct_id
                 resolution_action = "created"
                 
             else:
                 # [CASE A-2] JIT SNS-only Account
                 logger.info(f"JIT creating SNS-only account for {current_user.uid}")
                 new_ref = db.collection("accounts").document()
                 account_id = new_ref.id
                 batch = db.batch()
                 batch.set(new_ref, {
                     "primaryUid": current_user.uid,
                     "createdAt": now,
                     "plan": "free",
                     "providers": [token_provider] if token_provider else [],
                     "credits": {
                         "summaryRemaining": FREE_LIMITS["summary_generated"],
                         "quizRemaining": FREE_LIMITS["quiz_generated"],
                         "cloudSecondsRemaining": FREE_LIMITS["cloud_stt_sec"]  # 1800 (30 min)
                     }
                 })
                 batch.set(link_ref, {"uid": current_user.uid, "accountId": account_id, "linkedAt": now})
                 batch.commit()
                 resolution_action = "created"
                 
        except Exception as e:
            logger.error(f"Account JIT failed for {current_user.uid}: {e}")


    # Step C: Fetch and Process Account Data
    account_data = {}
    if account_id:
        acc_doc_ref = db.collection("accounts").document(account_id)
        acc_snap = acc_doc_ref.get()
        if acc_snap.exists:
            account_data = acc_snap.to_dict()
        else:
            account_id = None # Link points to deleted account

    # Fallback Response (Safety Gate)
    if not account_id:
        # [FIX] Return proper free tier credits even for users without account
        # [2026-01] SNS users don't need phone verification (soft gate only)
        verified_sns_providers = {"google.com", "apple.com", "custom", "line"}
        fallback_is_sns_verified = token_provider in verified_sns_providers
        fallback_needs_phone = not fallback_is_sns_verified and not bool(token_phone)
        return MeResponse(
            id=current_user.uid, uid=current_user.uid, displayName=final_display_name,
            email=current_user.email, photoUrl=current_user.photo_url,
            provider=token_provider, providers=[token_provider] if token_provider else [],
            needsPhoneVerification=fallback_needs_phone,
            needsSnsLogin=bool(token_provider == "phone"),
            plan="free",
            credits={
                "cloudSecondsRemaining": FREE_LIMITS["cloud_stt_sec"],  # 1800 (30 min)
                "summaryRemaining": FREE_LIMITS["summary_generated"],  # 3
                "quizRemaining": FREE_LIMITS["quiz_generated"],  # 3
            },
            freeCloudCreditsRemaining=int(FREE_LIMITS["cloud_stt_sec"]),  # 1800
            freeSummaryCreditsRemaining=FREE_LIMITS["summary_generated"],  # 3
            freeQuizCreditsRemaining=FREE_LIMITS["quiz_generated"],  # 3
            cloudSessionLimit=FREE_LIMITS["cloud_sessions_started"],  # 10
            serverSessionLimit=FREE_LIMITS["server_session"],  # 5
            phoneRequiredFor=[] if bool(token_phone) else ["subscriptionRestore", "accountMerge"],
        )

    # Step D: Data Hydration & Consistency Check
    # [FIX] Multi-source phone lookup for needsPhoneVerification
    # Priority: accounts > users > uid_links
    phone_in_db = account_data.get("phoneE164")
    if not phone_in_db:
        # Fallback 1: Check users/{uid} (set by link_phone)
        phone_in_db = user_profile.get("phoneE164")
    if not phone_in_db:
        # Fallback 2: Check uid_links/{uid} (set by link_phone)
        phone_in_db = link_data.get("phoneE164")

    providers_in_db = set(account_data.get("providers", []))
    
    dirty = False
    update_params = {"updatedAt": firestore.SERVER_TIMESTAMP}
    
    # Provider Hydration
    if token_provider and token_provider not in providers_in_db:
        providers_in_db.add(token_provider)
        update_params["providers"] = list(providers_in_db)
        dirty = True
        
    # Phone Hydration
    # Case 1: Token has phone, DB doesn't
    if token_phone and not phone_in_db:
        phone_in_db = token_phone
        update_params["phoneE164"] = token_phone
        dirty = True
        # Also ensure phone_numbers registry
        try:
            db.collection("phone_numbers").document(token_phone).set({
                "standardOwnerUid": current_user.uid, "isVerified": True, "updatedAt": now
            }, merge=True)
        except: pass

    # Case 2: [FIX] phone_in_db from users/links but NOT in account -> Sync to account
    if phone_in_db and not account_data.get("phoneE164"):
        logger.info(f"[/users/me] Self-repair: Syncing phoneE164={phone_in_db} to account {account_id}")
        update_params["phoneE164"] = phone_in_db
        dirty = True

    if dirty:
        db.collection("accounts").document(account_id).update(update_params)

    # Step E: Determine State Flags
    # PRINCIPLE: Token is Truth.
    # If token has phone_number, user IS verified.

    # 1. needsPhoneVerification
    # [2024-01 POLICY CHANGE] Phone is NO LONGER a hard gate for login.
    # SNS-authenticated users (Google, Apple, LINE) are considered verified.
    # Phone is only required for specific features (share, subscription restore).
    #
    # needsPhoneVerification = False for:
    # - Users with phone in token
    # - Users with phone in DB
    # - Users with appleAppAccountToken (device-verified)
    # - Users authenticated via SNS providers (google.com, apple.com, custom/LINE)
    #
    # needsPhoneVerification = True ONLY for:
    # - Anonymous or unverified users who need phone for identity
    has_phone_in_token = bool(token_phone)
    has_phone_in_db = bool(phone_in_db)
    has_app_account_token = bool(user_profile.get("appleAppAccountToken"))

    # SNS providers that count as "verified identity"
    verified_sns_providers = {"google.com", "apple.com", "custom", "line"}
    is_sns_verified = token_provider in verified_sns_providers or any(p in providers_in_db for p in verified_sns_providers)

    needs_phone = False
    # Only require phone if user has NO verified identity at all
    if not has_phone_in_token and not has_phone_in_db and not has_app_account_token and not is_sns_verified:
         needs_phone = True

    # [FIX] JIT Hydration for Phone
    # If token has phone, but DB doesn't -> Save it now.
    if has_phone_in_token and not has_phone_in_db:
        logger.info(f"JIT Hydrating phone {token_phone} for user {current_user.uid}")
        try:
            db.collection("users").document(current_user.uid).set({
                "phoneE164": token_phone,
                "updatedAt": firestore.SERVER_TIMESTAMP
            }, merge=True)
            phone_in_db = token_phone # Update local var for response
        except Exception as e:
            logger.error(f"JIT hydration failed: {e}")

    # 2. needsSnsLogin: Phone-only session detected but it's not the primary identity
    # (If we assume SNS is primary, and phone is just for linking)
    has_any_sns_linked = any(p in providers_in_db for p in ["google.com", "apple.com", "custom", "line"])
    needs_sns = False
    if token_provider == "phone":
        if not has_any_sns_linked:
             needs_sns = True


    # Step F: Plan & Entitlement (Standard Owner logic)
    raw_plan = account_data.get("plan", "free")
    # ... JIT Expiration ...
    if raw_plan != "free":
        expires_at = account_data.get("planExpiresAt")
        if expires_at:
            if not expires_at.tzinfo: expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at < now:
                raw_plan = "free"
                background_tasks.add_task(downgrade_account_if_expired, account_id, current_user.uid)

    # Ownership check for paid plan
    final_plan = "free"
    if raw_plan != "free" and phone_in_db:
        try:
            p_doc = db.collection("phone_numbers").document(phone_in_db).get()
            if p_doc.exists and p_doc.to_dict().get("standardOwnerUid") == current_user.uid:
                final_plan = raw_plan
        except: pass

    # [FIX] Calculate credits dynamically from plan limits - usage
    # This ensures free users always see their correct remaining quota
    try:
        usage_report = await cost_guard.get_usage_report(account_id, mode="account")
    except Exception as e:
        logger.warning(f"[/users/me] Failed to get usage report for {account_id}: {e}")
        usage_report = {}

    # Select limits based on effective plan
    if final_plan == "premium":
        cloud_limit = PREMIUM_LIMITS["cloud_stt_sec"]
        summary_limit = PREMIUM_LIMITS.get("llm_calls", 1000)
        quiz_limit = PREMIUM_LIMITS.get("llm_calls", 1000)
        cloud_session_limit = 999999
    elif final_plan == "basic":
        cloud_limit = BASIC_LIMITS["cloud_stt_sec"]
        summary_limit = BASIC_LIMITS["summary_generated"]
        quiz_limit = BASIC_LIMITS["quiz_generated"]
        cloud_session_limit = BASIC_LIMITS["cloud_sessions_started"]
    else:  # free
        cloud_limit = FREE_LIMITS["cloud_stt_sec"]  # 1800 (30 min)
        summary_limit = FREE_LIMITS["summary_generated"]  # 3
        quiz_limit = FREE_LIMITS["quiz_generated"]  # 3
        cloud_session_limit = FREE_LIMITS["cloud_sessions_started"]  # 10

    # Calculate remaining = limit - used
    cloud_used = usage_report.get("usedSeconds", 0.0)
    summary_used = usage_report.get("summaryGenerated", 0)
    quiz_used = usage_report.get("quizGenerated", 0)
    cloud_sessions_used = usage_report.get("sessionsStarted", 0)

    cloud_remaining = max(0.0, cloud_limit - cloud_used)
    summary_remaining = max(0, summary_limit - summary_used)
    quiz_remaining = max(0, quiz_limit - quiz_used)
    cloud_sessions_remaining = max(0, cloud_session_limit - cloud_sessions_used)

    # Build calculated credits dict
    calculated_credits = {
        "cloudSecondsRemaining": cloud_remaining,
        "summaryRemaining": summary_remaining,
        "quizRemaining": quiz_remaining,
    }

    # Construct Response
    return MeResponse(
        id=current_user.uid,
        uid=current_user.uid,
        displayName=final_display_name,
        email=current_user.email,
        photoUrl=current_user.photo_url,
        provider=token_provider,
        providers=list(providers_in_db),
        
        # Account Info
        accountId=account_id,
        phoneE164=phone_in_db,
        plan=final_plan,
        credits=calculated_credits,  # [FIX] Use calculated credits
        needsPhoneVerification=needs_phone,
        needsSnsLogin=needs_sns,

        # Compat defaults
        isShareable=user_profile.get("isShareable", True),
        allowSearch=user_profile.get("allowSearch", True),
        hasUsername=final_has_username,
        username=final_username,
        serverSessionCount=0,
        activeSessionCount=0,
        serverSessionLimit=999 if final_plan == "premium" else (BASIC_LIMITS["server_session"] if final_plan == "basic" else FREE_LIMITS["server_session"]),
        cloudSessionCount=int(cloud_sessions_used),
        cloudSessionLimit=cloud_session_limit,
        freeCloudCreditsRemaining=int(cloud_remaining),  # [FIX] Use calculated value (int for model)
        freeSummaryCreditsRemaining=int(summary_remaining),  # [FIX] Use calculated value
        freeQuizCreditsRemaining=int(quiz_remaining),  # [FIX] Use calculated value
        cloud=None,
        securityState="normal",
        riskScore=0,
        
        # [NEW] Resolution Details
        # These fields help client UI show "Account Restored" message
        accountResolution={
            "action": resolution_action,
            "reason": resolution_reason,
            "resolvedAt": now.isoformat()
        } if resolution_action != "none" else None,

        # [NEW 2026-01] Feature-level phone gate
        # If user has no phone, these features require phone verification
        phoneRequiredFor=[] if (has_phone_in_token or has_phone_in_db) else ["subscriptionRestore", "accountMerge"]
    )

@router.get("/me/entitlement", response_model=EntitlementResponse)
async def get_my_entitlement(current_user: CurrentUser = Depends(get_current_user)):
    """
    ユーザーの現在の権限状態を返す (StoreKit 2 / billing flow対応)
    """
    sub_ref = db.collection("users").document(current_user.uid).collection("subscriptions").document("apple")
    doc = sub_ref.get()
    
    if not doc.exists:
        return EntitlementResponse(
             entitled=False,
             plan="free"
        )
    
    data = doc.to_dict()
    
    # Check expiration logic explicitly or reuse stored status
    status = data.get("status")
    expires_ms = data.get("expiresDateMs")
    
    # Simple check
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    is_expired = False
    if expires_ms and expires_ms <= now_ms:
        is_expired = True
        
    entitled = False
    if status == "active" and not is_expired:
        entitled = True
    elif status == "grace_period":
        entitled = True
        
    plan = data.get("plan", "free")
    if not entitled:
        plan = "free"

    return EntitlementResponse(
        entitled=entitled,
        plan=plan,
        expiresAt=expires_ms,
        source=data.get("source")
    )

def utcnow():
    return datetime.now(timezone.utc)

@router.post("/claim-username")
async def claim_username(req: ClaimUsernameRequest, current_user: CurrentUser = Depends(get_current_user)):
    username_raw = req.username.strip()
    username_lower = username_raw.lower()

    if not USERNAME_RE.match(username_lower):
        raise HTTPException(400, "Invalid username format. 3-20 chars, a-z0-9_ only.")

    user_ref = db.collection("users").document(current_user.uid)
    claim_ref = db.collection("username_claims").document(username_lower)

    @firestore.transactional
    def txn(transaction: firestore.Transaction):
        user_doc = user_ref.get(transaction=transaction)
        if user_doc.exists and user_doc.to_dict().get("username"):
            # Already set
            raise HTTPException(409, "Username already set")

        claim_doc = claim_ref.get(transaction=transaction)
        if claim_doc.exists:
            raise HTTPException(409, "Username already taken")

        # Claim it
        transaction.set(claim_ref, {
            "uid": current_user.uid,
            "username": username_lower,
            "createdAt": utcnow(),
        })

        # Update User
        user_data = user_doc.to_dict() if user_doc.exists else {}
        created_at = user_data.get("createdAt") or firestore.SERVER_TIMESTAMP
        
        transaction.set(user_ref, {
            "uid": current_user.uid,
            "username": username_lower,
            "usernameLower": username_lower,
            # If no displayName yet, use username
            "displayName": user_data.get("displayName") or username_raw,
            "updatedAt": firestore.SERVER_TIMESTAMP,
            "createdAt": created_at,
            "allowSearch": True,
            "isShareable": True,
            "hasUsername": True, # [FIX] Ensure this is set for GET /me
        }, merge=True)

    transaction = db.transaction()
    try:
        txn(transaction)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"claim failed: {e}")

    return {"ok": True, "username": username_lower}


@router.get("/lookup", response_model=dict)
async def users_lookup(uids: str = Query(..., description="Comma separated uids"), current_user: CurrentUser = Depends(get_current_user)):
    uid_list = [u.strip() for u in uids.split(",") if u.strip()]
    if len(uid_list) > 50:
        raise HTTPException(400, "Too many uids")
    
    if not uid_list:
        return {"users": []}

    refs = [db.collection("users").document(u) for u in uid_list]
    docs = db.get_all(refs)

    res = []
    for d in docs:
        if d.exists:
            x = d.to_dict()
            res.append({
                "uid": d.id,
                "displayName": x.get("displayName") or x.get("username"),
                "username": x.get("username"),
                "photoUrl": x.get("photoUrl"),
            })
    return {"users": res}


def _get_plan_from_product_id(product_id: str) -> str:
    """
    Apple App Storeの製品IDからプラン名を決定する。
    """
    return PRODUCT_TO_PLAN.get(product_id, "basic")

def _is_active(transaction_info: dict) -> bool:
    """
    検証済みトランザクション情報からサブスクリプションが有効か判定する。
    """
    expires_date_ms = transaction_info.get("expiresDate")
    if not expires_date_ms:
        # 期限がない場合は有効とみなす（例: ライフタイム）
        return True
    
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return int(expires_date_ms) > now_ms


@router.post("/me/subscription/apple:claim", response_model=EntitlementResponse)
async def claim_apple_subscription(
    req: SubscriptionVerifyRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Validates and claims an Apple subscription.
    - Creates/Updates `entitlements` ledger entry.
    - Locks `originalTransactionId` to the first `ownerUserId`.
    - Validates `appAccountToken` to prevent hijacking.
    """
    if not req.signedTransactionInfo:
        raise HTTPException(
            status_code=400,
            detail="signedTransactionInfo is required.",
        )

    # 1. JWS Verification
    from app.services.apple import apple_service
    from starlette.concurrency import run_in_threadpool

    try:
        transaction_info = await run_in_threadpool(apple_service.verify_jws, req.signedTransactionInfo)
        if not transaction_info:
            raise HTTPException(status_code=400, detail="Invalid signedTransactionInfo")
    except Exception as e:
        logger.error(f"JWS verification failed: {e}")
        raise HTTPException(status_code=400, detail=f"JWS verification failed: {e}")

    # 2. Extract Key Fields
    otid = transaction_info.get("originalTransactionId")
    product_id = transaction_info.get("productId")
    if not otid or not product_id:
        raise HTTPException(status_code=400, detail="Missing originalTransactionId or productId")

    # 2.5 appAccountToken Verification (Anti-Hijack)
    tx_app_token = transaction_info.get("appAccountToken")
    user_doc = db.collection("users").document(current_user.uid).get()
    expected_token = (user_doc.to_dict() or {}).get("appleAppAccountToken")
    
    if tx_app_token:
        # Enforce if token is present in receipt (StoreKit 2)
        if not expected_token:
             # Transaction has token, but user profile doesn't? 
             # Error (400) telling client to register token.
             raise HTTPException(status_code=400, detail="APPLE_APP_ACCOUNT_TOKEN_NOT_REGISTERED")
             
        if tx_app_token != expected_token:
            # Token mismatch -> Potential hijacking attempt or different user
            logger.warning(f"AppAccountToken mismatch for user {current_user.uid}. Expected {expected_token}, got {tx_app_token}")
            raise HTTPException(status_code=403, detail="APP_ACCOUNT_TOKEN_MISMATCH")

    # 3. Transactional Claim
    try:
        @firestore.transactional
        def claim_in_transaction(transaction):
            now = datetime.now(timezone.utc)
            
            entitlement_id = f"apple:{otid}"
            entitlement_ref = db.collection("entitlements").document(entitlement_id)
            user_ref = db.collection("users").document(current_user.uid)
            
            entitlement_doc = transaction.get(entitlement_ref)
            expires_ms = transaction_info.get("expiresDate")
            current_period_end = parse_ms_to_dt(expires_ms)
            active = is_active_from_expires_ms(expires_ms)
            plan = plan_from_product_id(product_id) if active else "free"
            status = "active" if active else "expired"
            
            # --- [NEW] Standard Owner Check ---
            # Resolve phone from Account (User -> Link -> Account)
            # Optimization: We might need to fetch Account/Link here if not in user object.
            # But wait, we are inside a transaction.
            
            # 1. Get User Link to find Account
            link_ref = db.collection("uid_links").document(current_user.uid)
            link_doc = transaction.get(link_ref)
            if not link_doc.exists:
                # [SECURITY] Block claim if UID is not yet formalised with a Phone Link.
                # This ensures standardOwnerUid locking is enforced from the first purchase.
                raise HTTPException(
                    status_code=403, 
                    detail="PHONE_LINK_REQUIRED_TO_CLAIM_SUBSCRIPTION"
                )
            else:
                account_id = link_doc.to_dict().get("accountId")
                acc_ref = db.collection("accounts").document(account_id)
                acc_doc = transaction.get(acc_ref)
                phone = acc_doc.to_dict().get("phoneE164")
                
                if phone:
                    phone_ref = db.collection("phone_numbers").document(phone)
                    phone_doc = transaction.get(phone_ref)
                    
                    if not phone_doc.exists:
                        # Should exist if linked, but auto-create if missing
                        transaction.set(phone_ref, {"standardOwnerUid": current_user.uid, "updatedAt": now})
                    else:
                        p_data = phone_doc.to_dict()
                        std_owner = p_data.get("standardOwnerUid")
                        
                        if std_owner and std_owner != current_user.uid:
                            # CONFLICT: Another user already owns Standard for this phone
                            # We must deny the claim OR (if policy allows) overwrite?
                            # User says: "already entered -> 409"
                            raise EntitlementConflictError(f"Standard plan is already owned by another account on phone {phone}")
                        
                        if not std_owner:
                            # Claim it
                            transaction.update(phone_ref, {"standardOwnerUid": current_user.uid, "updatedAt": now})

            # --- New Entitlement (First Claim) ---
            if not entitlement_doc.exists:
                entitlement_data = {
                    "provider": "apple",
                    "providerEntitlementId": otid,
                    "ownerUserId": current_user.uid,
                    "productId": product_id,
                    "environment": transaction_info.get("environment"),
                    "status": status,
                    "plan": plan,
                    "currentPeriodEnd": current_period_end,
                    "appAccountToken": expected_token, # Store expected token for record
                    "createdAt": now,
                    "updatedAt": now,
                }
                transaction.set(entitlement_ref, entitlement_data)
                
                # Update User Reference (Link to entitlement)
                transaction.update(user_ref, {
                    "appleEntitlementId": entitlement_id,
                    "planUpdatedAt": now,
                })
                return entitlement_data

            # --- Existing Entitlement (Refresh) ---
            existing = entitlement_doc.to_dict()
            owner_uid = existing.get("ownerUserId")
            
            if owner_uid != current_user.uid:
                # CONFLICT: Owned by another user
                logger.warning(f"Entitlement conflict: {entitlement_id} owned by {owner_uid}, requested by {current_user.uid}")
                raise HTTPException(status_code=409, detail="ENTITLEMENT_OWNED_BY_ANOTHER_ACCOUNT")
            
            # Update Existing
            update_data = {
                "updatedAt": now,
                "status": status,
                "plan": plan,
                "productId": product_id, 
                "currentPeriodEnd": current_period_end,
            }
            transaction.update(entitlement_ref, update_data)
            transaction.update(user_ref, {"planUpdatedAt": now})
            
            return {**existing, **update_data}

        # Run Transaction
        transaction = db.transaction()
        final_data = claim_in_transaction(transaction)
        
        is_active = final_data.get("status") == "active"
        expires_ms = None
        if final_data.get("currentPeriodEnd"):
             expires_ms = int(final_data["currentPeriodEnd"].timestamp() * 1000)

        return EntitlementResponse(
            entitled=is_active,
            plan=final_data.get("plan", "free") if is_active else "free",
            expiresAt=expires_ms,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Subscription claim failed for user {current_user.uid}: {e}")
        status_code = 500
        if "ENTITLEMENT_OWNED_BY_ANOTHER_ACCOUNT" in str(e): 
             status_code = 409
        if isinstance(e, EntitlementConflictError):
             status_code = 409
        raise HTTPException(status_code=status_code, detail=f"Failed to process subscription claim: {type(e).__name__} {str(e)}")



@router.get("/me/capabilities", response_model=CapabilitiesResponse)
async def get_my_capabilities(current_user: CurrentUser = Depends(get_current_user)):
    """
    プランに応じた機能フラグ・制限情報を返す。
    クライアントはこれを信じてUIを制御する。
    """
    # Get consolidated usage report
    report = await cost_guard.get_usage_report(current_user.uid)
    plan = report.get("plan", "free")
    
    limit_min = int(report.get("limitSeconds", 0) / 60)
    used_recording_min = int(report.get("usedSeconds", 0) / 60)
        
    # Plan Definitions (Backend Source of Truth)
    if plan == "premium":
        caps = CapabilitiesResponse(
            plan="pro", # iOS compat: premium is "pro" in UI mapping
            canRealtimeTranslate=True,
            sttPostEngine="whisper_large_v3",
            monthlyRecordingLimitMin=limit_min,
            remainingRecordingMin=max(0, limit_min - used_recording_min),
            canRegenerateTranscript=True,
            maxSessions=PREMIUM_LIMITS["server_session"],
            maxSummaries=PREMIUM_LIMITS["llm_calls"],
            maxQuizzes=PREMIUM_LIMITS["llm_calls"]
        )
    elif plan == "basic":
        caps = CapabilitiesResponse(
            plan="basic",
            canRealtimeTranslate=False,
            sttPostEngine="gcp_speech",
            monthlyRecordingLimitMin=limit_min,
            remainingRecordingMin=max(0, limit_min - used_recording_min),
            canRegenerateTranscript=False,
            maxSessions=BASIC_LIMITS["server_session"],
            maxSummaries=BASIC_LIMITS["summary_generated"],
            maxQuizzes=BASIC_LIMITS["quiz_generated"]
        )
    else:  # free
        caps = CapabilitiesResponse(
            plan="free",
            canRealtimeTranslate=False,
            sttPostEngine="gcp_speech",
            monthlyRecordingLimitMin=limit_min,
            remainingRecordingMin=max(0, limit_min - used_recording_min),
            canRegenerateTranscript=False,
            maxSessions=FREE_LIMITS["server_session"], 
            maxSummaries=FREE_LIMITS["summary_generated"],
            maxQuizzes=FREE_LIMITS["quiz_generated"]
        )
        
    return caps


# iOS互換エイリアス: /capabilities (ルートレベル)
# → /users/me/capabilities と同じ処理
# Note: このエイリアスはルーターのprefixが/usersなので /users/capabilities になる
# ルートレベルの /capabilities が必要な場合は main.py で別途追加

@router.get("/me/usage", response_model=None)
async def get_my_usage_alias(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    current_user: CurrentUser = Depends(get_current_user)
):
    """
    iOS互換エイリアス: /users/me/usage → /usage/me/summary
    """
    from datetime import date, timedelta
    from app.services.usage import usage_logger
    from app.usage_models import UsageSummaryResponse
    
    if not to_date:
        to_date = date.today().isoformat()
    if not from_date:
        from_date = (date.today() - timedelta(days=30)).isoformat()
    
    summary = await usage_logger.get_user_usage_summary(
        user_id=current_user.uid,
        from_date=from_date,
        to_date=to_date
    )
    
    return UsageSummaryResponse(**summary)


@router.patch("/me", response_model=MeResponse)
async def update_me(
    body: MeUpdateRequest,
    current_user: CurrentUser = Depends(get_current_user)
):
    doc_ref = db.collection("users").document(current_user.uid)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="User not found")

    update_data = {}
    if body.displayName is not None:
        update_data["displayName"] = body.displayName
    if body.email is not None:
        update_data["email"] = body.email
        update_data["emailLower"] = body.email.lower()
    if body.allowSearch is not None:
        update_data["allowSearch"] = body.allowSearch
    if body.isShareable is not None:
        update_data["isShareable"] = body.isShareable

    if update_data:
        update_data["updatedAt"] = firestore.SERVER_TIMESTAMP
        doc_ref.update(update_data)

    refreshed = doc_ref.get().to_dict() or {}
    is_shareable = refreshed.get("isShareable", refreshed.get("allowSearch", True))
    return MeResponse(
        uid=current_user.uid,
        displayName=refreshed.get("displayName"),
        email=refreshed.get("email"),
        photoUrl=refreshed.get("photoUrl"),
        providers=refreshed.get("providers", []),
        provider=refreshed.get("provider"),
        allowSearch=refreshed.get("allowSearch", True),
        shareCode=refreshed.get("shareCode"),
        isShareable=is_shareable,
    )


# --- Profile (displayName + share settings) --- #

@router.get("/me/profile", response_model=UserProfileResponse)
async def get_profile(current_user: CurrentUser = Depends(get_current_user)):
    doc_ref = db.collection("users").document(current_user.uid)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="User not found")
    data = doc.to_dict() or {}
    return UserProfileResponse(
        uid=current_user.uid,
        displayName=data.get("displayName"),
        shareCode=data.get("shareCode"),
        isShareable=data.get("isShareable", data.get("allowSearch", True)),
    )


@router.patch("/me/profile", response_model=UserProfileResponse)
async def update_profile(
    body: UserProfileUpdateRequest,
    current_user: CurrentUser = Depends(get_current_user)
):
    doc_ref = db.collection("users").document(current_user.uid)
    doc = doc_ref.get()
    if not doc.exists:
        # プロファイルがない場合は作成する
        base_data = {
            "displayName": body.displayName or current_user.display_name or "ゲスト",
            "isShareable": body.isShareable if body.isShareable is not None else True,
            "allowSearch": body.isShareable if body.isShareable is not None else True,
            "photoUrl": None,
            "provider": None,
            "providers": [],
            "createdAt": firestore.SERVER_TIMESTAMP,
            "updatedAt": firestore.SERVER_TIMESTAMP,
        }
        doc_ref.set(base_data, merge=True)
        refreshed = base_data
    else:
        update_data = {}
        if body.displayName is not None:
            update_data["displayName"] = body.displayName
        if body.isShareable is not None:
            update_data["isShareable"] = body.isShareable
            update_data["allowSearch"] = body.isShareable  # 後方互換

        if update_data:
            update_data["updatedAt"] = firestore.SERVER_TIMESTAMP
            doc_ref.update(update_data)
        refreshed = doc_ref.get().to_dict() or {}

    return UserProfileResponse(
        uid=current_user.uid,
        displayName=refreshed.get("displayName"),
        shareCode=refreshed.get("shareCode"),
        isShareable=refreshed.get("isShareable", refreshed.get("allowSearch", True)),
    )


# --- Share Code Handling --- #

def _generate_code() -> str:
    # 000000-999999
    return f"{random.randint(0, 999999):06d}"


@router.post("/me/share-code", response_model=ShareCodeResponse)
async def create_or_refresh_share_code(current_user: CurrentUser = Depends(get_current_user)):
    user_id = current_user.uid
    user_ref = db.collection("users").document(user_id)
    
    # Check old code to remove it
    user_doc = user_ref.get()
    if user_doc.exists:
        old_code = user_doc.to_dict().get("shareCode")
        if old_code:
            # Delete old mapping
            db.collection("shareCodes").document(old_code).delete()

    # Generate new unique code
    for _ in range(10):
        code = _generate_code()
        code_ref = db.collection("shareCodes").document(code)
        if not code_ref.get().exists:
            # Set reverse mapping
            code_ref.set({
                "userId": user_id,
                "createdAt": firestore.SERVER_TIMESTAMP
            })
            # Set user mapping
            user_ref.update({
                "shareCode": code,
                "shareCodeUpdatedAt": firestore.SERVER_TIMESTAMP,
                "isShareable": True, # Ensure shareable
                "allowSearch": True
            })
            return ShareCodeResponse(shareCode=code)

    raise HTTPException(status_code=500, detail="Failed to generate unique share code")


@router.post("/share_lookup", response_model=ShareLookupResponse)
async def share_lookup(body: ShareLookupRequest):
    code = (body.code or "").strip()
    if not code:
        return ShareLookupResponse(found=False)

    qs = (
        db.collection("users")
        .where("shareCode", "==", code)
        .where("shareCodeSearchEnabled", "==", True)
        .limit(1)
        .stream()
    )
    docs = list(qs)
    if not docs:
        return ShareLookupResponse(found=False)

    doc = docs[0]
    data = doc.to_dict() or {}
    if data.get("isShareable", data.get("allowSearch", True)) is False:
        return ShareLookupResponse(found=False)

    return ShareLookupResponse(
        found=True,
        targetUserId=doc.id,
        displayName=data.get("displayName"),
    )


@router.get("/search_by_share_code", response_model=ShareLookupResponse)
async def search_by_share_code(code: str, current_user: CurrentUser = Depends(get_current_user)):
    code = (code or "").strip()
    if len(code) != 6:
        raise HTTPException(status_code=400, detail="share code must be 6 characters")

    qs = (
        db.collection("users")
        .where("shareCode", "==", code)
        .where("shareCodeSearchEnabled", "==", True)
        .limit(1)
        .stream()
    )
    docs = list(qs)
    if not docs:
        raise HTTPException(status_code=404, detail="User not found")

    doc = docs[0]
    data = doc.to_dict() or {}
    if data.get("isShareable", data.get("allowSearch", True)) is False:
        raise HTTPException(status_code=404, detail="User not found")

    return ShareLookupResponse(
        found=True,
        targetUserId=doc.id,
        displayName=data.get("displayName"),
    )


@router.delete("/me", status_code=204)
async def delete_me(current_user: CurrentUser = Depends(get_current_user)):
    """
    [ASYNC] Request account deletion.
    Records a deletion request and schedules hard delete after a grace period.
    """
    try:
        now = datetime.now(timezone.utc)
        delete_after = deletion_schedule_at(now)

        user_ref = db.collection("users").document(current_user.uid)
        user_doc = user_ref.get()
        user_data = user_doc.to_dict() if user_doc.exists else {}

        email = user_data.get("email") or current_user.email
        email_lower = email.lower() if email else None
        provider_id = user_data.get("provider")
        providers = user_data.get("providers") or []
        if not provider_id and providers:
            provider_id = providers[0]

        req_data = {
            "uid": current_user.uid,
            "email": email,
            "emailLower": email_lower,
            "providerId": provider_id,
            "providers": providers,
            "status": "requested",
            "requestedAt": now,
            "deleteAfterAt": delete_after,
        }
        req_data = {k: v for k, v in req_data.items() if v is not None}
        db.collection(REQUESTS_COLLECTION).document(current_user.uid).set(req_data, merge=True)

        user_ref.set(
            {
                "deletionRequestedAt": now,
                "deletionScheduledAt": delete_after,
                "deletionStatus": "requested",
                "updatedAt": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )

        if email_lower and provider_id:
            lock_data = {
                "uid": current_user.uid,
                "emailLower": email_lower,
                "providerId": provider_id,
                "status": "requested",
                "requestedAt": now,
                "deleteAfterAt": delete_after,
            }
            db.collection(LOCKS_COLLECTION).document(deletion_lock_id(email_lower, provider_id)).set(
                lock_data,
                merge=True,
            )

        logger.info(f"Account deletion requested for {current_user.uid}.")
        
        # We return 204 immediately. 
        # Hard delete is scheduled after the grace period.
        return 
        
    except Exception as e:
        logger.error(f"Failed to request deletion for {current_user.uid}: {e}")
        raise HTTPException(status_code=500, detail="Failed to request account deletion")
        # Build logic: Do we fail the request? Usually 204 implies success.
        
    return


# --- Consent Log API --- #
from app.util_models import ConsentRequest, ConsentResponse

@router.post("/me/consents", response_model=ConsentResponse)
async def post_consent(
    body: ConsentRequest,
    current_user: CurrentUser = Depends(get_current_user)
):
    """
    Record user consent to Terms of Service and Privacy Policy.
    Stores in Firestore subcollection for audit trail.
    """
    now = datetime.now(timezone.utc)
    
    consent_data = {
        "uid": current_user.uid,
        "termsVersion": body.termsVersion,
        "privacyVersion": body.privacyVersion,
        "acceptedAt": now,  # Server timestamp (authoritative)
        "clientAcceptedAt": body.acceptedAt,  # Client timestamp (for reference)
        "appVersion": body.appVersion,
        "build": body.build,
        "platform": body.platform or "ios",
        "locale": body.locale,
        "createdAt": firestore.SERVER_TIMESTAMP,
    }
    
    # Store in subcollection: users/{uid}/consents/{docId}
    # Using termsVersion_privacyVersion as doc ID for idempotency
    doc_id = f"{body.termsVersion}_{body.privacyVersion}"
    consent_ref = db.collection("users").document(current_user.uid).collection("consents").document(doc_id)
    
    consent_ref.set(consent_data, merge=True)
    
    # Also update user doc with latest consent info (for quick lookup)
    user_ref = db.collection("users").document(current_user.uid)
    user_ref.update({
        "consent": {
            "termsVersion": body.termsVersion,
            "privacyVersion": body.privacyVersion,
            "acceptedAt": now,
        },
        "updatedAt": firestore.SERVER_TIMESTAMP,
    })
    
    logger.info(f"Consent logged for {current_user.uid}: terms={body.termsVersion}, privacy={body.privacyVersion}")
    
    return ConsentResponse(
        ok=True,
        termsVersion=body.termsVersion,
        privacyVersion=body.privacyVersion,
        acceptedAt=now,
    )


# ============================================================
# Account Deletion (Apple App Store Compliance)
# ============================================================

class DeleteRequestResponse(BaseModel):
    ok: bool
    state: str  # "none" | "queued" | "running" | "done" | "failed"
    jobId: Optional[str] = None
    error: Optional[str] = None


@router.post("/me:delete", response_model=DeleteRequestResponse)
async def request_account_deletion(current_user: CurrentUser = Depends(get_current_user)):
    """
    Initiates account deletion (async).
    The actual deletion is performed by a Cloud Tasks worker.
    Client should poll GET /me:delete/status until state == "done".
    """
    from app.task_queue import enqueue_nuke_user_task
    
    uid = current_user.uid
    user_ref = db.collection("users").document(uid)
    
    # 1. Check existing deletion state (idempotency)
    user_snap = user_ref.get()
    if not user_snap.exists:
        # User doc already deleted
        return DeleteRequestResponse(ok=True, state="done")
    
    user_data = user_snap.to_dict()
    deletion = user_data.get("deletion", {})
    state = deletion.get("state", "none")
    
    if state in ("queued", "running"):
        # Already in progress
        return DeleteRequestResponse(ok=True, state=state, jobId=deletion.get("jobId"))
    
    if state == "done":
        return DeleteRequestResponse(ok=True, state="done")
    
    # 2. Create deletion job
    now = datetime.now(timezone.utc)
    job_id = f"del_{uid}_{int(now.timestamp())}"
    
    user_ref.set({
        "deletion": {
            "state": "queued",
            "requestedAt": now,
            "jobId": job_id
        }
    }, merge=True)
    
    # 3. Enqueue to Cloud Tasks
    try:
        enqueue_nuke_user_task(uid)
        logger.info(f"Account deletion queued for {uid}, jobId={job_id}")
    except Exception as e:
        logger.error(f"Failed to enqueue deletion task for {uid}: {e}")
        # Keep state as "queued" - worker can retry via cron or manual trigger
    
    return DeleteRequestResponse(ok=True, state="queued", jobId=job_id)


@router.get("/me:delete/status", response_model=DeleteRequestResponse)
async def get_deletion_status(current_user: CurrentUser = Depends(get_current_user)):
    """
    Returns the current status of account deletion.
    Used for polling from client.
    """
    uid = current_user.uid
    user_ref = db.collection("users").document(uid)
    snap = user_ref.get()
    
    if not snap.exists:
        # User doc already deleted = done
        return DeleteRequestResponse(ok=True, state="done")
    
    deletion = snap.to_dict().get("deletion", {})
    return DeleteRequestResponse(
        ok=True,
        state=deletion.get("state", "none"),
        jobId=deletion.get("jobId"),
        error=deletion.get("error")
    )
