"""
Apple Sign-In relay for desktop (Tauri) apps.

Flow:
1. Desktop app opens browser → GET /auth/apple/desktop?port={port}&state={csrf}
2. This endpoint redirects to Apple OAuth authorization page
3. User authenticates with Apple
4. Apple form-posts to POST /auth/apple/callback with authorization code
5. We exchange the code for an id_token via Apple's token endpoint
6. Redirect to http://127.0.0.1:{port}?id_token={token}&state={state}
7. Desktop app captures the id_token and exchanges it with Firebase
"""

import os
import time
import logging
from urllib.parse import urlencode, quote

import httpx
from fastapi import APIRouter, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from jose import jwt

logger = logging.getLogger("app.auth.apple_desktop")

router = APIRouter()

# ── Configuration ──────────────────────────────────────────────────

APPLE_TEAM_ID = os.getenv("APPLE_TEAM_ID", "6RR7572ZLU")
APPLE_KEY_ID = os.getenv("APPLE_KEY_ID", "6JF4BSRGZ8")
APPLE_WEB_CLIENT_ID = os.getenv("APPLE_WEB_CLIENT_ID", "")
APPLE_KEY_P8 = os.getenv("APPLE_KEY_P8", "")

APPLE_AUTH_URL = "https://appleid.apple.com/auth/authorize"
APPLE_TOKEN_URL = "https://appleid.apple.com/auth/token"


def _get_redirect_uri() -> str:
    """Build the callback redirect URI from the running service URL."""
    base = os.getenv(
        "APPLE_DESKTOP_REDIRECT_URI",
        "https://deepnote-api-900324644592.asia-northeast1.run.app/auth/apple/callback",
    )
    return base


def _format_private_key(raw: str) -> str:
    """Normalize the P8 private key (may be stored with literal \\n)."""
    key = raw.replace("\\n", "\n")
    if "-----BEGIN PRIVATE KEY-----" not in key:
        key = f"-----BEGIN PRIVATE KEY-----\n{key}\n-----END PRIVATE KEY-----"
    return key


def _generate_client_secret() -> str:
    """
    Generate a short-lived client_secret JWT for Apple's token endpoint.
    Signed with ES256 using the App Store Connect API key.
    """
    now = int(time.time())
    private_key = _format_private_key(APPLE_KEY_P8)

    claims = {
        "iss": APPLE_TEAM_ID,
        "iat": now,
        "exp": now + 300,  # 5 minutes
        "aud": "https://appleid.apple.com",
        "sub": APPLE_WEB_CLIENT_ID,
    }

    return jwt.encode(
        claims,
        private_key,
        algorithm="ES256",
        headers={"kid": APPLE_KEY_ID},
    )


def _js_navigate(url: str, success: bool = True, message: str = "") -> HTMLResponse:
    """
    Return an HTML page that navigates to the loopback URL via JavaScript.
    This avoids Safari's "insecure form" warning when redirecting from HTTPS to HTTP.
    """
    if success:
        title = "ログイン成功"
        body = "認証が完了しました。アプリに戻ります..."
        color = "#10b981"
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
background:linear-gradient(135deg,#f0fdf4 0%,#e0f2fe 100%)}}
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
// Send token to loopback via Image (bypasses CORS, avoids HTTPS->HTTP warning)
new Image().src = "{url}";
setTimeout(function(){{ window.close(); }}, 2500);
</script>
</body></html>"""
    return HTMLResponse(content=html)


# ── Endpoints ──────────────────────────────────────────────────────


@router.get("/auth/apple/desktop")
async def apple_desktop_start(
    port: int = Query(..., ge=1024, le=65535),
    state: str = Query(..., min_length=1),
):
    """
    Initiate Apple Sign-In for a desktop app.
    Redirects the browser to Apple's OAuth authorization page.
    """
    if not APPLE_WEB_CLIENT_ID:
        raise HTTPException(
            status_code=500,
            detail="APPLE_WEB_CLIENT_ID is not configured on the server.",
        )

    # Encode port + state into Apple's state parameter
    combined_state = f"{port}:{state}"

    params = {
        "client_id": APPLE_WEB_CLIENT_ID,
        "redirect_uri": _get_redirect_uri(),
        "response_type": "code",
        "scope": "name email",
        "response_mode": "form_post",
        "state": combined_state,
    }

    auth_url = f"{APPLE_AUTH_URL}?{urlencode(params)}"
    logger.info(
        "[Apple Desktop] Redirecting to Apple OAuth (port=%d, state=%s...)",
        port,
        state[:8],
    )
    return RedirectResponse(auth_url)


@router.post("/auth/apple/callback")
async def apple_desktop_callback(
    code: str = Form(None),
    state: str = Form(None),
    error: str = Form(None),
    user: str = Form(None),  # Apple sends user info JSON on first sign-in
):
    """
    Handle Apple's OAuth form-post callback.
    Exchange the authorization code for an id_token,
    then redirect to the desktop app's loopback listener.
    """
    # ── Parse state to extract port + original CSRF state ──
    if not state or ":" not in state:
        logger.error("[Apple Desktop] Missing or malformed state: %s", state)
        raise HTTPException(status_code=400, detail="Invalid state parameter")

    port_str, original_state = state.split(":", 1)
    try:
        port = int(port_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid port in state")

    desktop_base = f"http://127.0.0.1:{port}"

    # ── Handle Apple-side errors ──
    if error:
        logger.warning("[Apple Desktop] Apple returned error: %s", error)
        target = f"{desktop_base}?error={quote(error)}&state={quote(original_state)}"
        return _js_navigate(target, success=False, message=error)

    if not code:
        logger.error("[Apple Desktop] No authorization code received")
        target = f"{desktop_base}?error=no_code&state={quote(original_state)}"
        return _js_navigate(target, success=False, message="No authorization code")

    # ── Exchange authorization code for tokens ──
    if not APPLE_WEB_CLIENT_ID or not APPLE_KEY_P8:
        logger.error("[Apple Desktop] Missing APPLE_WEB_CLIENT_ID or APPLE_KEY_P8")
        target = f"{desktop_base}?error=server_config_error&state={quote(original_state)}"
        return _js_navigate(target, success=False, message="Server configuration error")

    try:
        client_secret = _generate_client_secret()
    except Exception as e:
        logger.exception("[Apple Desktop] Failed to generate client_secret: %s", e)
        target = f"{desktop_base}?error=client_secret_error&state={quote(original_state)}"
        return _js_navigate(target, success=False, message="Internal error")

    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _get_redirect_uri(),
        "client_id": APPLE_WEB_CLIENT_ID,
        "client_secret": client_secret,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                APPLE_TOKEN_URL,
                data=token_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if resp.status_code != 200:
            logger.error(
                "[Apple Desktop] Token exchange failed: status=%d body=%s",
                resp.status_code,
                resp.text[:500],
            )
            target = f"{desktop_base}?error=token_exchange_failed&state={quote(original_state)}"
            return _js_navigate(target, success=False, message="Token exchange failed")

        token_resp = resp.json()
        id_token = token_resp.get("id_token")

        if not id_token:
            logger.error("[Apple Desktop] No id_token in Apple response: %s", token_resp.keys())
            target = f"{desktop_base}?error=no_id_token&state={quote(original_state)}"
            return _js_navigate(target, success=False, message="No ID token received")

    except httpx.TimeoutException:
        logger.error("[Apple Desktop] Token exchange timed out")
        target = f"{desktop_base}?error=timeout&state={quote(original_state)}"
        return _js_navigate(target, success=False, message="Request timed out")
    except Exception as e:
        logger.exception("[Apple Desktop] Token exchange error: %s", e)
        target = f"{desktop_base}?error=exchange_error&state={quote(original_state)}"
        return _js_navigate(target, success=False, message="Exchange error")

    # ── Navigate to desktop app with id_token (via JS to avoid HTTPS→HTTP warning) ──
    logger.info("[Apple Desktop] Success, navigating to desktop (port=%d)", port)
    target = f"{desktop_base}?id_token={quote(id_token)}&state={quote(original_state)}"
    return _js_navigate(target, success=True)
