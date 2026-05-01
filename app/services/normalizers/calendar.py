"""Unified Calendar event schema across providers.

Returned shape:
{
  "id": str,
  "provider": "google" | "microsoft",
  "calendarId": str | None,
  "summary": str,
  "description": str | None,
  "location": str | None,
  "start": {"dateTime": iso8601 | None, "date": iso8601 | None, "timeZone": str | None},
  "end":   {"dateTime": iso8601 | None, "date": iso8601 | None, "timeZone": str | None},
  "attendees": [{"email": str, "name": str | None, "status": str | None}],
  "organizer": {"email": str, "name": str | None} | None,
  "htmlLink": str | None,
  "isAllDay": bool,
  "status": str | None,
  "raw": {... small subset for debug},
}
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _norm_attendee_google(a: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "email": a.get("email"),
        "name": a.get("displayName"),
        "status": a.get("responseStatus"),
        "self": bool(a.get("self")),
        "optional": bool(a.get("optional")),
    }


def _norm_attendee_msft(a: Dict[str, Any]) -> Dict[str, Any]:
    email = (a.get("emailAddress") or {}).get("address")
    name = (a.get("emailAddress") or {}).get("name")
    return {
        "email": email,
        "name": name,
        "status": (a.get("status") or {}).get("response"),
        "self": False,
        "optional": (a.get("type") == "optional"),
    }


def normalize_google_event(ev: Dict[str, Any], *, calendar_id: Optional[str] = None) -> Dict[str, Any]:
    start = ev.get("start") or {}
    end = ev.get("end") or {}
    is_all_day = "date" in start and "dateTime" not in start
    return {
        "id": ev.get("id"),
        "provider": "google",
        "calendarId": calendar_id or "primary",
        "summary": ev.get("summary") or "(no title)",
        "description": ev.get("description"),
        "location": ev.get("location"),
        "start": {
            "dateTime": start.get("dateTime"),
            "date": start.get("date"),
            "timeZone": start.get("timeZone"),
        },
        "end": {
            "dateTime": end.get("dateTime"),
            "date": end.get("date"),
            "timeZone": end.get("timeZone"),
        },
        "attendees": [_norm_attendee_google(a) for a in (ev.get("attendees") or [])],
        "organizer": (
            {"email": (ev.get("organizer") or {}).get("email"),
             "name": (ev.get("organizer") or {}).get("displayName")}
            if ev.get("organizer") else None
        ),
        "htmlLink": ev.get("htmlLink"),
        "isAllDay": is_all_day,
        "status": ev.get("status"),
    }


def normalize_microsoft_event(ev: Dict[str, Any]) -> Dict[str, Any]:
    start = ev.get("start") or {}
    end = ev.get("end") or {}
    organizer = ev.get("organizer") or {}
    org_email = (organizer.get("emailAddress") or {}).get("address")
    org_name = (organizer.get("emailAddress") or {}).get("name")
    return {
        "id": ev.get("id"),
        "provider": "microsoft",
        "calendarId": None,
        "summary": ev.get("subject") or "(no title)",
        "description": (ev.get("bodyPreview") or None),
        "location": (ev.get("location") or {}).get("displayName"),
        "start": {
            "dateTime": start.get("dateTime"),
            "date": None,
            "timeZone": start.get("timeZone"),
        },
        "end": {
            "dateTime": end.get("dateTime"),
            "date": None,
            "timeZone": end.get("timeZone"),
        },
        "attendees": [_norm_attendee_msft(a) for a in (ev.get("attendees") or [])],
        "organizer": ({"email": org_email, "name": org_name} if org_email else None),
        "htmlLink": ev.get("webLink"),
        "isAllDay": bool(ev.get("isAllDay")),
        "status": ev.get("showAs") or ev.get("responseStatus", {}).get("response"),
    }


def normalize_google_events(items: List[Dict[str, Any]], *, calendar_id: Optional[str] = None) -> List[Dict[str, Any]]:
    return [normalize_google_event(ev, calendar_id=calendar_id) for ev in (items or [])]


def normalize_microsoft_events(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [normalize_microsoft_event(ev) for ev in (items or [])]
