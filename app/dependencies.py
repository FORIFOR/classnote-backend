from fastapi import Depends, HTTPException, status, Header, BackgroundTasks
from typing import Optional
from fastapi.security import OAuth2PasswordBearer
import firebase_admin
from firebase_admin import auth
from google.cloud import firestore
from app.firebase import db
from app.services.account_deletion import LOCKS_COLLECTION, deletion_lock_id
import time
from datetime import datetime, timezone

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)

if not firebase_admin._apps:
    firebase_admin.initialize_app()

# Simple in-memory cache for throttling activity updates (per instance)
# Key: uid, Value: timestamp (seconds)
USER_ACTIVITY_CACHE = {}
USER_ACTIVITY_THROTTLE_SEC = 300  # 5 minutes

class User:
    def __init__(self, uid: str, email: str = None, display_name: str = None, photo_url: str = None):
        self.uid = uid
        self.email = email
        self.display_name = display_name
        self.photo_url = photo_url


def _normalize_ts(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _update_last_seen(uid: str):
    """Background task to update lastSeenAt"""
    try:
        db.collection("users").document(uid).set({
            "lastSeenAt": firestore.SERVER_TIMESTAMP
        }, merge=True)
    except Exception as e:
        print(f"Error updating lastSeenAt for {uid}: {e}")

def _track_activity(uid: str, background_tasks: BackgroundTasks):
    """Check throttle and schedule background update if needed."""
    now = time.time()
    last_update = USER_ACTIVITY_CACHE.get(uid, 0)
    
    if now - last_update > USER_ACTIVITY_THROTTLE_SEC:
        USER_ACTIVITY_CACHE[uid] = now
        background_tasks.add_task(_update_last_seen, uid)


def _resolve_user_from_token(token: str) -> User:
    try:
        decoded_token = auth.verify_id_token(token)
        uid = decoded_token.get("uid")
        email = decoded_token.get("email")
        name = decoded_token.get("name")
        picture = decoded_token.get("picture")
        provider_id = None
        try:
            provider_id = decoded_token.get("firebase", {}).get("sign_in_provider")
        except Exception:
            provider_id = None
        default_name = name or "ゲスト"

        if not uid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: No UID",
                headers={"WWW-Authenticate": "Bearer"},
            )

        user_ref = db.collection("users").document(uid)
        user_doc = user_ref.get()

        # プロバイダー情報は Auth から取得
        providers = []
        photo_url = picture
        try:
            record = auth.get_user(uid)
            providers = [p.provider_id for p in record.provider_data] if record.provider_data else []
            photo_url = photo_url or record.photo_url
            # email が空なら Auth の値を使う
            if not email and record.email:
                email = record.email
            if not name and record.display_name:
                name = record.display_name
            if not provider_id and record.provider_id:
                provider_id = record.provider_id
        except Exception as e:
            # print(f"Auth get_user error: {e}") # Suppress noise
            pass

        email_lower = email.lower() if email else None
        if not provider_id and providers:
            provider_id = providers[0]

        if not user_doc.exists and email_lower and provider_id:
            lock_ref = db.collection(LOCKS_COLLECTION).document(deletion_lock_id(email_lower, provider_id))
            lock_doc = lock_ref.get()
            if lock_doc.exists:
                lock_data = lock_doc.to_dict() or {}
                delete_after = _normalize_ts(lock_data.get("deleteAfterAt"))
                now_dt = datetime.now(timezone.utc)
                if not delete_after or delete_after > now_dt:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="account_deletion_pending",
                    )
                lock_ref.delete()

        now_ts = firestore.SERVER_TIMESTAMP
        if not user_doc.exists:
            user_data = {
                "email": email,
                "emailLower": email_lower,
                "displayName": default_name,
                "photoUrl": photo_url,
                "providers": providers,
                "provider": provider_id,
                "allowSearch": True,
                "isShareable": True,
                "shareCodeSearchEnabled": True,
                "createdAt": now_ts,
                "updatedAt": now_ts,
            }
            user_data = {k: v for k, v in user_data.items() if v is not None}
            user_ref.set(user_data)
        else:
            existing = user_doc.to_dict() or {}
            update_data = {}
            # 既存ユーザーでも allowSearch/isShareable が無い場合は true に初期化
            if existing.get("allowSearch") is None:
                update_data["allowSearch"] = True
            if existing.get("isShareable") is None:
                update_data["isShareable"] = True
            if existing.get("shareCodeSearchEnabled") is None:
                update_data["shareCodeSearchEnabled"] = True
            if email and existing.get("email") != email:
                update_data["email"] = email
                update_data["emailLower"] = email.lower()
            if default_name and not existing.get("displayName"):
                update_data["displayName"] = default_name
            if photo_url and not existing.get("photoUrl"):
                update_data["photoUrl"] = photo_url
            if providers and not existing.get("providers"):
                update_data["providers"] = providers
            if update_data:
                update_data["updatedAt"] = now_ts
                user_ref.update(update_data)

        return User(uid=uid, email=email, display_name=name, photo_url=photo_url)

    except HTTPException:
        raise
    except Exception as e:
        print(f"Auth Error: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_current_user(
    background_tasks: BackgroundTasks,
    token: Optional[str] = Depends(oauth2_scheme)
) -> User:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = _resolve_user_from_token(token)
    _track_activity(user.uid, background_tasks)
    return user


def get_user_from_token(token: str) -> User:
    return _resolve_user_from_token(token)


async def get_current_user_optional(
    background_tasks: BackgroundTasks,
    authorization: Optional[str] = Header(None)
) -> Optional[User]:
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
        _track_activity(user.uid, background_tasks)
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

def ensure_can_view(session_data: dict, uid: str, session_id: Optional[str] = None):
    owner = session_data.get("ownerUid") or session_data.get("ownerUserId") or session_data.get("ownerId") or session_data.get("userId")
    shared_users = session_data.get("sharedUserIds") or session_data.get("sharedWithUserIds") or []
    shared_map = session_data.get("sharedWith") or {}

    if owner == uid:
        return
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

def ensure_is_owner(session_data: dict, uid: str, session_id: Optional[str] = None):
    owner = session_data.get("ownerUid") or session_data.get("ownerUserId") or session_data.get("ownerId") or session_data.get("userId")
    if owner != uid:
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


class AdminUser(User):
    """管理者ユーザー"""
    def __init__(self, uid: str, email: str = None, display_name: str = None, is_super_admin: bool = False):
        super().__init__(uid, email, display_name)
        self.is_super_admin = is_super_admin


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

    _track_activity(user.uid, background_tasks)

    return AdminUser(
        uid=user.uid,
        email=user.email,
        display_name=user.display_name,
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

    _track_activity(user.uid, background_tasks)

    return AdminUser(
        uid=user.uid,
        email=user.email,
        display_name=user.display_name,
        is_super_admin=is_super_admin
    )
