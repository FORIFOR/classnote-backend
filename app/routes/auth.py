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


@router.post("/auth/canonicalize", response_model=CanonicalizeResponse)
async def canonicalize_user(
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    [Account Unification] Canonicalize user identity.

    If the current Firebase UID differs from the canonical accountId,
    this endpoint returns a new Custom Token that the client can use
    to re-authenticate with the canonical identity.

    This handles cases like:
    - LINE login users (uid=line:xxx) who have an existing account
    - Users who need to migrate to a unified accountId
    """
    uid = current_user.uid

    # 1. Look up uid_links to get accountId
    link_doc = db.collection("uid_links").document(uid).get()

    if not link_doc.exists:
        logger.warning(f"[/auth/canonicalize] No uid_link found for uid={uid}")
        raise HTTPException(
            status_code=404,
            detail="No account link found. Phone verification may be required."
        )

    link_data = link_doc.to_dict() or {}
    account_id = link_data.get("accountId")

    if not account_id:
        logger.error(f"[/auth/canonicalize] uid_link exists but no accountId for uid={uid}")
        raise HTTPException(
            status_code=500,
            detail="Account data integrity error."
        )

    # 2. Check if canonicalization is needed
    if uid == account_id:
        # Already canonical - no migration needed
        logger.info(f"[/auth/canonicalize] uid={uid} is already canonical")
        return CanonicalizeResponse(
            canonicalized=False,
            accountId=account_id,
            firebaseCustomToken=None,
            message="Already using canonical identity."
        )

    # 3. Need to migrate - fetch account data for Custom Token claims
    account_doc = db.collection("accounts").document(account_id).get()
    account_data = account_doc.to_dict() if account_doc.exists else {}

    # Build claims from account data
    claims = {
        "provider": "canonicalized",
        "originalUid": uid,
    }

    # Add optional claims
    if account_data.get("displayName"):
        claims["name"] = account_data["displayName"]
    if account_data.get("email"):
        claims["email"] = account_data["email"]
    if account_data.get("phoneE164"):
        claims["phone_number"] = account_data["phoneE164"]

    # 4. Create Custom Token with accountId as the UID
    try:
        custom_token_bytes = fb_auth.create_custom_token(
            account_id,
            claims
        )
        custom_token = custom_token_bytes.decode("utf-8")
        logger.info(f"[/auth/canonicalize] Created canonical token: {uid} -> {account_id}")
    except Exception as e:
        logger.exception(f"[/auth/canonicalize] Failed to create token for {account_id}")
        raise HTTPException(
            status_code=500,
            detail="Failed to create canonical token."
        )

    return CanonicalizeResponse(
        canonicalized=True,
        accountId=account_id,
        firebaseCustomToken=custom_token,
        message=f"Re-authenticate with this token to use canonical identity."
    )
