from fastapi import APIRouter, HTTPException
import requests
import os
import logging
from firebase_admin import auth as fb_auth

from app.util_models import LineAuthRequest, LineAuthResponse

router = APIRouter()
logger = logging.getLogger("app.auth")

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

