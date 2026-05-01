from app.services.normalizers import mail as mn


def test_normalize_gmail_metadata():
    msg = {
        "id": "abc",
        "snippet": "Hello",
        "labelIds": ["INBOX", "UNREAD"],
        "payload": {"headers": [
            {"name": "From", "value": "Alice <alice@example.com>"},
            {"name": "To", "value": "bob@example.com, Charlie <c@example.com>"},
            {"name": "Subject", "value": "Hi"},
            {"name": "Date", "value": "Thu, 01 May 2026 09:00:00 +0900"},
        ]},
    }
    out = mn.normalize_gmail_message(msg)
    assert out["provider"] == "google"
    assert out["subject"] == "Hi"
    assert out["from"] == {"email": "alice@example.com", "name": "Alice"}
    assert {r["email"] for r in out["to"]} == {"bob@example.com", "c@example.com"}
    assert out["isRead"] is False
    assert "INBOX" in out["labels"]


def test_normalize_microsoft_message():
    msg = {
        "id": "m1",
        "subject": "Welcome",
        "bodyPreview": "thanks",
        "from": {"emailAddress": {"address": "x@y.com", "name": "X"}},
        "toRecipients": [{"emailAddress": {"address": "me@y.com", "name": "Me"}}],
        "ccRecipients": [],
        "receivedDateTime": "2026-05-01T00:00:00Z",
        "isRead": True,
        "webLink": "https://outlook...",
    }
    out = mn.normalize_microsoft_message(msg)
    assert out["provider"] == "microsoft"
    assert out["subject"] == "Welcome"
    assert out["from"]["email"] == "x@y.com"
    assert out["to"][0]["email"] == "me@y.com"
    assert out["isRead"] is True
    assert out["webLink"].startswith("https://")
