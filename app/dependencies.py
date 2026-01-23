from fastapi import Depends, HTTPException, status, Header, BackgroundTasks, Request
from typing import Optional
from fastapi.security import OAuth2PasswordBearer
import firebase_admin
from firebase_admin import auth
from google.cloud import firestore
from app.firebase import db
from app.services.account_deletion import LOCKS_COLLECTION, deletion_lock_id
import time
from datetime import datetime, timezone, timedelta
import datetime as dt_module # Fallback
try:
    # Try public API first (recommended for newer SDKs)
    from firebase_admin.auth import InvalidIdTokenError, ExpiredIdTokenError, RevokedIdTokenError, CertificateFetchError
except ImportError:
    try:
        # Fallback to internal utils (older SDKs)
        from firebase_admin._auth_utils import InvalidIdTokenError, ExpiredIdTokenError, RevokedIdTokenError, CertificateFetchError
    except ImportError:
        # Final fallback: generic placeholders to allow app startup
        # The logic will simply catch general Exceptions if these match nothing
        class InvalidIdTokenError(Exception): pass
        class ExpiredIdTokenError(Exception): pass
        class RevokedIdTokenError(Exception): pass
        class CertificateFetchError(Exception): pass

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)

if not firebase_admin._apps:
    firebase_admin.initialize_app()

# Simple in-memory cache for throttling activity updates (per instance)
# Key: uid, Value: timestamp (seconds)
USER_ACTIVITY_CACHE = {}
USER_ACTIVITY_THROTTLE_SEC = 300  # 5 minutes


from dataclasses import dataclass

@dataclass
class CurrentUser:
    uid: str
    provider: str | None
    phone_number: str | None
    email: str | None
    display_name: str | None = None
    photo_url: str | None = None
    # Plan is now on Account, not User, but kept for compat if needed or removed. 
    # Logic will fetch Plan from Account.

def _resolve_user_from_token(token: str) -> CurrentUser:
    try:
        decoded_token = auth.verify_id_token(token, check_revoked=False)
        uid = decoded_token.get("uid")
        email = decoded_token.get("email")
        # Firebase Auth phone_number is top-level claim
        phone_number = decoded_token.get("phone_number")
        
        firebase_claims = decoded_token.get("firebase", {})
        provider_id = firebase_claims.get("sign_in_provider")
        
        if not uid:
             raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: No UID",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return CurrentUser(
            uid=uid,
            provider=provider_id,
            phone_number=phone_number,
            email=email,
            display_name=decoded_token.get("name"),
            photo_url=decoded_token.get("picture")
        )

    except (InvalidIdTokenError, ExpiredIdTokenError, RevokedIdTokenError) as e:
        print(f"Auth Token Error: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception as e:
        print(f"Auth System Error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Auth system error",
        )



async def get_current_user(
    request: Request,
    background_tasks: BackgroundTasks,
    token: Optional[str] = Depends(oauth2_scheme)
) -> CurrentUser:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = _resolve_user_from_token(token)
    # [NEW] Inject UID into request state for OpsLogger
    request.state.uid = user.uid
    request.state.email = user.email
    request.state.email = user.email
    # _track_activity(user.uid, background_tasks)
    return user


async def get_verified_user(
    request: Request,
    background_tasks: BackgroundTasks,
    currentUser: CurrentUser = Depends(get_current_user)
) -> CurrentUser:
    """
    Dependency to enforce phone verification.
    If the user has not verified their phone number (and needs to), this raises 403.
    """
    
    # 1. Check Link State (Same logic as /users/me to determine needsPhoneVerification)
    # We can rely on the fact that if they are linked to an account, they are verified.
    # However, get_current_user only resolves the token. We need to check the link doc.
    
    uid = currentUser.uid
    
    # Optimization: If the token has the custom claim "verified", we could trust it.
    # But for now, let's check the DB or use a cached approach if clear.
    # Actually, let's just do the DB check. usage of this endpoint implies a "write" or "critical" action usually.

    link_ref = db.collection("uid_links").document(uid)
    link_doc = link_ref.get()
    
    # If not linked -> Unverified
    if not link_doc.exists:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "PHONE_VERIFICATION_REQUIRED", "message": "Phone verification required to perform this action."}
        )
        
    # If linked but strictly check? 
    # Usually existence of uid_links means phone verification passed.
    
    return currentUser


def get_user_from_token(token: str) -> CurrentUser:
    return _resolve_user_from_token(token)


async def get_current_user_optional(
    background_tasks: BackgroundTasks,
    authorization: Optional[str] = Header(None)
) -> Optional[CurrentUser]:
    """
    Optional authentication - returns None if no valid token provided.
    Used for endpoints that support both authenticated and unauthenticated access.
    """
    if not authorization:
        return None
    
    # Extract token from "Bearer <token>" format
    if authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    else:
        token = authorization
    
    try:
        user = _resolve_user_from_token(token)
        return user
    except HTTPException:
        return None

# --- Authorization Helpers ---

def _session_member_doc_id(session_id: str, user_id: str) -> str:
    return f"{session_id}_{user_id}"

def _get_session_member(session_id: str | None, user_id: str | None) -> Optional[dict]:
    if not session_id or not user_id:
        return None
    doc = db.collection("session_members").document(_session_member_doc_id(session_id, user_id)).get()
    if not doc.exists:
        return None
    return doc.to_dict() or {}

from app.services.account import account_id_from_phone
from typing import Union

def ensure_can_view(session_data: dict, user_or_uid: Union[str, CurrentUser], session_id: Optional[str] = None):
    # Normalize input
    if isinstance(user_or_uid, CurrentUser):
        uid = user_or_uid.uid
        phone = user_or_uid.phone_number
        provider = user_or_uid.provider
    else:
        uid = user_or_uid
        phone = None
        provider = None

    owner_uid = session_data.get("ownerUid") or session_data.get("ownerUserId") or session_data.get("ownerId") or session_data.get("userId")
    owner_account_id = session_data.get("ownerAccountId")

    # 1. UID Match (Legacy/Fastest)
    if owner_uid == uid:
        return

    # 2. Account Match (New)
    if owner_account_id:
        # A. Check if this UID is explicitly linked (Most reliable)
        link_doc = db.collection("uid_links").document(uid).get()
        if link_doc.exists and link_doc.to_dict().get("accountId") == owner_account_id:
            return
        
        # B. Fallback: Check if the token's phone number matches the account
        #    (Prevents "I just linked but link doc propagation failed" or "uid_links missing")
        #    NOTE: This enables "Unified Identity" - same person, different UIDs.
        if phone:
            derived_acc_id = account_id_from_phone(phone)
            if derived_acc_id == owner_account_id:
                return

    shared_users = session_data.get("sharedUserIds") or session_data.get("sharedWithUserIds") or []
    shared_map = session_data.get("sharedWith") or {}

    if session_id:
        member = _get_session_member(session_id, uid)
        if member:
            return
    if uid in shared_users:
        return
    if shared_map.get(uid):
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have access to this session"
    )

def ensure_is_owner(session_data: dict, user_or_uid: Union[str, CurrentUser], session_id: Optional[str] = None):
    # Normalize input
    if isinstance(user_or_uid, CurrentUser):
        uid = user_or_uid.uid
        phone = user_or_uid.phone_number
    else:
        uid = user_or_uid
        phone = None

    owner_uid = session_data.get("ownerUid") or session_data.get("ownerUserId") or session_data.get("ownerId") or session_data.get("userId")
    owner_account_id = session_data.get("ownerAccountId")
    
    # 1. Legacy Match
    if owner_uid == uid:
        return

    # 2. Account Match
    if owner_account_id:
        # A. Check UID Link
        link_doc = db.collection("uid_links").document(uid).get()
        if link_doc.exists and link_doc.to_dict().get("accountId") == owner_account_id:
            return
            
        # B. Fallback: Phone Match
        if phone:
            derived_acc_id = account_id_from_phone(phone)
            if derived_acc_id == owner_account_id:
                return

    if session_id:
        member = _get_session_member(session_id, uid)
        if member and member.get("role") == "owner":
            return
            
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only the owner can perform this operation"
    )


# --- Admin Authentication ---

# 管理者UIDリスト（環境変数から読み込み、またはハードコード）
import os
ADMIN_UIDS = set(filter(None, (os.environ.get("ADMIN_UIDS") or "").split(",")))


@dataclass
class AdminUser(CurrentUser):
    """管理者ユーザー"""
    is_super_admin: bool = False


def _check_admin_claims(token: str) -> tuple[bool, bool]:
    """
    トークンから管理者権限をチェック。

    Returns:
        tuple[bool, bool]: (is_admin, is_super_admin)
    """
    try:
        decoded_token = auth.verify_id_token(token)
        uid = decoded_token.get("uid")

        # 方法1: Custom Claims をチェック
        is_admin = decoded_token.get("admin", False)
        is_super_admin = decoded_token.get("superAdmin", False)

        # 方法2: ADMIN_UIDS 環境変数でチェック（フォールバック）
        if not is_admin and uid in ADMIN_UIDS:
            is_admin = True

        # 方法3: Firestore users コレクションでチェック（フォールバック）
        if not is_admin:
            user_doc = db.collection("users").document(uid).get()
            if user_doc.exists:
                user_data = user_doc.to_dict() or {}
                is_admin = user_data.get("isAdmin", False) or user_data.get("admin", False)
                is_super_admin = is_super_admin or user_data.get("isSuperAdmin", False)

        return is_admin, is_super_admin
    except Exception:
        return False, False


async def get_admin_user(
    background_tasks: BackgroundTasks,
    token: Optional[str] = Depends(oauth2_scheme)
) -> AdminUser:
    """
    管理者ユーザーを取得。管理者でない場合は403エラー。
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # まず通常のユーザー認証
    user = _resolve_user_from_token(token)

    # 管理者権限チェック
    is_admin, is_super_admin = _check_admin_claims(token)

    if not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )

    return AdminUser(
        uid=user.uid,
        provider=user.provider,
        phone_number=user.phone_number,
        email=user.email,
        display_name=user.display_name,
        photo_url=user.photo_url,
        is_super_admin=is_super_admin
    )


async def get_admin_user_optional(
    background_tasks: BackgroundTasks,
    token: Optional[str] = Depends(oauth2_scheme)
) -> Optional[AdminUser]:
    """
    Optional admin auth. Returns None if no token is provided.
    Raises 401/403 when a token is present but invalid or non-admin.
    """
    if not token:
        return None

    user = _resolve_user_from_token(token)
    is_admin, is_super_admin = _check_admin_claims(token)

    if not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )

    return AdminUser(
        uid=user.uid,
        provider=user.provider,
        phone_number=user.phone_number,
        email=user.email,
        display_name=user.display_name,
        photo_url=user.photo_url,
        is_super_admin=is_super_admin
    )
