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
import logging

logger = logging.getLogger("app.dependencies")

@dataclass
class CurrentUser:
    uid: str
    account_id: str  # [CRITICAL] Always resolved - this is the canonical identity
    provider: str | None
    phone_number: str | None
    email: str | None
    display_name: str | None = None
    photo_url: str | None = None


def _resolve_account_id_for_uid(uid: str, phone_number: str | None = None) -> str:
    """
    [Account Architecture] Resolve uid -> accountId.

    Resolution priority:
    1. uid_links/{uid}.accountId (existing link)
    2. phone_numbers/{phone}.accountId (if phone in token)
    3. users/{uid}.accountId (legacy fallback)
    4. Create new account (last resort)

    This function ALWAYS returns an accountId and ensures uid_links is set.
    """
    now = datetime.now(timezone.utc)

    # 1. Check uid_links (primary source of truth)
    link_ref = db.collection("uid_links").document(uid)
    link_doc = link_ref.get()
    if link_doc.exists:
        account_id = link_doc.to_dict().get("accountId")
        if account_id:
            return account_id

    # 2. Check phone_numbers index (for cross-provider unification)
    if phone_number:
        phone_ref = db.collection("phone_numbers").document(phone_number)
        phone_doc = phone_ref.get()
        if phone_doc.exists:
            account_id = phone_doc.to_dict().get("accountId")
            if account_id:
                # Link this uid to the phone's account
                link_ref.set({
                    "uid": uid,
                    "accountId": account_id,
                    "linkedAt": now,
                    "linkedVia": "phone_number_match"
                }, merge=True)
                logger.info(f"[resolve_account] Linked uid={uid} to account={account_id} via phone")
                return account_id

    # 3. Check users/{uid} (legacy)
    user_ref = db.collection("users").document(uid)
    user_doc = user_ref.get()
    if user_doc.exists:
        account_id = user_doc.to_dict().get("accountId")
        if account_id:
            # Repair: ensure uid_links exists
            link_ref.set({
                "uid": uid,
                "accountId": account_id,
                "linkedAt": now,
                "linkedVia": "legacy_repair"
            }, merge=True)
            logger.info(f"[resolve_account] Repaired uid_links for uid={uid} -> account={account_id}")
            return account_id

    # 4. Create new account (no existing link found)
    new_acc_ref = db.collection("accounts").document()
    account_id = new_acc_ref.id

    new_acc_ref.set({
        "primaryUid": uid,
        "memberUids": [uid],
        "plan": "free",
        "createdAt": now,
        "updatedAt": now
    })

    link_ref.set({
        "uid": uid,
        "accountId": account_id,
        "linkedAt": now,
        "linkedVia": "auto_create"
    })

    # Also update users doc
    user_ref.set({
        "accountId": account_id,
        "updatedAt": now
    }, merge=True)

    logger.info(f"[resolve_account] Created new account={account_id} for uid={uid}")
    return account_id

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

        # [CRITICAL] Always resolve accountId - this is the canonical identity
        account_id = _resolve_account_id_for_uid(uid, phone_number)

        return CurrentUser(
            uid=uid,
            account_id=account_id,
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
    except HTTPException:
        raise
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

from typing import Union

def ensure_can_view(session_data: dict, user_or_uid: Union[str, CurrentUser], session_id: Optional[str] = None):
    """
    [Account Architecture] Check if user can view a session.

    Authorization is based on accountId ONLY (no uid fallback).
    Sessions without ownerAccountId require migration.
    """
    # Normalize input
    if isinstance(user_or_uid, CurrentUser):
        uid = user_or_uid.uid
        account_id = user_or_uid.account_id
    else:
        # Legacy: if raw uid string is passed, resolve account_id
        uid = user_or_uid
        account_id = _resolve_account_id_for_uid(uid)

    owner_account_id = session_data.get("ownerAccountId")

    # [CRITICAL] Require ownerAccountId - sessions without it need migration
    if not owner_account_id:
        # Temporary compatibility: check ownerUid match during migration period
        owner_uid = session_data.get("ownerUid") or session_data.get("ownerUserId")
        if owner_uid == uid:
            logger.warning(f"[ensure_can_view] Session missing ownerAccountId, uid match used (migration needed)")
            return
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Session requires migration (missing ownerAccountId)"
        )

    # 1. Account Match (Primary check)
    if owner_account_id == account_id:
        return

    # 2. Shared access (via accountId or legacy uid)
    shared_account_ids = session_data.get("sharedWithAccountIds") or []
    if account_id in shared_account_ids:
        return

    # Legacy shared access (uid-based) - to be deprecated
    shared_users = session_data.get("sharedUserIds") or session_data.get("sharedWithUserIds") or []
    shared_map = session_data.get("sharedWith") or {}
    if uid in shared_users or shared_map.get(uid):
        return

    # 3. Session member check
    if session_id:
        member = _get_session_member(session_id, uid)
        if member:
            return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have access to this session"
    )


def ensure_is_owner(session_data: dict, user_or_uid: Union[str, CurrentUser], session_id: Optional[str] = None):
    """
    [Account Architecture] Check if user is the owner of a session.

    Authorization is based on accountId ONLY (no uid fallback).
    Sessions without ownerAccountId require migration.
    """
    # Normalize input
    if isinstance(user_or_uid, CurrentUser):
        uid = user_or_uid.uid
        account_id = user_or_uid.account_id
    else:
        # Legacy: if raw uid string is passed, resolve account_id
        uid = user_or_uid
        account_id = _resolve_account_id_for_uid(uid)

    owner_account_id = session_data.get("ownerAccountId")

    # [CRITICAL] Require ownerAccountId - sessions without it need migration
    if not owner_account_id:
        # Temporary compatibility: check ownerUid match during migration period
        owner_uid = session_data.get("ownerUid") or session_data.get("ownerUserId")
        if owner_uid == uid:
            logger.warning(f"[ensure_is_owner] Session missing ownerAccountId, uid match used (migration needed)")
            return
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Session requires migration (missing ownerAccountId)"
        )

    # Account Match (Primary check)
    if owner_account_id == account_id:
        return

    # Session member with owner role
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
        account_id=user.account_id,
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
        account_id=user.account_id,
        provider=user.provider,
        phone_number=user.phone_number,
        email=user.email,
        display_name=user.display_name,
        photo_url=user.photo_url,
        is_super_admin=is_super_admin
    )
