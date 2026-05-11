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
    # Per-capability fields (Desktop spec §3.4 — one row per capability,
    # not per provider). ``id`` and ``capability`` are the canonical
    # discriminator; ``status`` is the string form Desktop UI checks
    # via ``status === 'connected'``.
    id: Optional[str] = None
    capability: Optional[str] = None
    status: Optional[Literal["connected", "disconnected", "needs_reconnect"]] = None
    # Provider-level fields (kept for back-compat with any consumer that
    # still reads the per-provider shape).
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
    the spec calls out (calendarRead / calendarWrite / mailRead /
    mailDraft / mailSend)."""
    s = set(scopes or [])
    if provider == "google":
        return {
            "calendarRead":  any("calendar" in x for x in s),
            "calendarWrite": "https://www.googleapis.com/auth/calendar.events" in s
                              or "https://www.googleapis.com/auth/calendar" in s,
            "mailRead":      "https://www.googleapis.com/auth/gmail.readonly" in s
                              or "https://www.googleapis.com/auth/gmail.modify" in s,
            "mailDraft":     "https://www.googleapis.com/auth/gmail.compose" in s
                              or "https://www.googleapis.com/auth/gmail.modify" in s,
            "mailSend":      "https://www.googleapis.com/auth/gmail.send" in s,
        }
    if provider == "microsoft":
        ls = {x.lower() for x in s}
        return {
            "calendarRead":  any("calendars.read" in x for x in ls),
            "calendarWrite": "calendars.readwrite" in ls,
            "mailRead":      "mail.read" in ls or "mail.readwrite" in ls,
            "mailDraft":     "mail.readwrite" in ls,
            "mailSend":      "mail.send" in ls,
        }
    return {}


# Per-capability rows the Desktop UI renders as one card each.
# (capability_id, [capability flag keys it cares about])
_CAPABILITY_MAP: Dict[str, List[tuple]] = {
    "google": [
        ("google_calendar", ["calendarRead", "calendarWrite"]),
        ("gmail",           ["mailRead", "mailDraft", "mailSend"]),
    ],
    "microsoft": [
        ("microsoft_calendar", ["calendarRead", "calendarWrite"]),
        ("outlook_mail",       ["mailRead", "mailDraft", "mailSend"]),
    ],
}


def _capability_rows_for(uid: str, provider: str) -> List[IntegrationStatus]:
    """Build one ``IntegrationStatus`` per capability for a provider.

    Desktop spec §3.4 expects 4 cards total:
      - google → google_calendar, gmail
      - microsoft → microsoft_calendar, outlook_mail
    """
    rec = _store.load(uid, provider)
    if not rec:
        return [
            IntegrationStatus(
                id=cap_id,
                capability=cap_id,
                status="disconnected",
                provider=provider,  # type: ignore[arg-type]
                connected=False,
                scopes=[],
                capabilities={k: False for k in keys},
            )
            for cap_id, keys in _CAPABILITY_MAP.get(provider, [])
        ]

    # Storage key compatibility: integ_store.save_tokens persists ``scope``
    # as a space-separated string and ``accountEmail`` as the user's email.
    scopes_raw = rec.get("scopes") or rec.get("scope") or []
    if isinstance(scopes_raw, str):
        scopes = scopes_raw.split()
    else:
        scopes = list(scopes_raw)
    email = rec.get("email") or rec.get("accountEmail")
    full_caps = _capabilities_from_scopes(provider, scopes)
    health = rec.get("lastHealthCheckAt")
    if hasattr(health, "isoformat"):
        health = health.isoformat()
    last_error = rec.get("lastError")
    rows: List[IntegrationStatus] = []
    for cap_id, keys in _CAPABILITY_MAP.get(provider, []):
        cap_subset = {k: bool(full_caps.get(k, False)) for k in keys}
        # A capability is "connected" if the underlying provider has a
        # token AND at least one of this capability's flags is True.
        token_present = bool(rec.get("status") == "connected"
                             or rec.get("encryptedRefreshToken"))
        connected = token_present and any(cap_subset.values())
        rows.append(IntegrationStatus(
            id=cap_id,
            capability=cap_id,
            status="connected" if connected else "disconnected",
            provider=provider,  # type: ignore[arg-type]
            connected=connected,
            email=email,
            scopes=scopes,
            capabilities=cap_subset,
            lastHealthCheckAt=health,
            lastError=last_error,
        ))
    return rows


@router.get("", response_model=IntegrationsResponse)
def list_integrations(current_user: CurrentUser = Depends(get_current_user)):
    items: List[IntegrationStatus] = []
    for p in PROVIDERS:
        items.extend(_capability_rows_for(current_user.uid, p))
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

    # ── Google ────────────────────────────────────────────────────────
    # Fan out across the user's owned / writer calendars, not just
    # ``primary``. Many people put work meetings on a secondary calendar
    # (e.g. company.com) so a primary-only fetch returns 0 events even
    # though the user has plenty of upcoming meetings.
    #
    # [FIX 2026-05-12] Connection check uses the canonical token cipher
    # field names from store._migrate_legacy_google (accessTokenCipher /
    # refreshTokenCipher). The previous ``encryptedRefreshToken`` check
    # never matched any user's integration doc, so this entire branch
    # was a silent no-op — /v1/integrations/calendar/events always
    # returned an empty array, while the legacy
    # /integrations/google/calendar/events kept working because it
    # bypassed this gate and called google_client directly.
    google_creds = _store.load(current_user.uid, "google") or {}
    if google_creds.get("accessTokenCipher") or google_creds.get("refreshTokenCipher"):
        google_calendars: List[str] = ["primary"]
        try:
            cal_list = _g.list_calendar_list(current_user.uid)
            for c in (cal_list.get("items") or []):
                cid = c.get("id")
                role = c.get("accessRole") or ""
                if not cid:
                    continue
                # Skip the primary alias dup, holidays, birthdays, group
                # subscribed read-only calendars. Keep only calendars the
                # user actually owns or can write to.
                if c.get("primary"):
                    continue
                if role not in ("owner", "writer"):
                    continue
                google_calendars.append(cid)
        except Exception as e:
            logger.warning("[integrations.calendar.google] calendar_list failed: %s", e)

        google_count = 0
        for cid in google_calendars:
            try:
                res = _g.list_calendar_events(
                    current_user.uid, calendar_id=cid,
                    time_min=f, time_max=t, max_results=50,
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
                    google_count += 1
            except Exception as e:
                logger.warning("[integrations.calendar.google] fan-out failed for %s: %s", cid, e)
        logger.info(
            "[integrations.calendar.google] uid=%s fetched=%d calendars=%d range=%s..%s",
            current_user.uid, google_count, len(google_calendars), f, t,
        )

    # ── Microsoft ─────────────────────────────────────────────────────
    # ``/me/calendarView`` already aggregates across all calendars the
    # user has access to in Microsoft Graph, so single fetch is enough.
    # [FIX 2026-05-12] See Google branch comment — same token cipher
    # field name bug applied here too.
    ms_creds = _store.load(current_user.uid, "microsoft") or {}
    if ms_creds.get("accessTokenCipher") or ms_creds.get("refreshTokenCipher"):
        ms_count = 0
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
                ms_count += 1
        except Exception as e:
            logger.warning("[integrations.calendar.microsoft] fan-out failed: %s", e)
        logger.info(
            "[integrations.calendar.microsoft] uid=%s fetched=%d range=%s..%s",
            current_user.uid, ms_count, f, t,
        )

    items.sort(key=lambda e: e.startAt or "")
    # Cap total to keep response payloads bounded (50 google + 50 ms
    # was the implicit prior limit; keep the same effective ceiling).
    items = items[:100]
    return CalendarEventsResponse(items=items)
