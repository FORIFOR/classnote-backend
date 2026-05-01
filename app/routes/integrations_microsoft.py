"""Microsoft integrations endpoints (Outlook Calendar / Mail)."""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse

from app.dependencies import CurrentUser, get_current_user, get_current_user_optional
from app.services import oauth_state_store, token_crypto
from app.services.integrations import microsoft_client
from app.services.integrations import store as integ_store
from app.services.normalizers import calendar as cal_norm
from app.services.normalizers import mail as mail_norm

logger = logging.getLogger("app.routes.integrations.microsoft")
router = APIRouter(prefix="/integrations/microsoft", tags=["Integrations:Microsoft"])
oauth_router = APIRouter(prefix="/auth/microsoft", tags=["Integrations:Microsoft"])


def _ensure_ready():
    if not microsoft_client.is_configured():
        raise HTTPException(status_code=503, detail="microsoft_oauth_not_configured")
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


@oauth_router.get("/start")
async def microsoft_oauth_start(
    return_to: str = "/",
    token: Optional[str] = None,
    current_user: Optional[CurrentUser] = Depends(get_current_user_optional),
):
    """Canonical path: /auth/microsoft/start (matches Apr-29 dev contract)."""
    _ensure_ready()
    uid = _resolve_uid(token, current_user)
    state = oauth_state_store.issue(
        uid=uid,
        provider="microsoft",
        return_to=return_to,
        scope_set=microsoft_client.SCOPES,
    )
    params = {
        "client_id": microsoft_client.CLIENT_ID,
        "response_type": "code",
        "redirect_uri": microsoft_client.REDIRECT_URI,
        "response_mode": "query",
        "scope": microsoft_client.SCOPES,
        "state": state,
        "prompt": "select_account",
    }
    return RedirectResponse(microsoft_client.AUTH_URL + "?" + urlencode(params))


# Backward-compat alias under /integrations prefix
@router.get("/oauth/start")
async def microsoft_oauth_start_compat(
    return_to: str = "/",
    token: Optional[str] = None,
    current_user: Optional[CurrentUser] = Depends(get_current_user_optional),
):
    return await microsoft_oauth_start(return_to=return_to, token=token, current_user=current_user)


@oauth_router.get("/callback")
async def microsoft_oauth_callback(code: str, state: str, error: Optional[str] = None):
    _ensure_ready()
    if error:
        raise HTTPException(status_code=400, detail=f"oauth_provider_error:{error}")
    try:
        payload = oauth_state_store.consume(state, expected_provider="microsoft")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"state_invalid:{e}")

    uid = payload["uid"]
    return_to = payload.get("returnTo") or "/"

    try:
        token_data = microsoft_client.exchange_code(code)
    except microsoft_client.MicrosoftAuthError as e:
        raise HTTPException(status_code=400, detail=str(e))

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="no_access_token")

    account_email: Optional[str] = None
    account_id: Optional[str] = None
    try:
        info = microsoft_client.fetch_userinfo(access_token)
        account_email = info.get("mail") or info.get("userPrincipalName")
        account_id = info.get("id")
    except Exception as e:
        logger.warning("[microsoft] userinfo fetch failed: %s", e)

    integ_store.save_tokens(
        uid=uid,
        provider="microsoft",
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=token_data.get("expires_in"),
        scope=token_data.get("scope"),
        token_type=token_data.get("token_type", "Bearer"),
        account_email=account_email,
        account_id=account_id,
    )
    logger.info("[microsoft] oauth connected uid=%s email=%s", uid, account_email)
    return RedirectResponse(return_to)


@router.get("/status")
async def microsoft_status(current_user: CurrentUser = Depends(get_current_user)):
    data = integ_store.load(current_user.uid, "microsoft") or {}
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
async def microsoft_disconnect(current_user: CurrentUser = Depends(get_current_user)):
    integ_store.revoke(current_user.uid, "microsoft")
    return None


@router.get("/calendar/events")
async def microsoft_calendar_events(
    startDateTime: Optional[str] = Query(None),
    endDateTime: Optional[str] = Query(None),
    top: int = Query(25, ge=1, le=100),
    skip: int = Query(0, ge=0),
    current_user: CurrentUser = Depends(get_current_user),
):
    _ensure_ready()
    try:
        raw = microsoft_client.list_calendar_events(
            current_user.uid,
            start_datetime=startDateTime,
            end_datetime=endDateTime,
            top=top,
            skip=skip,
        )
    except microsoft_client.MicrosoftAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except microsoft_client.MicrosoftApiError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {
        "items": cal_norm.normalize_microsoft_events(raw.get("value") or []),
        "nextLink": raw.get("@odata.nextLink"),
    }


@router.get("/mail/messages")
async def microsoft_mail_messages(
    top: int = Query(25, ge=1, le=100),
    search: Optional[str] = Query(None),
    folder: Optional[str] = Query(None),
    current_user: CurrentUser = Depends(get_current_user),
):
    _ensure_ready()
    try:
        raw = microsoft_client.list_mail_messages(current_user.uid, top=top, search=search, folder=folder)
    except microsoft_client.MicrosoftAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except microsoft_client.MicrosoftApiError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {
        "items": [mail_norm.normalize_microsoft_message(m) for m in (raw.get("value") or [])],
        "nextLink": raw.get("@odata.nextLink"),
    }


@router.get("/mail/messages/{message_id}")
async def microsoft_mail_message(message_id: str, current_user: CurrentUser = Depends(get_current_user)):
    _ensure_ready()
    try:
        msg = microsoft_client.get_mail_message(current_user.uid, message_id)
    except microsoft_client.MicrosoftApiError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return mail_norm.normalize_microsoft_message(msg)
