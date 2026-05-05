"""Backend-hosted minimal login page for LINE / Slack bot account linking.

Phase 5 design:
  When LINE_CONNECT_FRONTEND_URL / SLACK_CONNECT_FRONTEND_URL is unset
  (or this fallback is requested explicitly), we serve a lightweight
  Firebase Web SDK Google sign-in page from the backend itself. After
  sign-in, JS calls
      POST /integrations/{line,slack}/link-tokens/{token}:consume
  with the Firebase ID token. This makes the link flow self-contained:
  no separate frontend repository required.

  Required env to enable the fallback:
    FIREBASE_WEB_API_KEY
    FIREBASE_WEB_AUTH_DOMAIN     (e.g. classnote-x-dev.firebaseapp.com)
    FIREBASE_WEB_PROJECT_ID      (defaults to GOOGLE_CLOUD_PROJECT)

  If Firebase Web env is unset, we render a "frontend not configured"
  notice instead of a broken page.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

from app.services import line_link_tokens, slack_link_tokens

logger = logging.getLogger("app.routes.bot_login")
router = APIRouter(tags=["Integrations:LoginFallback"])


def _firebase_web_config() -> Optional[dict]:
    api_key = os.environ.get("FIREBASE_WEB_API_KEY")
    auth_domain = os.environ.get("FIREBASE_WEB_AUTH_DOMAIN")
    project_id = (
        os.environ.get("FIREBASE_WEB_PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCP_PROJECT")
    )
    if not api_key or not auth_domain or not project_id:
        return None
    return {
        "apiKey": api_key,
        "authDomain": auth_domain,
        "projectId": project_id,
    }


_PAGE_TEMPLATE = """<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Hiragino Kaku Gothic ProN", sans-serif;
          padding: 24px; line-height: 1.6; max-width: 480px; margin: 0 auto;
          color: #1a1a1a; background: #fafafa; }}
  h1 {{ font-size: 18px; margin: 0 0 16px; }}
  p  {{ margin: 0 0 12px; font-size: 14px; }}
  button {{ width: 100%; margin: 12px 0; padding: 14px 16px; font-size: 15px;
            background: {accent}; color: #fff; border: none; border-radius: 8px;
            cursor: pointer; }}
  button:disabled {{ background: #ccc; }}
  #status {{ margin-top: 16px; font-size: 13px; color: #555; min-height: 24px; }}
  .err {{ color: #b00020; }}
  .ok  {{ color: #137333; }}
</style>
</head><body>
<h1>{title}</h1>
<p>{intro}</p>
<button id="signin">Google でログインして連携を完了</button>
<p id="status"></p>

<script type="module">
  import {{ initializeApp }} from "https://www.gstatic.com/firebasejs/10.13.0/firebase-app.js";
  import {{ getAuth, GoogleAuthProvider, signInWithPopup, signInWithRedirect, getRedirectResult }}
    from "https://www.gstatic.com/firebasejs/10.13.0/firebase-auth.js";

  const cfg = {firebase_cfg};
  const provider_label = "{provider_label}";
  const consume_url = "{consume_url}";

  const app = initializeApp(cfg);
  const auth = getAuth(app);
  const goog = new GoogleAuthProvider();

  const status = document.getElementById("status");
  const btn = document.getElementById("signin");

  async function consume(idToken) {{
    status.textContent = "連携を確定しています…";
    const r = await fetch(consume_url, {{
      method: "POST",
      headers: {{ "Authorization": "Bearer " + idToken }},
    }});
    if (r.ok) {{
      status.className = "ok";
      status.textContent = provider_label + " と DeepNote の連携が完了しました。" +
        provider_label + " のチャットに戻ってご利用ください。";
      btn.disabled = true;
    }} else {{
      const body = await r.text();
      status.className = "err";
      status.textContent = "連携に失敗しました (" + r.status + "): " + body;
    }}
  }}

  btn.addEventListener("click", async () => {{
    btn.disabled = true;
    status.textContent = "Google ログイン画面を表示します…";
    try {{
      const result = await signInWithPopup(auth, goog);
      const tok = await result.user.getIdToken();
      await consume(tok);
    }} catch (e) {{
      // popup blocked -> fallback to redirect
      console.warn("popup failed, trying redirect", e);
      try {{
        await signInWithRedirect(auth, goog);
      }} catch (e2) {{
        status.className = "err";
        status.textContent = "Google ログインに失敗しました: " + (e2.message || e2);
        btn.disabled = false;
      }}
    }}
  }});

  // Handle redirect-result on page load (when popup fallback fired earlier).
  (async () => {{
    try {{
      const r = await getRedirectResult(auth);
      if (r && r.user) {{
        const tok = await r.user.getIdToken();
        await consume(tok);
      }}
    }} catch (e) {{
      console.warn("redirect-result error", e);
    }}
  }})();
</script>
</body></html>
"""

_NOT_CONFIGURED_HTML = """<!doctype html>
<html lang="ja"><body style="font-family:sans-serif;padding:24px;max-width:480px;margin:0 auto">
<h1>連携できません</h1>
<p>サーバー側の Firebase Web 設定が完了していません (FIREBASE_WEB_API_KEY 等)。</p>
<p>管理者にお問い合わせください。</p>
</body></html>
"""


def _render_login_page(*, provider: str, token: str) -> HTMLResponse:
    cfg = _firebase_web_config()
    if not cfg:
        return HTMLResponse(_NOT_CONFIGURED_HTML, status_code=503)
    label = "LINE" if provider == "line" else "Slack"
    accent = "#06c755" if provider == "line" else "#4a154b"
    consume = f"/integrations/{provider}/link-tokens/{token}:consume"
    import json as _json
    html = _PAGE_TEMPLATE.format(
        title=f"DeepNote と {label} の連携",
        intro=f"{label} bot からの連携リクエストを完了するために、DeepNote にログインしてください。",
        accent=accent,
        firebase_cfg=_json.dumps(cfg),
        provider_label=label,
        consume_url=consume,
    )
    return HTMLResponse(content=html)


@router.get("/integrations/line/login", include_in_schema=False)
async def line_login_fallback(token: str = Query(...)):
    try:
        line_link_tokens.resolve(token)
    except line_link_tokens.TokenError as e:
        raise HTTPException(status_code=e.status, detail=e.code)
    return _render_login_page(provider="line", token=token)


@router.get("/integrations/slack/login", include_in_schema=False)
async def slack_login_fallback(token: str = Query(...)):
    try:
        slack_link_tokens.resolve(token)
    except slack_link_tokens.TokenError as e:
        raise HTTPException(status_code=e.status, detail=e.code)
    return _render_login_page(provider="slack", token=token)
