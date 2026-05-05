"""Slack Web API client + signature verification + workspace token store.

Reads:
  SLACK_CLIENT_ID
  SLACK_CLIENT_SECRET
  SLACK_SIGNING_SECRET           (X-Slack-Signature V0)
  SLACK_OAUTH_REDIRECT_URI       (default: dev URL)
  SLACK_OAUTH_SCOPES             (default: chat:write,im:history,im:read,
                                  app_mentions:read,users:read,commands)
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Any, Dict, Optional

import requests

from app.firebase import db
from app.services import token_crypto

logger = logging.getLogger("app.services.integrations.slack")

WORKSPACES_COLLECTION = "slack_workspaces"

SLACK_AUTH_URL = "https://slack.com/oauth/v2/authorize"
SLACK_OAUTH_ACCESS_URL = "https://slack.com/api/oauth.v2.access"
SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"

DEFAULT_SCOPES = ",".join([
    "app_mentions:read",
    "chat:write",
    "im:history",
    "im:read",
    "im:write",
    "users:read",
])

CLIENT_ID = os.environ.get("SLACK_CLIENT_ID")
CLIENT_SECRET = os.environ.get("SLACK_CLIENT_SECRET")
SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")
REDIRECT_URI = os.environ.get(
    "SLACK_OAUTH_REDIRECT_URI",
    "http://localhost:8000/integrations/slack/oauth/callback",
)
SCOPES = os.environ.get("SLACK_OAUTH_SCOPES", DEFAULT_SCOPES)

# Slack signature replay-protection window (5 minutes per Slack docs).
SIGNATURE_TIMESTAMP_TOLERANCE = 60 * 5


class SlackAuthError(RuntimeError):
    pass


class SlackApiError(RuntimeError):
    def __init__(self, error: str, payload: Optional[Dict[str, Any]] = None):
        super().__init__(f"slack_api: {error}")
        self.error = error
        self.payload = payload or {}


def is_configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET and SIGNING_SECRET)


def signing_secret() -> str:
    return SIGNING_SECRET or ""


# ──────────────────────────────────────────────────────────────────────
# Signature verification (V0)
# ──────────────────────────────────────────────────────────────────────

def verify_signature(*, body: bytes, timestamp: str, signature: str) -> bool:
    if not signing_secret() or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts) > SIGNATURE_TIMESTAMP_TOLERANCE:
        return False
    base = f"v0:{timestamp}:".encode("utf-8") + body
    digest = hmac.new(signing_secret().encode("utf-8"), base, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature)


# ──────────────────────────────────────────────────────────────────────
# OAuth code exchange + workspace token persistence
# ──────────────────────────────────────────────────────────────────────

def exchange_code(code: str) -> Dict[str, Any]:
    if not is_configured():
        raise SlackAuthError("slack_oauth_not_configured")
    resp = requests.post(
        SLACK_OAUTH_ACCESS_URL,
        data={
            "code": code,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise SlackAuthError(f"oauth_http_{resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    if not body.get("ok"):
        raise SlackAuthError(f"oauth_failed:{body.get('error')}")
    return body


def save_workspace(payload: Dict[str, Any], *, installed_by_uid: Optional[str] = None) -> str:
    """Persist the bot token (encrypted) for a workspace and return team_id."""
    team = payload.get("team") or {}
    team_id = team.get("id") or payload.get("team_id")
    if not team_id:
        raise SlackAuthError("missing_team_id")
    bot_token = payload.get("access_token")
    if not bot_token:
        raise SlackAuthError("missing_access_token")
    if not token_crypto.is_configured():
        raise SlackAuthError("token_crypto_not_configured")

    record = {
        "teamId": team_id,
        "teamName": team.get("name"),
        "botUserId": payload.get("bot_user_id"),
        "scope": payload.get("scope"),
        "tokenType": payload.get("token_type", "bot"),
        "accessTokenCipher": token_crypto.encrypt(bot_token),
        "installedAt": _now(),
        "installedByUid": installed_by_uid,
    }
    db.collection(WORKSPACES_COLLECTION).document(team_id).set(record, merge=True)
    return team_id


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def get_bot_token(team_id: str) -> Optional[str]:
    if not team_id:
        return None
    snap = db.collection(WORKSPACES_COLLECTION).document(team_id).get()
    if not snap.exists:
        return None
    data = snap.to_dict() or {}
    cipher = data.get("accessTokenCipher")
    if not cipher:
        return None
    try:
        return token_crypto.decrypt(cipher)
    except Exception as e:
        logger.warning("[slack] decrypt bot token failed for team=%s: %s", team_id, e)
        return None


# ──────────────────────────────────────────────────────────────────────
# chat.postMessage
# ──────────────────────────────────────────────────────────────────────

def post_message(*, team_id: str, channel: str, text: str, thread_ts: Optional[str] = None) -> None:
    """Send a plain-text message back to the user. Truncated to 3500 chars."""
    if not channel or not text:
        return
    bot_token = get_bot_token(team_id)
    if not bot_token:
        logger.warning("[slack] no bot token for team=%s; skipping post", team_id)
        return
    if len(text) > 3500:
        text = text[:3497] + "..."
    payload: Dict[str, Any] = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    resp = requests.post(
        SLACK_POST_MESSAGE_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        timeout=10,
    )
    if resp.status_code != 200:
        logger.warning("[slack] post_message http=%s body=%s", resp.status_code, resp.text[:200])
        return
    body = resp.json()
    if not body.get("ok"):
        logger.warning("[slack] post_message error=%s", body.get("error"))
