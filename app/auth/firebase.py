from dataclasses import dataclass
from fastapi import HTTPException
import firebase_admin
from firebase_admin import auth as fb_auth

# [FIX] Ensure timedelta is available if needed, though this file mainly uses firebase_admin
from datetime import timedelta

@dataclass
class CurrentUser:
    uid: str
    provider: str | None
    phone_number: str | None
    email: str | None

def verify_firebase_id_token(id_token: str) -> dict:
    try:
        # check_revoked=False for speed, or True for security (user preference seems to be standard verify)
        return fb_auth.verify_id_token(id_token, check_revoked=False)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Could not validate credentials: {e}")

def current_user_from_claims(claims: dict) -> CurrentUser:
    fb = claims.get("firebase", {}) or {}
    provider = fb.get("sign_in_provider")
    return CurrentUser(
        uid=claims["uid"],
        provider=provider,
        phone_number=claims.get("phone_number"),
        email=claims.get("email"),
    )
