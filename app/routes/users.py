from fastapi import APIRouter, Depends, Query, HTTPException
from typing import List, Optional
import random
import string
from datetime import datetime, timezone
from google.cloud import firestore
from app.dependencies import get_current_user, User
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
import re
import logging

logger = logging.getLogger("app.users")

USERNAME_RE = re.compile(r"^[a-z0-9_]{3,20}$")

router = APIRouter(prefix="/users")

@router.get("/search", response_model=List[PublicUser])
async def search_users(
    q: str = Query(..., min_length=1, description="Search query (email or display name)"),
    current_user: User = Depends(get_current_user)
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


@router.get("/me", response_model=MeResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    doc_ref = db.collection("users").document(current_user.uid)
    doc = doc_ref.get()
    
    # [BOOTSTRAP] If user document doesn't exist, create it ("Source of Truth")
    if not doc.exists:
        base_data = {
            "uid": current_user.uid,
            "email": current_user.email,
            "emailLower": (current_user.email or "").lower(),
            "displayName": current_user.display_name or "New User",
            "photoUrl": current_user.picture,
            "providers": current_user.firebase_user_record.provider_data if hasattr(current_user, "firebase_user_record") else [],
            "plan": "free", # Default plan
            "createdAt": firestore.SERVER_TIMESTAMP,
            "updatedAt": firestore.SERVER_TIMESTAMP,
            "isShareable": True,
            "allowSearch": True,
            "freeCloudCreditsRemaining": 1, # Default 1 credit for Free plan
            "securityState": "normal",
            "riskScore": 0,
        }
        # Try-catch in case of race condition
        try:
            doc_ref.set(base_data, merge=True)
            data = base_data
        except Exception:
            # Re-fetch if creation failed (race condition)
            data = doc_ref.get().to_dict() or {}
    else:
        data = doc.to_dict() or {}
        
    is_shareable = data.get("isShareable", data.get("allowSearch", True))
    # [NEW] Count active sessions for Free plan paywall
    active_session_count = None
    if data.get("plan", "free") == "free":
        try:
            # Query sessions without complex filters to avoid missing index errors
            active_sessions_query = db.collection("sessions")\
                .where("ownerUid", "==", current_user.uid)\
                .limit(50).stream()
            
            # Count only active (deletedAt is None)
            count = 0
            for d in active_sessions_query:
                if d.to_dict().get("deletedAt") is None:
                    count += 1
            active_session_count = count
        except Exception as e:
            logger.warning(f"Error counting active sessions: {e}")
            active_session_count = 0
    
    return MeResponse(
        id=current_user.uid,
        uid=current_user.uid,
        displayName=data.get("displayName"),
        username=data.get("username"),
        hasUsername=bool(data.get("username")),
        email=data.get("email"),
        photoUrl=data.get("photoUrl"),
        providers=data.get("providers", []),
        provider=data.get("provider"),
        allowSearch=data.get("allowSearch", True),
        shareCode=data.get("shareCode"),
        isShareable=is_shareable,
        plan=data.get("plan", "free"),
        createdAt=data.get("createdAt"),
        securityState=data.get("securityState", "normal"),
        riskScore=data.get("riskScore", 0),
        freeCloudCreditsRemaining=data.get("freeCloudCreditsRemaining", 1) if data.get("plan", "free") == "free" else None,
        freeSummaryCreditsRemaining=data.get("freeSummaryCreditsRemaining", 1) if data.get("plan", "free") == "free" else None,
        freeQuizCreditsRemaining=data.get("freeQuizCreditsRemaining", 1) if data.get("plan", "free") == "free" else None,
        activeSessionCount=active_session_count
    )

@router.get("/me/entitlement", response_model=EntitlementResponse)
async def get_my_entitlement(current_user: User = Depends(get_current_user)):
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
async def claim_username(req: ClaimUsernameRequest, current_user: User = Depends(get_current_user)):
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
async def users_lookup(uids: str = Query(..., description="Comma separated uids"), current_user: User = Depends(get_current_user)):
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


@router.post("/me/subscription")
@router.post("/me/subscription/ios")
async def verify_ios_subscription(
    req: SubscriptionVerifyRequest,
    current_user: User = Depends(get_current_user)
):
    """
    iOSクライアントからのStoreKit 2購入状態を受け取り、プランを更新する。
    1. App Store Server API (JWS) 検証 (Robust)
    2. クライアント信頼 (Fallback/MVP)
    """
    try:
        from app.services.apple import apple_service
        from starlette.concurrency import run_in_threadpool
        from datetime import timedelta

        doc_ref = db.collection("users").document(current_user.uid)
        sub_data = req.dict()
        new_plan = "free"
        verified = False
        

        
        # [Robust Verification]
        if apple_service.client or apple_service.verifier:
             transaction_info = None
             if req.signedTransactionInfo:
                 transaction_info = await run_in_threadpool(apple_service.verify_jws, req.signedTransactionInfo)
             elif req.transactionId:
                 transaction_info = await run_in_threadpool(apple_service.get_transaction_info, req.transactionId)
                 
             if transaction_info:
                 verified = True
                 verified_pid = transaction_info.get("productId", "")
                 # Logic: Check expiration?
                 # For now, trust the presence and minimal expiry check if available
                 expires_date_ms = transaction_info.get("expiresDate")
                 if expires_date_ms:
                     # Check if expired
                     if int(expires_date_ms) < int(datetime.now(timezone.utc).timestamp() * 1000):
                         new_plan = "free" # Expired
                     else:
                         # Active
                         if "premium" in verified_pid:
                             new_plan = "pro"
                         elif "standard" in verified_pid:
                             new_plan = "basic"
                         else:
                             new_plan = "pro" # Fallback/Default
                 else:
                     # Lifetime or Unknown (treat as active if verified)
                     if "premium" in verified_pid:
                         new_plan = "pro"
                     elif "standard" in verified_pid:
                         new_plan = "basic"
                     else:
                         new_plan = "pro"
                         
                 # Merge verified info into sub_data
                 sub_data.update(transaction_info)
                 sub_data["verificationSource"] = "server_api"
             else:
                 # Verification failed (Invalid JWS or API Error)
                 # If keys are present but verification fails, do we fallback?
                 # Safer to stay free if verification fails, UNLESS it's a test environment.
                 # Logic: If client says subscribed but verification fails -> suspicious.
                 logger.warning(f"Subscription verification failed for uid={current_user.uid}")
                 # Fallback logic below if needed, or stick to 'free'.
        
        # [MVP / Fallback / Test Mode]
        if not verified:
            # If server side verification was skipped (no keys) or failed,
            # Check if we should fallback to client trust (Only if keys MISSING or Explicit Test Mode)
            # For now, preserve old logic as fallback if verification didn't happen
            if not apple_service.client and req.isSubscribed:
                new_plan = "free" # Default
                pid = req.productId or ""
                if "premium" in pid:
                    new_plan = "pro"
                elif "standard" in pid:
                    new_plan = "basic"
                else:
                    new_plan = "pro" # Old fallback
                sub_data["verificationSource"] = "client_trust"

        # Store subscription history/detail
        sub_data["updatedAt"] = datetime.now(timezone.utc)
        
        # Write to sub-collection for audit
        db.collection("users").document(current_user.uid).collection("subscriptions").document("apple").set(sub_data, merge=True)
        
        # Update user plan
        doc_ref.update({
            "plan": new_plan,
            "subscriptionPlatform": "ios",
            "planUpdatedAt": datetime.now(timezone.utc)
        })
        
        return {"status": "success", "plan": new_plan}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Subscription Sync Failed: {e}")
        # Return 400 with detail as requested
        raise HTTPException(status_code=400, detail=f"subscription sync failed: {type(e).__name__}: {e}")


@router.get("/me/capabilities", response_model=CapabilitiesResponse)
async def get_my_capabilities(current_user: User = Depends(get_current_user)):
    """
    プランに応じた機能フラグ・制限情報を返す。
    クライアントはこれを信じてUIを制御する。
    """
    from datetime import date, timedelta
    from app.services.usage import usage_logger
    
    doc_ref = db.collection("users").document(current_user.uid)
    doc = doc_ref.get()
    if not doc.exists:
        plan = "free"
    else:
        plan = doc.to_dict().get("plan", "free")
    
    # Get this month's usage for the current user
    today = date.today()
    first_day = today.replace(day=1)
    usage_summary = await usage_logger.get_user_usage_summary(
        user_id=current_user.uid,
        from_date=first_day.isoformat(),
        to_date=today.isoformat()
    )
    
    # Convert seconds to minutes
    used_recording_min = int(usage_summary.get("total_recording_sec", 0) / 60)
        
    # Plan Definitions (Backend Source of Truth)
    if plan == "pro":
        limit_min = 60000  # Unlimited-ish (1000h)
        caps = CapabilitiesResponse(
            plan="pro",
            canRealtimeTranslate=True,
            sttPostEngine="whisper_large_v3",
            monthlyRecordingLimitMin=limit_min,
            remainingRecordingMin=max(0, limit_min - used_recording_min),
            canRegenerateTranscript=True,
            maxSessions=None, # Unlimited
            maxSummaries=None,
            maxQuizzes=None
        )
    elif plan == "basic":
        limit_min = 600  # 10 hours
        caps = CapabilitiesResponse(
            plan="basic",
            canRealtimeTranslate=False,
            sttPostEngine="gcp_speech",
            monthlyRecordingLimitMin=limit_min,
            remainingRecordingMin=max(0, limit_min - used_recording_min),
            canRegenerateTranscript=False,
            maxSessions=20,
            maxSummaries=20,
            maxQuizzes=10
        )
    else:  # free
        limit_min = 60  # 1 hour
        credits = doc.to_dict().get("freeCloudCreditsRemaining", 1) if doc.exists else 1
        
        # Free users can use Cloud STT, Summary, and Quiz but only once (Lifetime credit)
        # We show 1 if they have credit, 0 if used.
        caps = CapabilitiesResponse(
            plan="free",
            canRealtimeTranslate=False,
            sttPostEngine="gcp_speech", # Always allowed until 403 at start (streaming/batch)
            monthlyRecordingLimitMin=limit_min,
            remainingRecordingMin=max(0, limit_min - used_recording_min),
            canRegenerateTranscript=False,
            # [Free 1 credit]
            maxSessions=1, 
            maxSummaries=1 if credits > 0 else 0,
            maxQuizzes=1 if credits > 0 else 0
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
    current_user: User = Depends(get_current_user)
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
    current_user: User = Depends(get_current_user)
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
async def get_profile(current_user: User = Depends(get_current_user)):
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
    current_user: User = Depends(get_current_user)
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
async def create_or_refresh_share_code(current_user: User = Depends(get_current_user)):
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
async def search_by_share_code(code: str, current_user: User = Depends(get_current_user)):
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
