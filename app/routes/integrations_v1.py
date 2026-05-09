"""Unified ``/v1/integrations/*`` surface (V-041-B).

Goals:
  - One stop status / disconnect / readiness probe across providers.
  - One unified mail draft endpoint that picks the provider per
    connected status (no auto-send — drafts only).
  - One unified calendar events endpoint that fans out to whichever
    providers the caller has connected.

Per-provider routes (``/integrations/google/*``, ``/integrations/microsoft/*``)
remain in place for backwards compatibility — this surface is **add-only**.

Hard rules carried forward:
  - No auto-send. ``send_*_message`` is intentionally NOT exposed here.
  - Token encryption required: ``store.get_decrypted_tokens`` enforces it.
  - Disconnect == revoke local token; we do NOT call provider revoke
    endpoints (the user does that themselves via their account page).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_current_user, CurrentUser
from app.services.integrations import (
    google_client as _g,
    microsoft_client as _ms,
    store as _store,
)

logger = logging.getLogger("app.routes.integrations_v1")

router = APIRouter(prefix="/v1/integrations", tags=["Integrations"])

PROVIDERS = ("google", "microsoft")


# ──────────────────────────────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────────────────────────────

class IntegrationStatus(BaseModel):
    provider: Literal["google", "microsoft"]
    connected: bool
    email: Optional[str] = None
    scopes: List[str] = Field(default_factory=list)
    capabilities: Dict[str, bool] = Field(default_factory=dict)
    lastHealthCheckAt: Optional[str] = None
    lastError: Optional[str] = None


class IntegrationsResponse(BaseModel):
    items: List[IntegrationStatus]


class TestResponse(BaseModel):
    provider: Literal["google", "microsoft"]
    ok: bool
    durationMs: int
    checks: List[Dict[str, Any]]
    reason: Optional[str] = None


class DisconnectResponse(BaseModel):
    provider: Literal["google", "microsoft"]
    revoked: bool


class MailDraftRequest(BaseModel):
    provider: Literal["gmail", "outlook"]
    to: List[str]
    subject: str
    body: str = ""
    cc: List[str] = Field(default_factory=list)
    bcc: List[str] = Field(default_factory=list)
    bodyHtml: Optional[str] = None
    sourceSessionId: Optional[str] = None


class MailDraftResponse(BaseModel):
    draftId: str
    externalDraftId: str
    provider: Literal["gmail", "outlook"]
    openUrl: str


class CalendarEventDTO(BaseModel):
    provider: Literal["google", "microsoft"]
    externalEventId: str
    title: str
    startAt: Optional[str] = None
    endAt: Optional[str] = None
    attendees: List[Dict[str, Any]] = Field(default_factory=list)
    meetingUrl: Optional[str] = None
    location: Optional[str] = None


class CalendarEventsResponse(BaseModel):
    items: List[CalendarEventDTO]


# ──────────────────────────────────────────────────────────────────────
# GET /v1/integrations — aggregated status
# ──────────────────────────────────────────────────────────────────────

def _capabilities_from_scopes(provider: str, scopes: List[str]) -> Dict[str, bool]:
    """Translate raw OAuth scopes into the user-facing capability flags
    the spec calls out (calendarRead / calendarWrite / mailDraft /
    mailSend)."""
    s = set(scopes or [])
    if provider == "google":
        return {
            "calendarRead":  any("calendar" in x for x in s),
            "calendarWrite": "https://www.googleapis.com/auth/calendar.events" in s
                              or "https://www.googleapis.com/auth/calendar" in s,
            "mailDraft":     "https://www.googleapis.com/auth/gmail.compose" in s
                              or "https://www.googleapis.com/auth/gmail.modify" in s,
            "mailSend":      "https://www.googleapis.com/auth/gmail.send" in s,
        }
    if provider == "microsoft":
        ls = {x.lower() for x in s}
        return {
            "calendarRead":  any("calendars.read" in x for x in ls),
            "calendarWrite": "calendars.readwrite" in ls,
            "mailDraft":     "mail.readwrite" in ls,
            "mailSend":      "mail.send" in ls,
        }
    return {}


def _status_for(uid: str, provider: str) -> IntegrationStatus:
    rec = _store.load(uid, provider)
    if not rec:
        return IntegrationStatus(
            provider=provider,  # type: ignore[arg-type]
            connected=False,
            scopes=[],
            capabilities={"calendarRead": False, "calendarWrite": False,
                          "mailDraft": False, "mailSend": False},
        )
    # Storage key compatibility: integ_store.save_tokens persists ``scope``
    # as a space-separated string and ``accountEmail`` as the user's email.
    # The canonical V1 contract surfaces ``scopes`` (list) and ``email``.
    # Accept both shapes so the desktop client sees populated values
    # whether the row came from save_tokens (legacy) or a writer that
    # uses canonical keys directly.
    scopes_raw = rec.get("scopes") or rec.get("scope") or []
    if isinstance(scopes_raw, str):
        scopes = scopes_raw.split()
    else:
        scopes = list(scopes_raw)
    return IntegrationStatus(
        provider=provider,  # type: ignore[arg-type]
        connected=bool(rec.get("status") == "connected"
                       or rec.get("encryptedRefreshToken")),
        email=rec.get("email") or rec.get("accountEmail"),
        scopes=scopes,
        capabilities=_capabilities_from_scopes(provider, scopes),
        lastHealthCheckAt=rec.get("lastHealthCheckAt") and rec["lastHealthCheckAt"].isoformat()
                          if hasattr(rec.get("lastHealthCheckAt"), "isoformat")
                          else rec.get("lastHealthCheckAt"),
        lastError=rec.get("lastError"),
    )


@router.get("", response_model=IntegrationsResponse)
def list_integrations(current_user: CurrentUser = Depends(get_current_user)):
    items = [_status_for(current_user.uid, p) for p in PROVIDERS]
    return IntegrationsResponse(items=items)


# ──────────────────────────────────────────────────────────────────────
# POST /v1/integrations/{provider}:disconnect
# ──────────────────────────────────────────────────────────────────────

@router.post("/{provider}:disconnect", response_model=DisconnectResponse)
def disconnect_integration(
    provider: Literal["google", "microsoft"],
    current_user: CurrentUser = Depends(get_current_user),
):
    rec = _store.load(current_user.uid, provider)
    if not rec:
        return DisconnectResponse(provider=provider, revoked=False)
    _store.revoke(current_user.uid, provider)
    return DisconnectResponse(provider=provider, revoked=True)


# ──────────────────────────────────────────────────────────────────────
# POST /v1/integrations/{provider}:test — readiness probe
# ──────────────────────────────────────────────────────────────────────

import time as _time


@router.post("/{provider}:test", response_model=TestResponse)
def test_integration(
    provider: Literal["google", "microsoft"],
    current_user: CurrentUser = Depends(get_current_user),
):
    """Lightweight readiness probe. Performs one Calendar list + one
    profile read. Refuses to attempt anything if the user is not
    connected. Used by Desktop's `[接続テスト]` button and by the
    backend smoke matrix."""
    started = _time.time()
    checks: List[Dict[str, Any]] = []
    rec = _store.load(current_user.uid, provider)
    if not rec or not rec.get("encryptedRefreshToken"):
        return TestResponse(
            provider=provider, ok=False,
            durationMs=int((_time.time() - started) * 1000),
            checks=[{"name": "connected", "ok": False}],
            reason="not_connected",
        )

    if provider == "google":
        try:
            tok = _g._ensure_access_token(current_user.uid)
            checks.append({"name": "token_refresh", "ok": True})
        except Exception as e:
            return TestResponse(
                provider=provider, ok=False,
                durationMs=int((_time.time() - started) * 1000),
                checks=[{"name": "token_refresh", "ok": False, "error": str(e)[:120]}],
                reason="token_refresh_failed",
            )
        try:
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            res = _g.list_calendar_events(
                current_user.uid,
                time_min=now.isoformat().replace("+00:00", "Z"),
                time_max=(now + timedelta(days=7)).isoformat().replace("+00:00", "Z"),
                max_results=1,
            )
            checks.append({"name": "calendar.events.list",
                           "ok": True, "items": len(res.get("items", []))})
        except Exception as e:
            checks.append({"name": "calendar.events.list",
                           "ok": False, "error": str(e)[:120]})
    elif provider == "microsoft":
        try:
            tok = _ms._ensure_access_token(current_user.uid)
            checks.append({"name": "token_refresh", "ok": True})
        except Exception as e:
            return TestResponse(
                provider=provider, ok=False,
                durationMs=int((_time.time() - started) * 1000),
                checks=[{"name": "token_refresh", "ok": False, "error": str(e)[:120]}],
                reason="token_refresh_failed",
            )
        try:
            res = _ms.list_calendar_events(current_user.uid, top=1)
            checks.append({"name": "calendar.events", "ok": True,
                           "items": len(res.get("value", []))})
        except Exception as e:
            checks.append({"name": "calendar.events",
                           "ok": False, "error": str(e)[:120]})

    ok = all(c.get("ok") for c in checks)
    return TestResponse(
        provider=provider, ok=ok,
        durationMs=int((_time.time() - started) * 1000),
        checks=checks,
        reason=None if ok else "probe_failed",
    )


# ──────────────────────────────────────────────────────────────────────
# POST /v1/integrations/mail/drafts — unified draft create
# ──────────────────────────────────────────────────────────────────────

@router.post("/mail/drafts", response_model=MailDraftResponse, status_code=201)
def create_mail_draft(
    req: MailDraftRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Create a draft email in Gmail or Outlook. Never sends."""
    if not req.to:
        raise HTTPException(status_code=400,
                            detail={"code": "to_required"})
    provider = "google" if req.provider == "gmail" else "microsoft"
    rec = _store.load(current_user.uid, provider)
    if not rec or not rec.get("encryptedRefreshToken"):
        raise HTTPException(
            status_code=409,
            detail={"code": "not_connected",
                    "message": f"connect {req.provider} first via /integrations/{provider}/oauth/start"},
        )

    try:
        if req.provider == "gmail":
            res = _g.create_gmail_draft(
                current_user.uid,
                to=req.to, subject=req.subject, body_text=req.body,
                cc=req.cc or None, bcc=req.bcc or None,
                body_html=req.bodyHtml,
            )
            external_id = res.get("id") or ""
            open_url = "https://mail.google.com/mail/u/0/#drafts"
        else:  # outlook
            res = _ms.create_outlook_draft(
                current_user.uid,
                to=req.to, subject=req.subject, body_text=req.body,
                cc=req.cc or None, bcc=req.bcc or None,
                body_html=req.bodyHtml,
            )
            external_id = res.get("id") or ""
            open_url = res.get("webLink") or "https://outlook.office.com/mail/drafts"
    except (_g.GoogleApiError, _ms.MicrosoftApiError, _g.GoogleAuthError, _ms.MicrosoftAuthError) as e:
        raise HTTPException(status_code=502,
                            detail={"code": "provider_api_failed",
                                    "message": str(e)[:200]})

    return MailDraftResponse(
        draftId=f"draft_{external_id}",
        externalDraftId=external_id,
        provider=req.provider,  # type: ignore[arg-type]
        openUrl=open_url,
    )


# ──────────────────────────────────────────────────────────────────────
# GET /v1/integrations/calendar/events — unified, fans out
# ──────────────────────────────────────────────────────────────────────

from fastapi import Query as _Q


@router.get("/calendar/events", response_model=CalendarEventsResponse)
def calendar_events(
    from_: Optional[str] = _Q(None, alias="from"),
    to: Optional[str] = None,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Fan-out across connected providers. Time range defaults to
    [now, now+7d). Items are normalised into ``CalendarEventDTO``.
    Disconnected providers are silently skipped."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    f = from_ or now.isoformat().replace("+00:00", "Z")
    t = to or (now + timedelta(days=7)).isoformat().replace("+00:00", "Z")

    items: List[CalendarEventDTO] = []

    # Google
    if (_store.load(current_user.uid, "google") or {}).get("encryptedRefreshToken"):
        try:
            res = _g.list_calendar_events(
                current_user.uid, time_min=f, time_max=t, max_results=50,
            )
            for ev in (res.get("items") or []):
                start = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date")
                end = (ev.get("end") or {}).get("dateTime") or (ev.get("end") or {}).get("date")
                items.append(CalendarEventDTO(
                    provider="google",
                    externalEventId=ev.get("id") or "",
                    title=ev.get("summary") or "(no title)",
                    startAt=start, endAt=end,
                    attendees=[{"email": a.get("email"), "displayName": a.get("displayName")}
                               for a in (ev.get("attendees") or [])],
                    meetingUrl=ev.get("hangoutLink"),
                    location=ev.get("location"),
                ))
        except Exception as e:
            logger.warning("[integrations.calendar.google] fan-out failed: %s", e)

    # Microsoft
    if (_store.load(current_user.uid, "microsoft") or {}).get("encryptedRefreshToken"):
        try:
            res = _ms.list_calendar_events(
                current_user.uid,
                start_datetime=f, end_datetime=t, top=50,
            )
            for ev in (res.get("value") or []):
                items.append(CalendarEventDTO(
                    provider="microsoft",
                    externalEventId=ev.get("id") or "",
                    title=ev.get("subject") or "(no title)",
                    startAt=(ev.get("start") or {}).get("dateTime"),
                    endAt=(ev.get("end") or {}).get("dateTime"),
                    attendees=[{"email": a.get("emailAddress", {}).get("address"),
                                "displayName": a.get("emailAddress", {}).get("name")}
                               for a in (ev.get("attendees") or [])],
                    meetingUrl=(ev.get("onlineMeeting") or {}).get("joinUrl"),
                    location=(ev.get("location") or {}).get("displayName"),
                ))
        except Exception as e:
            logger.warning("[integrations.calendar.microsoft] fan-out failed: %s", e)

    items.sort(key=lambda e: e.startAt or "")
    return CalendarEventsResponse(items=items)
