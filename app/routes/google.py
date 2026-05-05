from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse, JSONResponse

from app.dependencies import get_current_user, get_current_user_optional, CurrentUser
from app.google_calendar import (
    _sign_state,
    _verify_state,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_REDIRECT_URI,
    GOOGLE_SCOPES,
    delete_tokens,
    list_events,
    load_tokens,
    save_tokens,
)


router = APIRouter(prefix="/google")
integrations_router = APIRouter(prefix="/integrations/google", tags=["Google Integration"])


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


# ────────────────────────────────────────────────
# /integrations/google/status, /integrations/google, /integrations/google/calendar/events
# ────────────────────────────────────────────────

@integrations_router.get("/status")
async def google_integration_status(current_user: CurrentUser = Depends(get_current_user)):
    tokens = load_tokens(current_user.uid)
    if not tokens:
        return {"connected": False}

    expires_at = tokens.get("expiresAt")
    expires_iso: str | None = None
    if isinstance(expires_at, datetime):
        expires_iso = expires_at.astimezone(timezone.utc).isoformat()
    elif isinstance(expires_at, str):
        expires_iso = expires_at

    return {
        "connected": True,
        "hasRefreshToken": bool(tokens.get("refreshToken")),
        "expiresAt": expires_iso,
        "scopes": GOOGLE_SCOPES.split(),
    }


@integrations_router.delete("")
async def google_integration_disconnect(current_user: CurrentUser = Depends(get_current_user)):
    existed = delete_tokens(current_user.uid)
    return {"disconnected": existed}


@integrations_router.get("/calendar/events")
async def list_google_calendar_events(
    start: datetime | None = Query(
        None,
        description="ISO8601 (UTC). 省略時は現在時刻。",
    ),
    end: datetime | None = Query(
        None,
        description="ISO8601 (UTC). 省略時は start + 7 日。",
    ),
    top: int = Query(50, ge=1, le=200, description="返す最大件数（1-200）"),
    calendar_id: str = Query("primary", description="カレンダー ID（既定: primary）"),
    current_user: CurrentUser = Depends(get_current_user),
):
    now = datetime.now(timezone.utc)
    s = start or now
    e = end or (s + timedelta(days=7))
    if e <= s:
        raise HTTPException(status_code=400, detail="end must be after start")

    try:
        events = list_events(current_user.uid, s, e, top=top, calendar_id=calendar_id)
    except RuntimeError as ex:
        msg = str(ex)
        if "not connected" in msg:
            raise HTTPException(status_code=409, detail="Google Calendar not connected")
        if "not configured" in msg:
            raise HTTPException(status_code=500, detail=msg)
        if "access denied" in msg:
            raise HTTPException(status_code=401, detail=msg)
        raise HTTPException(status_code=502, detail=msg)

    return {
        "calendarId": calendar_id,
        "start": s.isoformat(),
        "end": e.isoformat(),
        "count": len(events),
        "events": events,
    }
