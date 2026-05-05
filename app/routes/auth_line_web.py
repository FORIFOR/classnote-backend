"""
LINE Login relay for Web apps (Billing UI).

Flow:
1. Web UI opens → GET /auth/line/web?redirect={billing_url}
2. This endpoint redirects to LINE's OAuth authorization page
3. User authenticates with LINE
4. LINE redirects to GET /auth/line/web/callback?code={code}&state={redirect_url}
5. We exchange the code for tokens, verify id_token, create Firebase custom token
6. Redirect to {redirect_url}?lineToken={firebaseCustomToken}
7. Web UI calls signInWithCustomToken(auth, lineToken)
"""

import os
import logging
from urllib.parse import urlencode, quote

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from firebase_admin import auth as fb_auth

logger = logging.getLogger("app.auth.line_web")

router = APIRouter()

LINE_AUTH_URL = "https://access.line.me/oauth2/v2.1/authorize"
LINE_TOKEN_URL = "https://api.line.me/oauth2/v2.1/token"
LINE_VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"

LINE_WEB_CALLBACK_URI = "https://deepnote-api-900324644592.asia-northeast1.run.app/auth/line/web/callback"

# Allowed redirect origins (prevent open redirect)
ALLOWED_ORIGINS = [
    "https://deepnote-billing-ui.vercel.app",
    "http://localhost:3000",
    "http://localhost:3001",
]


def _is_allowed_redirect(url: str) -> bool:
    return any(url.startswith(origin) for origin in ALLOWED_ORIGINS)


@router.get("/auth/line/web")
async def line_web_start(
    redirect: str = Query(..., description="URL to redirect after login"),
):
    """Initiate LINE Login for a web app."""
    LINE_CLIENT_ID = os.environ.get("LINE_CHANNEL_ID")
    if not LINE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="LINE_CHANNEL_ID not configured")

    if not _is_allowed_redirect(redirect):
        raise HTTPException(status_code=400, detail="Invalid redirect URL")

    params = {
        "response_type": "code",
        "client_id": LINE_CLIENT_ID,
        "redirect_uri": LINE_WEB_CALLBACK_URI,
        "state": redirect,  # Pass redirect URL as state
        "scope": "profile openid",
    }

    auth_url = f"{LINE_AUTH_URL}?{urlencode(params)}"
    logger.info("[LINE Web] Redirecting to LINE OAuth (redirect=%s)", redirect)
    return RedirectResponse(auth_url)


@router.get("/auth/line/web/callback")
async def line_web_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    error_description: str = Query(None),
):
    """Handle LINE's OAuth callback for web apps."""
    redirect_url = state or "https://deepnote-billing-ui.vercel.app/login"

    if not _is_allowed_redirect(redirect_url):
        redirect_url = "https://deepnote-billing-ui.vercel.app/login"

    # Handle errors
    if error:
        logger.warning("[LINE Web] LINE returned error: %s (%s)", error, error_description)
        return RedirectResponse(f"{redirect_url}?lineError={quote(error_description or error)}")

    if not code:
        return RedirectResponse(f"{redirect_url}?lineError=no_code")

    LINE_CLIENT_ID = os.environ.get("LINE_CHANNEL_ID")
    LINE_CLIENT_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
    if not LINE_CLIENT_ID or not LINE_CLIENT_SECRET:
        return RedirectResponse(f"{redirect_url}?lineError=server_config")

    # 1. Exchange code for tokens
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            token_resp = await client.post(
                LINE_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": LINE_WEB_CALLBACK_URI,
                    "client_id": LINE_CLIENT_ID,
                    "client_secret": LINE_CLIENT_SECRET,
                },
            )
    except httpx.TimeoutException:
        return RedirectResponse(f"{redirect_url}?lineError=timeout")

    if token_resp.status_code != 200:
        logger.error("[LINE Web] Token exchange failed: %s", token_resp.text)
        return RedirectResponse(f"{redirect_url}?lineError=token_failed")

    token_data = token_resp.json()
    id_token = token_data.get("id_token")
    if not id_token:
        return RedirectResponse(f"{redirect_url}?lineError=no_id_token")

    # 2. Verify ID token
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            verify_resp = await client.post(
                LINE_VERIFY_URL,
                data={"id_token": id_token, "client_id": LINE_CLIENT_ID},
            )
    except httpx.TimeoutException:
        return RedirectResponse(f"{redirect_url}?lineError=verify_timeout")

    if verify_resp.status_code != 200:
        logger.error("[LINE Web] Verify failed: %s", verify_resp.text)
        return RedirectResponse(f"{redirect_url}?lineError=verify_failed")

    payload = verify_resp.json()
    line_user_id = payload.get("sub")
    name = payload.get("name")
    picture = payload.get("picture")

    if not line_user_id:
        return RedirectResponse(f"{redirect_url}?lineError=no_user")

    logger.info("[LINE Web] Verified: sub=%s, name=%s", line_user_id, name)

    # 3. Create Firebase custom token
    firebase_uid = f"line:{line_user_id}"
    try:
        custom_token_bytes = fb_auth.create_custom_token(
            firebase_uid,
            {"provider": "line", "name": name, "picture": picture},
        )
        custom_token = custom_token_bytes.decode("utf-8")
    except Exception:
        logger.exception("Failed to create custom token for uid=%s", firebase_uid)
        return RedirectResponse(f"{redirect_url}?lineError=firebase_error")

    # 4. Redirect to web app with token
    logger.info("[LINE Web] Success, redirecting to web app")
    return RedirectResponse(f"{redirect_url}?lineToken={quote(custom_token)}")
