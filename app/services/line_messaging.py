"""LINE Messaging API client + signature verification.

Reads:
  LINE_MESSAGING_CHANNEL_SECRET        (HMAC-SHA256 secret for X-Line-Signature)
  LINE_MESSAGING_CHANNEL_ACCESS_TOKEN  (Bearer for reply / push)

Provides:
  is_configured()         True if both secrets are present
  verify_signature(...)   Validate webhook payload against header
  reply(reply_token, msgs)  POST /v2/bot/message/reply
  push(to, msgs)          POST /v2/bot/message/push
  text_message(text)      Helper to build a single text message dict
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from typing import Any, Dict, List

import requests

logger = logging.getLogger("app.services.line_messaging")

REPLY_URL = "https://api.line.me/v2/bot/message/reply"
PUSH_URL = "https://api.line.me/v2/bot/message/push"

_CHANNEL_SECRET_ENV = "LINE_MESSAGING_CHANNEL_SECRET"
_ACCESS_TOKEN_ENV = "LINE_MESSAGING_CHANNEL_ACCESS_TOKEN"


def _channel_secret() -> str:
    return os.environ.get(_CHANNEL_SECRET_ENV, "")


def _access_token() -> str:
    return os.environ.get(_ACCESS_TOKEN_ENV, "")


def is_configured() -> bool:
    return bool(_channel_secret() and _access_token())


def verify_signature(*, body: bytes, header_signature: str) -> bool:
    """Compare base64(HMAC-SHA256(channel_secret, body)) against header.

    Returns False (never raises) for any mismatch / missing config.
    """
    secret = _channel_secret()
    if not secret or not header_signature:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, header_signature)


def confirm_template_message(*, alt_text: str, prompt: str,
                              yes_label: str, yes_data: str,
                              no_label: str, no_data: str) -> Dict[str, Any]:
    """LINE template confirm message with two postback actions.
    Used by the in-group Lv3 share-confirm card."""
    return {
        "type": "template",
        "altText": (alt_text or "")[:400],
        "template": {
            "type": "confirm",
            "text": (prompt or "")[:240],
            "actions": [
                {"type": "postback", "label": (yes_label or "OK")[:20], "data": (yes_data or "")[:300]},
                {"type": "postback", "label": (no_label or "Cancel")[:20], "data": (no_data or "")[:300]},
            ],
        },
    }


def text_message(text: str) -> Dict[str, Any]:
    """Build a single text message body. LINE caps text at 5000 chars."""
    if text is None:
        text = ""
    if len(text) > 5000:
        text = text[:4997] + "..."
    return {"type": "text", "text": text}


def _post(url: str, payload: Dict[str, Any]) -> None:
    token = _access_token()
    if not token:
        logger.warning("[line_messaging] access token not configured; skipping POST %s", url)
        return
    resp = requests.post(
        url,
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=10,
    )
    if resp.status_code >= 400:
        logger.warning("[line_messaging] %s -> %s: %s", url, resp.status_code, resp.text[:200])


def reply(reply_token: str, messages: List[Dict[str, Any]]) -> None:
    """Send up to 5 messages in a single reply. Idempotent per reply_token."""
    if not reply_token or not messages:
        return
    _post(REPLY_URL, {"replyToken": reply_token, "messages": messages[:5]})


def push(to: str, messages: List[Dict[str, Any]]) -> None:
    """Push up to 5 messages to a userId / groupId / roomId."""
    if not to or not messages:
        return
    _post(PUSH_URL, {"to": to, "messages": messages[:5]})
