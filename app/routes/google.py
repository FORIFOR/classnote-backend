import os
from urllib.parse import urlencode
from datetime import datetime, timezone

import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse

from app.dependencies import get_current_user, get_current_user_optional, CurrentUser, CurrentUser
from app.google_calendar import (
    _sign_state,
    _verify_state,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_REDIRECT_URI,
    GOOGLE_SCOPES,
    save_tokens,
)


router = APIRouter(prefix="/google")


@router.get("/oauth/start")
async def google_oauth_start(
    return_to: str = "/",
    token: str | None = None,
    current_user: CurrentUser | None = Depends(get_current_user_optional)
):
    # Determine UID: priority to `token` query param (for iOS Safari), fallback to Header (for Web)
    uid = None
    
    if token:
        try:
            from firebase_admin import auth
            decoded = auth.verify_id_token(token)
            uid = decoded["uid"]
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid token")
    elif current_user:
        uid = current_user.uid
    
    if not uid:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Google OAuth is not configured")

    state = _sign_state({"uid": uid, "return_to": return_to, "ts": datetime.now(timezone.utc).timestamp()})
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": GOOGLE_SCOPES,
        "access_type": "offline",
        "include_granted_scopes": "true",
        "state": state,
        "prompt": "consent",
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    return RedirectResponse(url)


@router.get("/oauth/callback")
async def google_oauth_callback(code: str, state: str):
    try:
        payload = _verify_state(state)
        uid = payload["uid"]
        return_to = payload.get("return_to") or "/"
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid state: {e}")

    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Google OAuth is not configured")

    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=10,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Failed to exchange token: {resp.text}")

    data = resp.json()
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expires_in = data.get("expires_in", 3600)
    if not access_token:
        raise HTTPException(status_code=400, detail="No access_token in response")

    save_tokens(uid, access_token, refresh_token, expires_in)

    # Deep link / web redirect
    if return_to.startswith("http://") or return_to.startswith("https://") or return_to.startswith("/"):
        return RedirectResponse(return_to)

    # Fallback: JSON
    return JSONResponse({"status": "connected", "uid": uid})

