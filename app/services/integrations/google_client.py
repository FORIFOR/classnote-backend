"""Google OAuth + Calendar/Gmail API client (read-only).

Endpoints used:
  - Token exchange:  https://oauth2.googleapis.com/token
  - Userinfo:        https://openidconnect.googleapis.com/v1/userinfo
  - Calendar list:   https://www.googleapis.com/calendar/v3/users/me/calendarList
  - Events list:     https://www.googleapis.com/calendar/v3/calendars/{calendarId}/events
  - Gmail list:      https://gmail.googleapis.com/gmail/v1/users/me/messages
  - Gmail get:       https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from app.services.integrations import store as integ_store

logger = logging.getLogger("app.services.integrations.google")

PROVIDER = "google"

CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or os.environ.get("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or os.environ.get("GOOGLE_CLIENT_SECRET")
REDIRECT_URI = os.environ.get(
    "GOOGLE_OAUTH_REDIRECT_URI",
    "http://localhost:8000/google/oauth/callback",
)

DEFAULT_SCOPES = " ".join([
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
])
SCOPES = os.environ.get("GOOGLE_OAUTH_SCOPES", DEFAULT_SCOPES)

TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
CAL_BASE = "https://www.googleapis.com/calendar/v3"
GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


class GoogleAuthError(RuntimeError):
    pass


class GoogleApiError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"google_api {status}: {body[:200]}")
        self.status = status
        self.body = body


def is_configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


def exchange_code(code: str) -> Dict[str, Any]:
    if not is_configured():
        raise GoogleAuthError("google_oauth_not_configured")
    resp = requests.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise GoogleAuthError(f"token_exchange_failed status={resp.status_code} body={resp.text[:200]}")
    return resp.json()


def fetch_userinfo(access_token: str) -> Dict[str, Any]:
    resp = requests.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
    if resp.status_code != 200:
        raise GoogleApiError(resp.status_code, resp.text)
    return resp.json()


def refresh_access_token(refresh_token: str) -> Dict[str, Any]:
    if not is_configured():
        raise GoogleAuthError("google_oauth_not_configured")
    resp = requests.post(
        TOKEN_URL,
        data={
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise GoogleAuthError(f"refresh_failed status={resp.status_code} body={resp.text[:200]}")
    return resp.json()


def _ensure_access_token(uid: str) -> str:
    bundle = integ_store.get_decrypted_tokens(uid, PROVIDER)
    if not bundle:
        raise GoogleAuthError("not_connected")
    expires_at: Optional[datetime] = bundle.get("expiresAt")
    if expires_at and expires_at > datetime.now(timezone.utc):
        return bundle["accessToken"]
    refresh_token = bundle.get("refreshToken")
    if not refresh_token:
        raise GoogleAuthError("no_refresh_token")
    refreshed = refresh_access_token(refresh_token)
    integ_store.update_access_token(
        uid=uid,
        provider=PROVIDER,
        access_token=refreshed["access_token"],
        expires_in=refreshed.get("expires_in"),
        scope=refreshed.get("scope"),
    )
    return refreshed["access_token"]


def _api_post(uid: str, url: str, *, json_body: Optional[Dict[str, Any]] = None,
              params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Same auto-refresh-on-401 pattern as ``_api_get`` but for POST.
    Used by Gmail send / Calendar create. Returns parsed JSON body
    (may be empty for some endpoints — caller decides what to do).
    """
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
        raise GoogleApiError(resp.status_code, resp.text)
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
        # token may have just expired → force refresh once
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
        raise GoogleApiError(resp.status_code, resp.text)
    return resp.json()


# --- Calendar -------------------------------------------------------------

def list_calendar_events(
    uid: str,
    *,
    calendar_id: str = "primary",
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    max_results: int = 25,
    page_token: Optional[str] = None,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": max(1, min(int(max_results), 100)),
    }
    if time_min:
        params["timeMin"] = time_min
    if time_max:
        params["timeMax"] = time_max
    if page_token:
        params["pageToken"] = page_token
    return _api_get(uid, f"{CAL_BASE}/calendars/{calendar_id}/events", params=params)


def list_calendar_list(uid: str) -> Dict[str, Any]:
    return _api_get(uid, f"{CAL_BASE}/users/me/calendarList")


# --- Gmail ----------------------------------------------------------------

def list_gmail_messages(
    uid: str,
    *,
    query: Optional[str] = None,
    max_results: int = 20,
    page_token: Optional[str] = None,
    label_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {"maxResults": max(1, min(int(max_results), 100))}
    if query:
        params["q"] = query
    if page_token:
        params["pageToken"] = page_token
    if label_ids:
        params["labelIds"] = label_ids
    return _api_get(uid, f"{GMAIL_BASE}/messages", params=params)


def get_gmail_message(uid: str, message_id: str, *, format: str = "metadata") -> Dict[str, Any]:
    params: Dict[str, Any] = {"format": format}
    if format == "metadata":
        params["metadataHeaders"] = ["From", "To", "Subject", "Date"]
    return _api_get(uid, f"{GMAIL_BASE}/messages/{message_id}", params=params)


# --- Phase E: Gmail send + Calendar create -------------------------------

def send_gmail_message(
    uid: str,
    *,
    to: List[str],
    subject: str,
    body_text: str,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    from_alias: Optional[str] = None,
    body_html: Optional[str] = None,
) -> Dict[str, Any]:
    """Send a plain-text (or html) email via the Gmail API.
    Requires OAuth scope ``https://www.googleapis.com/auth/gmail.send``.
    """
    import base64
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    if not to:
        raise GoogleApiError(400, "to is required")

    if body_html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body_text or "", "plain", "utf-8"))
        msg.attach(MIMEText(body_html, "html", "utf-8"))
    else:
        msg = MIMEText(body_text or "", "plain", "utf-8")

    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject or "(no subject)"
    if from_alias:
        msg["From"] = from_alias

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")
    return _api_post(uid, f"{GMAIL_BASE}/messages/send", json_body={"raw": raw})


def create_gmail_draft(
    uid: str,
    *,
    to: List[str],
    subject: str,
    body_text: str,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    from_alias: Optional[str] = None,
    body_html: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a Gmail draft (NOT sent). Returns the draft resource.

    The draft is created in the user's Gmail "Drafts" folder; the user
    must explicitly press Send in Gmail to deliver. This is the
    DeepNote V-041 default — auto-send is forbidden in this phase.

    Requires OAuth scope ``https://www.googleapis.com/auth/gmail.compose``.
    Response includes ``id`` (draft id) and ``message.id`` so the caller
    can render an ``openUrl`` like ``https://mail.google.com/mail/u/0/#drafts``.
    """
    import base64
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    if not to:
        raise GoogleApiError(400, "to is required")

    if body_html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body_text or "", "plain", "utf-8"))
        msg.attach(MIMEText(body_html, "html", "utf-8"))
    else:
        msg = MIMEText(body_text or "", "plain", "utf-8")

    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject or "(no subject)"
    if from_alias:
        msg["From"] = from_alias

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")
    return _api_post(uid, f"{GMAIL_BASE}/drafts",
                     json_body={"message": {"raw": raw}})


def delete_gmail_draft(uid: str, draft_id: str) -> None:
    """Delete a Gmail draft. Used by the readiness ``:test`` probe to
    create-then-delete without leaving a stub draft in the user's
    inbox."""
    if not draft_id:
        return
    # Gmail uses DELETE — _api_post supports POST only, so call _api_get with
    # a small request shim. Easiest: use _ensure_access_token + raw requests.
    import requests
    token = _ensure_access_token(uid)
    r = requests.delete(f"{GMAIL_BASE}/drafts/{draft_id}",
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=10)
    if r.status_code >= 400:
        raise GoogleApiError(r.status_code, r.text[:200])


def create_calendar_event(
    uid: str,
    *,
    calendar_id: str = "primary",
    summary: str,
    start: str,           # RFC3339 e.g. "2026-05-06T10:00:00+09:00"
    end: str,
    description: Optional[str] = None,
    attendees: Optional[List[str]] = None,
    location: Optional[str] = None,
    send_updates: str = "none",  # "all" | "externalOnly" | "none"
) -> Dict[str, Any]:
    """Insert a calendar event. Requires scope
    ``https://www.googleapis.com/auth/calendar.events``.
    """
    body: Dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees if a]
    return _api_post(
        uid, f"{CAL_BASE}/calendars/{calendar_id}/events",
        json_body=body, params={"sendUpdates": send_updates},
    )
