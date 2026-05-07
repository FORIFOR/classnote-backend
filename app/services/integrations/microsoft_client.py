"""Microsoft Entra ID + Microsoft Graph (Calendar/Mail) read-only client."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from app.services.integrations import store as integ_store

logger = logging.getLogger("app.services.integrations.microsoft")

PROVIDER = "microsoft"

CLIENT_ID = os.environ.get("MICROSOFT_OAUTH_CLIENT_ID") or os.environ.get("MICROSOFT_CLIENT_ID")
CLIENT_SECRET = os.environ.get("MICROSOFT_OAUTH_CLIENT_SECRET") or os.environ.get("MICROSOFT_CLIENT_SECRET")
TENANT = os.environ.get("MICROSOFT_OAUTH_TENANT", "common")
REDIRECT_URI = os.environ.get(
    "MICROSOFT_OAUTH_REDIRECT_URI",
    "http://localhost:8000/auth/microsoft/callback",
)

DEFAULT_SCOPES = " ".join([
    "openid",
    "profile",
    "email",
    "offline_access",
    "User.Read",
    "Calendars.Read",
    "Mail.Read",
])
SCOPES = os.environ.get("MICROSOFT_OAUTH_SCOPES", DEFAULT_SCOPES)

AUTH_URL = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class MicrosoftAuthError(RuntimeError):
    pass


class MicrosoftApiError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"microsoft_api {status}: {body[:200]}")
        self.status = status
        self.body = body


def is_configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


def exchange_code(code: str) -> Dict[str, Any]:
    if not is_configured():
        raise MicrosoftAuthError("microsoft_oauth_not_configured")
    resp = requests.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
            "scope": SCOPES,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise MicrosoftAuthError(f"token_exchange_failed status={resp.status_code} body={resp.text[:200]}")
    return resp.json()


def refresh_access_token(refresh_token: str) -> Dict[str, Any]:
    if not is_configured():
        raise MicrosoftAuthError("microsoft_oauth_not_configured")
    resp = requests.post(
        TOKEN_URL,
        data={
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "scope": SCOPES,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise MicrosoftAuthError(f"refresh_failed status={resp.status_code} body={resp.text[:200]}")
    return resp.json()


def fetch_userinfo(access_token: str) -> Dict[str, Any]:
    resp = requests.get(f"{GRAPH_BASE}/me", headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
    if resp.status_code != 200:
        raise MicrosoftApiError(resp.status_code, resp.text)
    return resp.json()


def _ensure_access_token(uid: str) -> str:
    bundle = integ_store.get_decrypted_tokens(uid, PROVIDER)
    if not bundle:
        raise MicrosoftAuthError("not_connected")
    expires_at: Optional[datetime] = bundle.get("expiresAt")
    if expires_at and expires_at > datetime.now(timezone.utc):
        return bundle["accessToken"]
    refresh_token = bundle.get("refreshToken")
    if not refresh_token:
        raise MicrosoftAuthError("no_refresh_token")
    refreshed = refresh_access_token(refresh_token)
    integ_store.update_access_token(
        uid=uid,
        provider=PROVIDER,
        access_token=refreshed["access_token"],
        expires_in=refreshed.get("expires_in"),
        scope=refreshed.get("scope"),
    )
    new_refresh = refreshed.get("refresh_token")
    if new_refresh:
        # Microsoft rotates refresh tokens — store the latest
        integ_store.save_tokens(
            uid=uid,
            provider=PROVIDER,
            access_token=refreshed["access_token"],
            refresh_token=new_refresh,
            expires_in=refreshed.get("expires_in"),
            scope=refreshed.get("scope"),
        )
    return refreshed["access_token"]


def _api_post(uid: str, url: str, *, json_body: Optional[Dict[str, Any]] = None,
              params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """POST with auto-refresh-on-401. Used by Outlook send + Calendar create."""
    token = _ensure_access_token(uid)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.post(url, params=params or {}, json=json_body or {}, headers=headers, timeout=20)
    if resp.status_code == 401:
        bundle = integ_store.get_decrypted_tokens(uid, PROVIDER) or {}
        if bundle.get("refreshToken"):
            refreshed = refresh_access_token(bundle["refreshToken"])
            integ_store.update_access_token(
                uid=uid, provider=PROVIDER,
                access_token=refreshed["access_token"],
                expires_in=refreshed.get("expires_in"),
                scope=refreshed.get("scope"),
            )
            headers["Authorization"] = f"Bearer {refreshed['access_token']}"
            resp = requests.post(url, params=params or {}, json=json_body or {}, headers=headers, timeout=20)
    if resp.status_code not in (200, 201, 202, 204):
        integ_store.mark_error(uid, PROVIDER, f"POST {url} -> {resp.status_code}: {resp.text[:200]}")
        raise MicrosoftApiError(resp.status_code, resp.text)
    if not resp.content:
        return {}
    try:
        return resp.json()
    except Exception:
        return {}


def _api_get(uid: str, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    token = _ensure_access_token(uid)
    resp = requests.get(url, params=params or {}, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    if resp.status_code == 401:
        bundle = integ_store.get_decrypted_tokens(uid, PROVIDER) or {}
        if bundle.get("refreshToken"):
            refreshed = refresh_access_token(bundle["refreshToken"])
            integ_store.update_access_token(
                uid=uid,
                provider=PROVIDER,
                access_token=refreshed["access_token"],
                expires_in=refreshed.get("expires_in"),
                scope=refreshed.get("scope"),
            )
            resp = requests.get(url, params=params or {}, headers={"Authorization": f"Bearer {refreshed['access_token']}"}, timeout=15)
    if resp.status_code != 200:
        integ_store.mark_error(uid, PROVIDER, f"GET {url} -> {resp.status_code}: {resp.text[:200]}")
        raise MicrosoftApiError(resp.status_code, resp.text)
    return resp.json()


# --- Calendar -------------------------------------------------------------

def list_calendar_events(
    uid: str,
    *,
    start_datetime: Optional[str] = None,
    end_datetime: Optional[str] = None,
    top: int = 25,
    skip: int = 0,
) -> Dict[str, Any]:
    """If start/end are provided, use calendarView for time range filtering;
    otherwise list /events ordered by start.
    """
    if start_datetime or end_datetime:
        params: Dict[str, Any] = {
            "startDateTime": start_datetime or datetime.now(timezone.utc).isoformat(),
            "endDateTime": end_datetime,
            "$top": max(1, min(int(top), 100)),
            "$orderby": "start/dateTime",
        }
        params = {k: v for k, v in params.items() if v is not None}
        return _api_get(uid, f"{GRAPH_BASE}/me/calendarView", params=params)
    params = {
        "$top": max(1, min(int(top), 100)),
        "$skip": max(0, int(skip)),
        "$orderby": "start/dateTime",
    }
    return _api_get(uid, f"{GRAPH_BASE}/me/events", params=params)


# --- Mail -----------------------------------------------------------------

def list_mail_messages(
    uid: str,
    *,
    top: int = 25,
    search: Optional[str] = None,
    folder: Optional[str] = None,
) -> Dict[str, Any]:
    base = f"{GRAPH_BASE}/me/mailFolders/{folder}/messages" if folder else f"{GRAPH_BASE}/me/messages"
    params: Dict[str, Any] = {
        "$top": max(1, min(int(top), 100)),
        "$select": "id,subject,from,toRecipients,receivedDateTime,bodyPreview,isRead,webLink",
        "$orderby": "receivedDateTime DESC",
    }
    if search:
        params["$search"] = f"\"{search}\""
        params.pop("$orderby", None)  # $search and $orderby cannot coexist
    return _api_get(uid, base, params=params)


def get_mail_message(uid: str, message_id: str) -> Dict[str, Any]:
    return _api_get(uid, f"{GRAPH_BASE}/me/messages/{message_id}", params={
        "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,body,bodyPreview,isRead,webLink",
    })


# --- Phase E: Outlook send + Calendar create -----------------------------

def send_mail(
    uid: str,
    *,
    to: List[str],
    subject: str,
    body_text: str,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    body_html: Optional[str] = None,
    save_to_sent_items: bool = True,
) -> Dict[str, Any]:
    """Send via Microsoft Graph (``POST /me/sendMail``). Requires
    ``Mail.Send`` scope."""
    if not to:
        raise MicrosoftApiError(400, "to is required")
    body = {
        "contentType": "HTML" if body_html else "Text",
        "content": body_html or (body_text or ""),
    }
    msg: Dict[str, Any] = {
        "subject": subject or "(no subject)",
        "body": body,
        "toRecipients": [{"emailAddress": {"address": a}} for a in to if a],
    }
    if cc:
        msg["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc if a]
    if bcc:
        msg["bccRecipients"] = [{"emailAddress": {"address": a}} for a in bcc if a]
    payload = {"message": msg, "saveToSentItems": bool(save_to_sent_items)}
    return _api_post(uid, f"{GRAPH_BASE}/me/sendMail", json_body=payload)


def create_outlook_draft(
    uid: str,
    *,
    to: List[str],
    subject: str,
    body_text: str,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    body_html: Optional[str] = None,
) -> Dict[str, Any]:
    """Create an Outlook draft via Microsoft Graph (``POST /me/messages``).
    The created message is a draft until the user explicitly sends it
    from Outlook. Requires ``Mail.ReadWrite`` scope.

    Returns the message resource (includes ``id`` and ``webLink`` so the
    caller can render an ``openUrl``).
    """
    if not to:
        raise MicrosoftApiError(400, "to is required")
    body = {
        "contentType": "HTML" if body_html else "Text",
        "content": body_html or (body_text or ""),
    }
    msg: Dict[str, Any] = {
        "subject": subject or "(no subject)",
        "body": body,
        "toRecipients": [{"emailAddress": {"address": a}} for a in to if a],
    }
    if cc:
        msg["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc if a]
    if bcc:
        msg["bccRecipients"] = [{"emailAddress": {"address": a}} for a in bcc if a]
    return _api_post(uid, f"{GRAPH_BASE}/me/messages", json_body=msg)


def delete_outlook_draft(uid: str, message_id: str) -> None:
    """Delete an Outlook draft. Used by readiness probe."""
    if not message_id:
        return
    import requests
    token = _ensure_access_token(uid)
    r = requests.delete(f"{GRAPH_BASE}/me/messages/{message_id}",
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=10)
    if r.status_code >= 400 and r.status_code != 404:
        raise MicrosoftApiError(r.status_code, r.text[:200])


def create_calendar_event(
    uid: str,
    *,
    subject: str,
    start: str,                # ISO 8601 e.g. "2026-05-06T10:00:00"
    end: str,
    timezone_name: str = "Asia/Tokyo",
    body_text: Optional[str] = None,
    attendees: Optional[List[str]] = None,
    location: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a calendar event via Microsoft Graph
    (``POST /me/events``). Requires ``Calendars.ReadWrite`` scope."""
    body: Dict[str, Any] = {
        "subject": subject,
        "start": {"dateTime": start, "timeZone": timezone_name},
        "end": {"dateTime": end, "timeZone": timezone_name},
    }
    if body_text:
        body["body"] = {"contentType": "Text", "content": body_text}
    if location:
        body["location"] = {"displayName": location}
    if attendees:
        body["attendees"] = [
            {"emailAddress": {"address": a}, "type": "required"}
            for a in attendees if a
        ]
    return _api_post(uid, f"{GRAPH_BASE}/me/events", json_body=body)
