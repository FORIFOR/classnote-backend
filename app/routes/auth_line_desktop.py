"""
LINE Login relay for desktop (Tauri) apps.

Flow:
1. Desktop app opens browser → GET /auth/line/desktop?port={port}&state={csrf}
2. This endpoint redirects to LINE's OAuth authorization page
3. User authenticates with LINE
4. LINE redirects to GET /auth/line/callback?code={code}&state={port:csrf}
5. We exchange the code for tokens, verify id_token, create Firebase custom token
6. Redirect to http://127.0.0.1:{port}?token={firebaseCustomToken}&state={state}
7. Desktop app captures the token and signs in with Firebase
"""

import os
import logging
from urllib.parse import urlencode, quote

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from firebase_admin import auth as fb_auth

logger = logging.getLogger("app.auth.line_desktop")

router = APIRouter()

LINE_AUTH_URL = "https://access.line.me/oauth2/v2.1/authorize"
LINE_TOKEN_URL = "https://api.line.me/oauth2/v2.1/token"
LINE_VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"


LINE_CALLBACK_URI = "https://deepnote-api-900324644592.asia-northeast1.run.app/auth/line/callback"


def _js_navigate(url: str, success: bool = True, message: str = "") -> HTMLResponse:
    if success:
        title = "ログイン成功"
        body = "LINE認証が完了しました。アプリに戻ります..."
        color = "#06C755"
    else:
        title = "ログインに失敗しました"
        body = message or "エラーが発生しました"
        color = "#ef4444"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
height:100vh;display:flex;align-items:center;justify-content:center;
background:linear-gradient(135deg,#e8ffe8 0%,#f0fdf4 100%)}}
.card{{background:#fff;border-radius:20px;padding:48px 40px;text-align:center;
box-shadow:0 20px 60px rgba(0,0,0,.08);max-width:380px;width:90%}}
.icon{{width:64px;height:64px;border-radius:50%;background:{color};
display:flex;align-items:center;justify-content:center;margin:0 auto 20px}}
.icon svg{{width:32px;height:32px;color:#fff}}
h2{{font-size:22px;font-weight:700;color:#1a1a1a;margin-bottom:8px}}
p{{font-size:14px;color:#6b7280;line-height:1.6}}
.sub{{font-size:12px;color:#9ca3af;margin-top:16px}}
</style></head><body>
<div class="card">
<div class="icon"><svg fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="3">
{"<path stroke-linecap='round' stroke-linejoin='round' d='M5 13l4 4L19 7'/>" if success else "<path stroke-linecap='round' stroke-linejoin='round' d='M6 18L18 6M6 6l12 12'/>"}
</svg></div>
<h2>{title}</h2>
<p>{body}</p>
<p class="sub">このウィンドウは自動的に閉じます</p>
</div>
<script>
new Image().src = "{url}";
setTimeout(function(){{ window.close(); }}, 2500);
</script>
</body></html>"""
    return HTMLResponse(content=html)


@router.get("/auth/line/desktop")
async def line_desktop_start(
    port: int = Query(..., ge=1024, le=65535),
    state: str = Query(..., min_length=1),
):
    """Initiate LINE Login for a desktop app."""
    LINE_CLIENT_ID = os.environ.get("LINE_CHANNEL_ID")
    if not LINE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="LINE_CHANNEL_ID not configured")

    # Encode port + state into LINE's state parameter
    combined_state = f"{port}:{state}"

    params = {
        "response_type": "code",
        "client_id": LINE_CLIENT_ID,
        "redirect_uri": LINE_CALLBACK_URI,
        "state": combined_state,
        "scope": "profile openid",
    }

    auth_url = f"{LINE_AUTH_URL}?{urlencode(params)}"
    logger.info("[LINE Desktop] Redirecting to LINE OAuth (port=%d, redirect_uri=%s, client_id=%s)", port, LINE_CALLBACK_URI, LINE_CLIENT_ID)
    return RedirectResponse(auth_url)


@router.get("/auth/line/callback")
async def line_desktop_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    error_description: str = Query(None),
):
    """Handle LINE's OAuth callback."""
    # Parse state
    if not state or ":" not in state:
        logger.error("[LINE Desktop] Missing or malformed state: %s", state)
        raise HTTPException(status_code=400, detail="Invalid state parameter")

    port_str, original_state = state.split(":", 1)
    try:
        port = int(port_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid port in state")

    desktop_base = f"http://127.0.0.1:{port}"

    # Handle errors
    if error:
        logger.warning("[LINE Desktop] LINE returned error: %s (%s)", error, error_description)
        target = f"{desktop_base}?error={quote(error)}&state={quote(original_state)}"
        return _js_navigate(target, success=False, message=error_description or error)

    if not code:
        target = f"{desktop_base}?error=no_code&state={quote(original_state)}"
        return _js_navigate(target, success=False, message="No authorization code")

    LINE_CLIENT_ID = os.environ.get("LINE_CHANNEL_ID")
    LINE_CLIENT_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
    if not LINE_CLIENT_ID or not LINE_CLIENT_SECRET:
        target = f"{desktop_base}?error=server_error&state={quote(original_state)}"
        return _js_navigate(target, success=False, message="Server configuration error")

    # 1. Exchange code for tokens
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            token_resp = await client.post(
                LINE_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": LINE_CALLBACK_URI,
                    "client_id": LINE_CLIENT_ID,
                    "client_secret": LINE_CLIENT_SECRET,
                },
            )
    except httpx.TimeoutException:
        target = f"{desktop_base}?error=timeout&state={quote(original_state)}"
        return _js_navigate(target, success=False, message="LINE server timeout")

    if token_resp.status_code != 200:
        logger.error("[LINE Desktop] Token exchange failed: %s", token_resp.text)
        target = f"{desktop_base}?error=token_exchange_failed&state={quote(original_state)}"
        return _js_navigate(target, success=False, message="Token exchange failed")

    token_data = token_resp.json()
    id_token = token_data.get("id_token")
    if not id_token:
        target = f"{desktop_base}?error=no_id_token&state={quote(original_state)}"
        return _js_navigate(target, success=False, message="No ID token from LINE")

    # 2. Verify ID token
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            verify_resp = await client.post(
                LINE_VERIFY_URL,
                data={"id_token": id_token, "client_id": LINE_CLIENT_ID},
            )
    except httpx.TimeoutException:
        target = f"{desktop_base}?error=verify_timeout&state={quote(original_state)}"
        return _js_navigate(target, success=False, message="LINE verify timeout")

    if verify_resp.status_code != 200:
        logger.error("[LINE Desktop] Verify failed: %s", verify_resp.text)
        target = f"{desktop_base}?error=verify_failed&state={quote(original_state)}"
        return _js_navigate(target, success=False, message="Token verification failed")

    payload = verify_resp.json()
    line_user_id = payload.get("sub")
    name = payload.get("name")
    picture = payload.get("picture")

    if not line_user_id:
        target = f"{desktop_base}?error=no_sub&state={quote(original_state)}"
        return _js_navigate(target, success=False, message="No user ID in token")

    logger.info("[LINE Desktop] Verified: sub=%s, name=%s", line_user_id, name)

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
        target = f"{desktop_base}?error=firebase_error&state={quote(original_state)}"
        return _js_navigate(target, success=False, message="Authentication error")

    # 4. Redirect to desktop app
    logger.info("[LINE Desktop] Success, navigating to desktop (port=%d)", port)
    target = f"{desktop_base}?token={quote(custom_token)}&state={quote(original_state)}"
    return _js_navigate(target, success=True)
