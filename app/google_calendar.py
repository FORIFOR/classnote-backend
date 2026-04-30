import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import requests

from app.firebase import db

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8000/google/oauth/callback")
GOOGLE_SCOPES = os.environ.get(
    "GOOGLE_OAUTH_SCOPES",
    "openid email profile https://www.googleapis.com/auth/calendar.events.readonly",
)


def _get_state_secret() -> bytes:
    # state 用のシークレットは専用環境変数があればそれを、なければ client_secret を使う
    secret = os.environ.get("GOOGLE_OAUTH_STATE_SECRET") or GOOGLE_CLIENT_SECRET
    if not secret:
        raise RuntimeError("GOOGLE_OAUTH_STATE_SECRET or GOOGLE_OAUTH_CLIENT_SECRET must be set")
    return secret.encode("utf-8")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _sign_state(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    sig = hmac.new(_get_state_secret(), raw, hashlib.sha256).digest()
    return f"{_b64url(raw)}.{_b64url(sig)}"


def _verify_state(state: str, max_age_seconds: int = 600) -> dict:
    try:
        raw_part, sig_part = state.split(".", 1)
        raw = base64.urlsafe_b64decode(raw_part + "==")
        sig = base64.urlsafe_b64decode(sig_part + "==")
    except Exception:
        raise ValueError("Invalid state format")

    expected = hmac.new(_get_state_secret(), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("Invalid state signature")

    payload = json.loads(raw.decode())
    ts = payload.get("ts")
    if ts is None:
        raise ValueError("Invalid state payload")
    now = datetime.now(timezone.utc).timestamp()
    if now - ts > max_age_seconds:
        raise ValueError("State expired")
    return payload


def save_tokens(uid: str, access_token: str, refresh_token: Optional[str], expires_in: int):
    # 既存 refresh_token があれば維持（Google は再認可時に返さない場合がある）
    existing = load_tokens(uid) or {}
    if not refresh_token:
        refresh_token = existing.get("refreshToken")
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 30)
    update = {
        "googleCalendarTokens": {
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "expiresAt": expires_at,
        }
    }
    db.collection("users").document(uid).set(update, merge=True)


def load_tokens(uid: str) -> Optional[dict]:
    doc = db.collection("users").document(uid).get()
    if not doc.exists:
        return None
    tokens = (doc.to_dict() or {}).get("googleCalendarTokens")
    if not tokens:
        return None
    return tokens


def refresh_access_token(tokens: dict, uid: str) -> Tuple[Optional[str], Optional[datetime]]:
    """
    トークン期限が近い場合は refresh_token で更新する。
    """
    expires_at = tokens.get("expiresAt")
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at)
        except Exception:
            expires_at = None
    refresh_token = tokens.get("refreshToken")
    access_token = tokens.get("accessToken")

    if not refresh_token:
        return access_token, expires_at

    if expires_at and isinstance(expires_at, datetime):
        if expires_at > datetime.now(timezone.utc) + timedelta(seconds=60):
            return access_token, expires_at

    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise RuntimeError("Google OAuth client is not configured")

    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=10,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to refresh token: {resp.text}")

    data = resp.json()
    new_access = data.get("access_token")
    expires_in = data.get("expires_in", 3600)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 30)

    db.collection("users").document(uid).set({
        "googleCalendarTokens": {
            "accessToken": new_access,
            "refreshToken": refresh_token,
            "expiresAt": expires_at,
        }
    }, merge=True)

    return new_access, expires_at


def delete_tokens(uid: str) -> bool:
    """
    Google 連携を解除する。Firestore 上の googleCalendarTokens を削除して
    disconnect 状態にする。削除前にトークンが存在していれば True、すでに未接続なら False。
    """
    from google.cloud.firestore_v1 import DELETE_FIELD

    existed = load_tokens(uid) is not None
    db.collection("users").document(uid).set(
        {"googleCalendarTokens": DELETE_FIELD},
        merge=True,
    )
    return existed


def list_events(
    uid: str,
    start: datetime,
    end: datetime,
    top: int = 50,
    calendar_id: str = "primary",
) -> list[dict]:
    """
    Google Calendar の予定を startTime 昇順で最大 top 件取得する。
    レスポンスは Microsoft 版 list_events と同等のスキーマに正規化する。
    """
    tokens = load_tokens(uid)
    if not tokens:
        raise RuntimeError("Google Calendar not connected for this user")

    access_token, _ = refresh_access_token(tokens, uid)
    if not access_token:
        raise RuntimeError("Google Calendar access token unavailable")

    headers = {"Authorization": f"Bearer {access_token}"}
    params = {
        "timeMin": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "timeMax": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": str(max(1, min(top, 250))),
    }
    resp = requests.get(
        f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
        headers=headers,
        params=params,
        timeout=10,
    )
    if resp.status_code == 401:
        raise RuntimeError("Google Calendar access denied (token may be revoked)")
    if resp.status_code != 200:
        raise RuntimeError(
            f"Failed to list calendar events: {resp.status_code} {resp.text}"
        )

    items = resp.json().get("items", []) or []
    out: list[dict] = []
    for ev in items:
        start_obj = ev.get("start") or {}
        end_obj = ev.get("end") or {}
        attendees = [
            {
                "email": a.get("email"),
                "displayName": a.get("displayName"),
                "optional": bool(a.get("optional")),
                "responseStatus": a.get("responseStatus"),
            }
            for a in (ev.get("attendees") or [])
            if a.get("email")
        ]
        organizer = ev.get("organizer") or {}
        conference = ev.get("conferenceData") or {}
        meet_url = None
        for ep in conference.get("entryPoints", []) or []:
            if ep.get("entryPointType") == "video" and ep.get("uri"):
                meet_url = ep["uri"]
                break
        if not meet_url and ev.get("hangoutLink"):
            meet_url = ev["hangoutLink"]

        out.append({
            "id": ev.get("id"),
            "title": ev.get("summary"),
            "description": ev.get("description"),
            "start": start_obj.get("dateTime") or start_obj.get("date"),
            "end": end_obj.get("dateTime") or end_obj.get("date"),
            "isAllDay": "date" in start_obj and "dateTime" not in start_obj,
            "location": ev.get("location"),
            "htmlLink": ev.get("htmlLink"),
            "meetUrl": meet_url,
            "organizer": {
                "email": organizer.get("email"),
                "displayName": organizer.get("displayName"),
            } if organizer else None,
            "attendees": attendees,
            "status": ev.get("status"),
        })
    return out


def create_event(
    uid: str,
    title: str,
    description: str,
    start_at: datetime,
    end_at: datetime,
    calendar_id: str = "primary",
) -> str:
    """
    Google カレンダーに予定を作成する。成功したら eventId を返す。
    """
    tokens = load_tokens(uid)
    if not tokens:
        raise RuntimeError("Google Calendar not connected for this user")

    access_token, _ = refresh_access_token(tokens, uid)
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    body = {
        "summary": title,
        "description": description,
        "start": {"dateTime": start_at.astimezone(timezone.utc).isoformat()},
        "end": {"dateTime": end_at.astimezone(timezone.utc).isoformat()},
    }

    resp = requests.post(
        f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
        headers=headers,
        data=json.dumps(body),
        timeout=10,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Failed to create calendar event: {resp.status_code} {resp.text}")
    return resp.json().get("id")
