from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import requests
import os
import logging
from firebase_admin import auth as fb_auth

from app.util_models import LineAuthRequest, LineAuthResponse
from app.dependencies import get_current_user, CurrentUser
from app.firebase import db
from google.cloud import firestore

router = APIRouter()
logger = logging.getLogger("app.auth")


class CanonicalizeResponse(BaseModel):
    """Response for /auth/canonicalize endpoint."""
    canonicalized: bool
    accountId: str
    firebaseCustomToken: str | None = None
    message: str

import httpx

@router.post("/auth/line", response_model=LineAuthResponse)
async def auth_line(req: LineAuthRequest):
    LINE_CLIENT_ID = os.environ.get("LINE_CHANNEL_ID")
    logger.info(f"[/auth/line] Configured LINE_CHANNEL_ID: {LINE_CLIENT_ID}") 
    
    if not LINE_CLIENT_ID:
        logger.warning("LINE_CHANNEL_ID is not set in environment")
        return JSONResponse(status_code=500, content={"detail": "Server misconfiguration: missing LINE_CHANNEL_ID"})

    # DEBUG: Decode token to see what client sent
    try:
        import jwt
        unverified_payload = jwt.decode(req.idToken, options={"verify_signature": False})
        logger.info(f"[/auth/line] Incoming Token Claims: aud={unverified_payload.get('aud')}, iss={unverified_payload.get('iss')}, exp={unverified_payload.get('exp')}")
    except Exception as decode_err:
        logger.error(f"[/auth/line] Failed to decode token for debug: {decode_err}")

    logger.info(f"[/auth/line] Verifying LINE token with ID: {LINE_CLIENT_ID}")
    
    async with httpx.AsyncClient() as client:
        try:
            verify_resp = await client.post(
                "https://api.line.me/oauth2/v2.1/verify",
                data={
                    "id_token": req.idToken,
                    "client_id": LINE_CLIENT_ID,
                    "nonce": req.nonce,
                },
                timeout=5.0
            )
        except httpx.TimeoutException:
            logger.error("LINE token verification timed out")
            raise HTTPException(status_code=503, detail="LINE server timeout")
    
    if verify_resp.status_code != 200:
        logger.error(f"LINE verify failed: status={verify_resp.status_code}, body={verify_resp.text}")
        raise HTTPException(status_code=401, detail=f"Invalid LINE token. Server expects aud={LINE_CLIENT_ID}. LINE Error: {verify_resp.text}")

    payload = verify_resp.json()
    line_user_id = payload.get("sub")
    name = payload.get("name")
    picture = payload.get("picture")
    
    logger.info(f"[/auth/line] LINE user verified: sub={line_user_id}, name={name}")
    
    if not line_user_id:
        raise HTTPException(status_code=401, detail="No sub in LINE token")

    # 2) Firebase Custom Token を発行
    firebase_uid = f"line:{line_user_id}"
    
    try:
        custom_token_bytes = fb_auth.create_custom_token(
            firebase_uid,
            {
                "provider": "line",
                "name": name,
                "picture": picture,
            }
        )
        custom_token = custom_token_bytes.decode("utf-8")
        logger.info(f"[/auth/line] Custom token created for uid={firebase_uid}")
    except Exception as e:
        logger.exception(f"Failed to create custom token for uid={firebase_uid}")
        raise HTTPException(status_code=500, detail="Failed to create custom token")

    return LineAuthResponse(firebaseCustomToken=custom_token)


from datetime import datetime, timezone


def _merge_uid_into_account_sync(uid: str, target_account_id: str, source_account_id: str | None = None) -> dict:
    """
    [Account Unification] Merge a uid into target_account_id (non-transactional version for auth.py).
    """
    now = datetime.now(timezone.utc)

    # 1. Update uid_links to point to target account
    db.collection("uid_links").document(uid).set({
        "uid": uid,
        "accountId": target_account_id,
        "linkedAt": now,
        "mergedFrom": source_account_id,
        "mergeReason": "canonicalize_token_match"
    }, merge=True)

    # 2. Add uid to target account's memberUids
    target_acc_ref = db.collection("accounts").document(target_account_id)
    target_acc_snap = target_acc_ref.get()
    if target_acc_snap.exists:
        target_data = target_acc_snap.to_dict() or {}
        member_uids = set(target_data.get("memberUids", []))
        member_uids.add(uid)
        target_acc_ref.update({
            "memberUids": list(member_uids),
            "updatedAt": now
        })
    else:
        target_acc_ref.set({
            "memberUids": [uid],
            "primaryUid": uid,
            "plan": "free",
            "createdAt": now,
            "updatedAt": now
        })

    # 3. Remove uid from source account's memberUids (if different)
    if source_account_id and source_account_id != target_account_id:
        source_acc_ref = db.collection("accounts").document(source_account_id)
        source_acc_snap = source_acc_ref.get()
        if source_acc_snap.exists:
            source_data = source_acc_snap.to_dict() or {}
            source_members = [m for m in source_data.get("memberUids", []) if m != uid]
            if len(source_members) == 0:
                source_acc_ref.update({
                    "memberUids": [],
                    "mergedInto": target_account_id,
                    "mergedAt": now,
                    "updatedAt": now
                })
            else:
                source_acc_ref.update({
                    "memberUids": source_members,
                    "updatedAt": now
                })

    # 4. Update users/{uid}.accountId
    db.collection("users").document(uid).set({
        "accountId": target_account_id,
        "updatedAt": now
    }, merge=True)

    return {"changed": True, "from": source_account_id, "to": target_account_id}


@router.post("/auth/canonicalize", response_model=CanonicalizeResponse)
async def canonicalize_user(
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    [Account Unification] Canonicalize user identity.

    Resolution priority:
    1. appAccountToken -> apple_app_account_tokens/{token}.accountId (strongest)
    2. phone_number -> phone_numbers/{phone}.accountId
    3. Existing uid_links/{uid}.accountId

    If a different accountId is found, merges the current uid into that account.
    """
    uid = current_user.uid
    token_phone = current_user.phone_number
    now = datetime.now(timezone.utc)

    # Get current user data
    user_doc = db.collection("users").document(uid).get()
    user_data = user_doc.to_dict() if user_doc.exists else {}

    # Get current accountId from uid_links
    link_doc = db.collection("uid_links").document(uid).get()
    link_data = link_doc.to_dict() if link_doc.exists else {}
    current_account_id = link_data.get("accountId") or user_data.get("accountId")

    target_account_id = None
    resolution_method = None

    # Priority 1: appAccountToken lookup (strongest - same device = same person)
    app_token = user_data.get("appleAppAccountToken")
    if app_token:
        token_doc = db.collection("apple_app_account_tokens").document(app_token).get()
        if token_doc.exists:
            token_data = token_doc.to_dict() or {}
            mapped_account_id = token_data.get("accountId")
            if mapped_account_id:
                target_account_id = mapped_account_id
                resolution_method = "app_account_token"
                logger.info(f"[/auth/canonicalize] Found account {target_account_id} by appAccountToken")

    # Priority 2: phone number lookup
    if not target_account_id and token_phone:
        phone_doc = db.collection("phone_numbers").document(token_phone).get()
        if phone_doc.exists:
            phone_data = phone_doc.to_dict() or {}
            mapped_account_id = phone_data.get("accountId")
            if mapped_account_id:
                target_account_id = mapped_account_id
                resolution_method = "phone_number"
                logger.info(f"[/auth/canonicalize] Found account {target_account_id} by phone {token_phone}")

    # Priority 3: Existing link
    if not target_account_id and current_account_id:
        target_account_id = current_account_id
        resolution_method = "existing_link"

    # No account found anywhere
    if not target_account_id:
        logger.info(f"[/auth/canonicalize] No account found for uid={uid}, creating new")
        # Create new account for this user
        new_acc_ref = db.collection("accounts").document()
        target_account_id = new_acc_ref.id
        new_acc_ref.set({
            "primaryUid": uid,
            "memberUids": [uid],
            "plan": "free",
            "createdAt": now,
            "updatedAt": now
        })
        db.collection("uid_links").document(uid).set({
            "uid": uid,
            "accountId": target_account_id,
            "linkedAt": now,
            "reason": "canonicalize_created"
        })
        db.collection("users").document(uid).set({
            "accountId": target_account_id,
            "updatedAt": now
        }, merge=True)

        return CanonicalizeResponse(
            canonicalized=True,
            accountId=target_account_id,
            firebaseCustomToken=None,
            message="New account created and linked."
        )

    # Check if merge is needed
    if current_account_id and current_account_id != target_account_id:
        # Merge current uid into target account
        logger.info(f"[/auth/canonicalize] Merging uid={uid} from {current_account_id} to {target_account_id} via {resolution_method}")
        try:
            _merge_uid_into_account_sync(uid, target_account_id, current_account_id)
        except Exception as e:
            logger.error(f"[/auth/canonicalize] Merge failed: {e}")
            raise HTTPException(status_code=500, detail=f"Account merge failed: {str(e)}")

        return CanonicalizeResponse(
            canonicalized=True,
            accountId=target_account_id,
            firebaseCustomToken=None,
            message=f"Account unified via {resolution_method}. Previous: {current_account_id}"
        )

    # No merge needed - just ensure link exists
    if not link_doc.exists:
        db.collection("uid_links").document(uid).set({
            "uid": uid,
            "accountId": target_account_id,
            "linkedAt": now,
            "reason": f"canonicalize_{resolution_method}"
        })
        db.collection("users").document(uid).set({
            "accountId": target_account_id,
            "updatedAt": now
        }, merge=True)
        return CanonicalizeResponse(
            canonicalized=True,
            accountId=target_account_id,
            firebaseCustomToken=None,
            message=f"Account linked via {resolution_method}."
        )

    # Already canonical
    logger.info(f"[/auth/canonicalize] uid={uid} already linked to {target_account_id}")
    return CanonicalizeResponse(
        canonicalized=False,
        accountId=target_account_id,
        firebaseCustomToken=None,
        message="Already using canonical identity."
    )
