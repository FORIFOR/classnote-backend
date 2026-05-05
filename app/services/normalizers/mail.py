"""Unified Mail message schema across providers.

Returned shape:
{
  "id": str,
  "provider": "google" | "microsoft",
  "subject": str,
  "snippet": str | None,
  "from": {"email": str, "name": str | None} | None,
  "to": [{"email": str, "name": str | None}],
  "cc": [{"email": str, "name": str | None}],
  "receivedAt": iso8601 str | None,
  "isRead": bool | None,
  "webLink": str | None,
  "labels": [str],
}
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

_EMAIL_RE = re.compile(r"<([^>]+)>")


def _split_email(value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not value:
        return None, None
    match = _EMAIL_RE.search(value)
    if match:
        email = match.group(1).strip()
        name = value.split("<")[0].strip().strip('"') or None
        return email, name
    if "@" in value:
        return value.strip(), None
    return None, value.strip() or None


def _gmail_headers_to_map(headers: List[Dict[str, str]]) -> Dict[str, str]:
    return {h.get("name", "").lower(): h.get("value", "") for h in headers or []}


def _split_emails(field: Optional[str]) -> List[Dict[str, Optional[str]]]:
    if not field:
        return []
    parts = [p.strip() for p in re.split(r",(?![^<]*>)", field) if p.strip()]
    out = []
    for p in parts:
        email, name = _split_email(p)
        if email:
            out.append({"email": email, "name": name})
    return out


def normalize_gmail_message(msg: Dict[str, Any]) -> Dict[str, Any]:
    payload = msg.get("payload") or {}
    headers = _gmail_headers_to_map(payload.get("headers") or [])
    from_email, from_name = _split_email(headers.get("from"))
    return {
        "id": msg.get("id"),
        "provider": "google",
        "subject": headers.get("subject") or "(no subject)",
        "snippet": msg.get("snippet"),
        "from": ({"email": from_email, "name": from_name} if from_email else None),
        "to": _split_emails(headers.get("to")),
        "cc": _split_emails(headers.get("cc")),
        "receivedAt": headers.get("date"),
        "isRead": ("UNREAD" not in (msg.get("labelIds") or [])),
        "webLink": None,
        "labels": list(msg.get("labelIds") or []),
    }


def normalize_microsoft_message(msg: Dict[str, Any]) -> Dict[str, Any]:
    sender = (msg.get("from") or {}).get("emailAddress") or {}
    return {
        "id": msg.get("id"),
        "provider": "microsoft",
        "subject": msg.get("subject") or "(no subject)",
        "snippet": msg.get("bodyPreview"),
        "from": ({"email": sender.get("address"), "name": sender.get("name")} if sender.get("address") else None),
        "to": [
            {"email": (r.get("emailAddress") or {}).get("address"),
             "name": (r.get("emailAddress") or {}).get("name")}
            for r in (msg.get("toRecipients") or [])
            if (r.get("emailAddress") or {}).get("address")
        ],
        "cc": [
            {"email": (r.get("emailAddress") or {}).get("address"),
             "name": (r.get("emailAddress") or {}).get("name")}
            for r in (msg.get("ccRecipients") or [])
            if (r.get("emailAddress") or {}).get("address")
        ],
        "receivedAt": msg.get("receivedDateTime"),
        "isRead": msg.get("isRead"),
        "webLink": msg.get("webLink"),
        "labels": [],
    }
