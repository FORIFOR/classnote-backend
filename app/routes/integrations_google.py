"""Google integrations endpoints (Calendar / Gmail).

Routes
  - GET  /integrations/google/oauth/start
  - GET  /google/oauth/callback                   (redirect URI registered in GCP)
  - GET  /integrations/google/status
  - DELETE /integrations/google
  - GET  /integrations/google/calendar/events
  - GET  /integrations/google/calendar/list
  - GET  /integrations/google/mail/messages
  - GET  /integrations/google/mail/messages/{id}
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse

from app.dependencies import CurrentUser, get_current_user, get_current_user_optional
from app.services import oauth_state_store, token_crypto
from app.services.integrations import google_client
from app.services.integrations import store as integ_store
from app.services.normalizers import calendar as cal_norm
from app.services.normalizers import mail as mail_norm

logger = logging.getLogger("app.routes.integrations.google")
router = APIRouter(prefix="/integrations/google", tags=["Integrations:Google"])
oauth_router = APIRouter(prefix="/google", tags=["Integrations:Google"])


def _ensure_ready():
    if not google_client.is_configured():
        raise HTTPException(status_code=503, detail="google_oauth_not_configured")
    if not token_crypto.is_configured():
        raise HTTPException(status_code=503, detail="token_crypto_not_configured")


def _resolve_uid(token: Optional[str], current_user: Optional[CurrentUser]) -> str:
    if token:
        try:
            from firebase_admin import auth
            decoded = auth.verify_id_token(token)
            return decoded["uid"]
        except Exception:
            raise HTTPException(status_code=401, detail="invalid_token")
    if current_user:
        return current_user.uid
    raise HTTPException(status_code=401, detail="not_authenticated")


@router.get("/oauth/start")
async def google_integrations_oauth_start(
    return_to: str = "/",
    token: Optional[str] = None,
    current_user: Optional[CurrentUser] = Depends(get_current_user_optional),
):
    _ensure_ready()
    uid = _resolve_uid(token, current_user)
    state = oauth_state_store.issue(
        uid=uid,
        provider="google",
        return_to=return_to,
        scope_set=google_client.SCOPES,
    )
    params = {
        "client_id": google_client.CLIENT_ID,
        "redirect_uri": google_client.REDIRECT_URI,
        "response_type": "code",
        "scope": google_client.SCOPES,
        "access_type": "offline",
        "include_granted_scopes": "true",
        "state": state,
        "prompt": "consent",
    }
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))


@oauth_router.get("/oauth/callback")
async def google_oauth_callback(code: str, state: str, error: Optional[str] = None):
    _ensure_ready()
    if error:
        raise HTTPException(status_code=400, detail=f"oauth_provider_error:{error}")
    try:
        payload = oauth_state_store.consume(state, expected_provider="google")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"state_invalid:{e}")

    uid = payload["uid"]
    return_to = payload.get("returnTo") or "/"

    try:
        token_data = google_client.exchange_code(code)
    except google_client.GoogleAuthError as e:
        raise HTTPException(status_code=400, detail=str(e))

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="no_access_token")

    account_email: Optional[str] = None
    account_id: Optional[str] = None
    try:
        info = google_client.fetch_userinfo(access_token)
        account_email = info.get("email")
        account_id = info.get("sub")
    except Exception as e:
        logger.warning("[google] userinfo fetch failed: %s", e)

    integ_store.save_tokens(
        uid=uid,
        provider="google",
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=token_data.get("expires_in"),
        scope=token_data.get("scope"),
        token_type=token_data.get("token_type", "Bearer"),
        account_email=account_email,
        account_id=account_id,
    )
    logger.info("[google] oauth connected uid=%s email=%s", uid, account_email)
    return RedirectResponse(return_to)


@router.get("/status")
async def google_status(current_user: CurrentUser = Depends(get_current_user)):
    data = integ_store.load(current_user.uid, "google") or {}
    return {
        "connected": data.get("status") == "connected",
        "accountEmail": data.get("accountEmail"),
        "accountId": data.get("accountId"),
        "scope": data.get("scope"),
        "expiresAt": data.get("expiresAt"),
        "lastError": data.get("lastError"),
        "lastErrorAt": data.get("lastErrorAt"),
    }


@router.delete("", status_code=204)
async def google_disconnect(current_user: CurrentUser = Depends(get_current_user)):
    integ_store.revoke(current_user.uid, "google")
    return None


@router.get("/calendar/events")
async def google_calendar_events(
    calendarId: str = Query("primary"),
    timeMin: Optional[str] = Query(None),
    timeMax: Optional[str] = Query(None),
    maxResults: int = Query(25, ge=1, le=100),
    pageToken: Optional[str] = Query(None),
    current_user: CurrentUser = Depends(get_current_user),
):
    _ensure_ready()
    try:
        raw = google_client.list_calendar_events(
            current_user.uid,
            calendar_id=calendarId,
            time_min=timeMin,
            time_max=timeMax,
            max_results=maxResults,
            page_token=pageToken,
        )
    except google_client.GoogleAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except google_client.GoogleApiError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {
        "items": cal_norm.normalize_google_events(raw.get("items") or [], calendar_id=calendarId),
        "nextPageToken": raw.get("nextPageToken"),
    }


@router.get("/calendar/list")
async def google_calendar_list(current_user: CurrentUser = Depends(get_current_user)):
    _ensure_ready()
    try:
        raw = google_client.list_calendar_list(current_user.uid)
    except google_client.GoogleAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except google_client.GoogleApiError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"items": raw.get("items") or []}


@router.get("/mail/messages")
async def google_gmail_messages(
    q: Optional[str] = Query(None),
    maxResults: int = Query(20, ge=1, le=100),
    pageToken: Optional[str] = Query(None),
    current_user: CurrentUser = Depends(get_current_user),
):
    _ensure_ready()
    try:
        raw = google_client.list_gmail_messages(
            current_user.uid,
            query=q,
            max_results=maxResults,
            page_token=pageToken,
        )
    except google_client.GoogleAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except google_client.GoogleApiError as e:
        raise HTTPException(status_code=502, detail=str(e))
    items_meta = raw.get("messages") or []
    detail_items = []
    for m in items_meta:
        try:
            detail = google_client.get_gmail_message(current_user.uid, m["id"], format="metadata")
            detail_items.append(mail_norm.normalize_gmail_message(detail))
        except google_client.GoogleApiError:
            continue
    return {
        "items": detail_items,
        "nextPageToken": raw.get("nextPageToken"),
    }


@router.get("/mail/messages/{message_id}")
async def google_gmail_message(message_id: str, current_user: CurrentUser = Depends(get_current_user)):
    _ensure_ready()
    try:
        msg = google_client.get_gmail_message(current_user.uid, message_id, format="metadata")
    except google_client.GoogleApiError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return mail_norm.normalize_gmail_message(msg)
