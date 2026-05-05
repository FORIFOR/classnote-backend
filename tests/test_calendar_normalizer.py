"""Normalizer tests for calendar events."""
from app.services.normalizers import calendar as cn


def test_normalize_google_event_basic():
    ev = {
        "id": "g1",
        "summary": "Standup",
        "description": "daily",
        "location": "Zoom",
        "start": {"dateTime": "2026-05-01T09:00:00+09:00", "timeZone": "Asia/Tokyo"},
        "end": {"dateTime": "2026-05-01T09:15:00+09:00", "timeZone": "Asia/Tokyo"},
        "attendees": [
            {"email": "alice@example.com", "displayName": "Alice", "responseStatus": "accepted", "self": True},
            {"email": "bob@example.com", "responseStatus": "needsAction"},
        ],
        "organizer": {"email": "alice@example.com", "displayName": "Alice"},
        "htmlLink": "https://calendar.google.com/...",
        "status": "confirmed",
    }
    out = cn.normalize_google_event(ev, calendar_id="primary")
    assert out["provider"] == "google"
    assert out["summary"] == "Standup"
    assert out["isAllDay"] is False
    assert out["start"]["dateTime"].startswith("2026-05-01")
    assert len(out["attendees"]) == 2
    assert out["attendees"][0]["email"] == "alice@example.com"
    assert out["attendees"][0]["self"] is True
    assert out["organizer"]["email"] == "alice@example.com"


def test_normalize_google_event_all_day():
    ev = {"id": "g2", "summary": "Holiday", "start": {"date": "2026-05-03"}, "end": {"date": "2026-05-04"}}
    out = cn.normalize_google_event(ev)
    assert out["isAllDay"] is True
    assert out["start"]["date"] == "2026-05-03"


def test_normalize_microsoft_event_basic():
    ev = {
        "id": "m1",
        "subject": "Sync",
        "bodyPreview": "let's sync",
        "location": {"displayName": "Teams"},
        "start": {"dateTime": "2026-05-01T00:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-01T00:30:00.0000000", "timeZone": "UTC"},
        "attendees": [
            {"emailAddress": {"address": "x@example.com", "name": "X"},
             "status": {"response": "accepted"}, "type": "required"},
            {"emailAddress": {"address": "y@example.com", "name": "Y"},
             "status": {"response": "none"}, "type": "optional"},
        ],
        "organizer": {"emailAddress": {"address": "x@example.com", "name": "X"}},
        "webLink": "https://outlook...",
        "isAllDay": False,
        "showAs": "busy",
    }
    out = cn.normalize_microsoft_event(ev)
    assert out["provider"] == "microsoft"
    assert out["summary"] == "Sync"
    assert out["location"] == "Teams"
    assert len(out["attendees"]) == 2
    assert out["attendees"][1]["optional"] is True
    assert out["organizer"]["email"] == "x@example.com"
    assert out["status"] == "busy"
