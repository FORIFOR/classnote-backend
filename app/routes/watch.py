"""
Watch Authentication Routes

Provides token-based authentication for Apple Watch companion app.
Firebase Auth SDK is not available on watchOS, so we use a lightweight
opaque-token + JWT approach:

Flow:
  iPhone (Firebase Auth) → POST /watch/pair → watchPairToken
  iPhone → WCSession → Watch
  Watch → POST /watch/exchange (pairToken) → accessToken + refreshToken
  Watch → API calls with Bearer accessToken
  Watch → POST /watch/refresh (refreshToken) → new accessToken
"""

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel
from app.dependencies import get_current_user, CurrentUser
from app.firebase import db
from datetime import datetime, timezone, timedelta
import secrets
import hashlib
import os
import logging

from jose import jwt, JWTError

router = APIRouter()
logger = logging.getLogger("app.watch")

WATCH_TOKEN_SECRET = os.environ.get("WATCH_TOKEN_SECRET", "dev-watch-secret-do-not-use-in-prod")
ACCESS_TOKEN_TTL = timedelta(hours=1)
REFRESH_TOKEN_TTL = timedelta(days=90)
PAIR_TOKEN_TTL = timedelta(days=90)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _generate_access_token(account_id: str, uid: str) -> tuple[str, datetime]:
    """Generate a signed JWT access token for Watch."""
    now = datetime.now(timezone.utc)
    expires_at = now + ACCESS_TOKEN_TTL
    payload = {
        "sub": account_id,
        "uid": uid,
        "type": "watch_access",
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, WATCH_TOKEN_SECRET, algorithm="HS256")
    return token, expires_at


# ── Models ──


class WatchPairResponse(BaseModel):
    watchPairToken: str
    expiresAt: str  # ISO8601


class WatchExchangeRequest(BaseModel):
    watchPairToken: str
    deviceName: str | None = None


class WatchExchangeResponse(BaseModel):
    accessToken: str
    refreshToken: str
    accessTokenExpiresAt: str
    accountId: str


class WatchRefreshRequest(BaseModel):
    refreshToken: str


class WatchRefreshResponse(BaseModel):
    accessToken: str
    accessTokenExpiresAt: str


# ── Endpoints ──


@router.post("/watch/pair", response_model=WatchPairResponse)
async def create_watch_pair_token(
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    iPhone calls this after Firebase authentication.
    Returns an opaque pairing token to send to Watch via WCSession.
    """
    raw_token = secrets.token_urlsafe(64)
    token_hash = _sha256(raw_token)
    now = datetime.now(timezone.utc)
    expires_at = now + PAIR_TOKEN_TTL

    db.collection("watch_pair_tokens").document(token_hash).set({
        "accountId": current_user.account_id,
        "uid": current_user.uid,
        "createdAt": now,
        "expiresAt": expires_at,
        "exchanged": False,
    })

    logger.info(f"[/watch/pair] Created pair token for account={current_user.account_id}")

    return WatchPairResponse(
        watchPairToken=raw_token,
        expiresAt=expires_at.isoformat(),
    )


@router.post("/watch/exchange", response_model=WatchExchangeResponse)
async def exchange_watch_token(req: WatchExchangeRequest):
    """
    Watch calls this with the pairing token received from iPhone.
    Returns JWT access token + opaque refresh token.
    One-time use: the pairing token is marked as exchanged after use.
    """
    token_hash = _sha256(req.watchPairToken)
    doc_ref = db.collection("watch_pair_tokens").document(token_hash)
    doc = doc_ref.get()

    if not doc.exists:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid pairing token",
        )

    data = doc.to_dict()
    now = datetime.now(timezone.utc)

    # Validate token
    if data.get("exchanged"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Pairing token already used",
        )
    if data.get("revokedAt"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Pairing token revoked",
        )
    expires_at = data.get("expiresAt")
    if expires_at and expires_at.replace(tzinfo=timezone.utc) < now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Pairing token expired",
        )

    account_id = data["accountId"]
    uid = data["uid"]

    # Mark as exchanged
    doc_ref.update({
        "exchanged": True,
        "exchangedAt": now,
        "deviceName": req.deviceName,
    })

    # Generate access token (JWT)
    access_token, access_expires = _generate_access_token(account_id, uid)

    # Generate refresh token (opaque)
    raw_refresh = secrets.token_urlsafe(64)
    refresh_hash = _sha256(raw_refresh)
    refresh_expires = now + REFRESH_TOKEN_TTL

    db.collection("watch_refresh_tokens").document(refresh_hash).set({
        "accountId": account_id,
        "uid": uid,
        "createdAt": now,
        "expiresAt": refresh_expires,
    })

    logger.info(f"[/watch/exchange] Token exchanged for account={account_id}")

    return WatchExchangeResponse(
        accessToken=access_token,
        refreshToken=raw_refresh,
        accessTokenExpiresAt=access_expires.isoformat(),
        accountId=account_id,
    )


@router.post("/watch/refresh", response_model=WatchRefreshResponse)
async def refresh_watch_token(req: WatchRefreshRequest):
    """
    Watch calls this when access token expires.
    Returns a new access token using the refresh token.
    """
    token_hash = _sha256(req.refreshToken)
    doc_ref = db.collection("watch_refresh_tokens").document(token_hash)
    doc = doc_ref.get()

    if not doc.exists:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    data = doc.to_dict()
    now = datetime.now(timezone.utc)

    if data.get("revokedAt"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token revoked",
        )
    expires_at = data.get("expiresAt")
    if expires_at and expires_at.replace(tzinfo=timezone.utc) < now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token expired",
        )

    account_id = data["accountId"]
    uid = data["uid"]

    access_token, access_expires = _generate_access_token(account_id, uid)

    logger.info(f"[/watch/refresh] Access token refreshed for account={account_id}")

    return WatchRefreshResponse(
        accessToken=access_token,
        accessTokenExpiresAt=access_expires.isoformat(),
    )


@router.post("/watch/revoke")
async def revoke_watch_tokens(
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    iPhone calls this on logout.
    Revokes all watch pair tokens and refresh tokens for the account.
    """
    account_id = current_user.account_id
    now = datetime.now(timezone.utc)
    revoked_count = 0

    # Revoke pair tokens
    pair_docs = db.collection("watch_pair_tokens").where(
        "accountId", "==", account_id
    ).stream()
    for doc in pair_docs:
        doc.reference.update({"revokedAt": now})
        revoked_count += 1

    # Revoke refresh tokens
    refresh_docs = db.collection("watch_refresh_tokens").where(
        "accountId", "==", account_id
    ).stream()
    for doc in refresh_docs:
        doc.reference.update({"revokedAt": now})
        revoked_count += 1

    logger.info(f"[/watch/revoke] Revoked {revoked_count} tokens for account={account_id}")

    return {"revoked": revoked_count}
