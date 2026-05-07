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
            border: none; border-radius: 8px; cursor: pointer;
            display: flex; align-items: center; justify-content: center; gap: 8px; }}
  button:disabled {{ opacity: 0.5; cursor: not-allowed; }}
  .btn-google {{ background: #ffffff; color: #1a1a1a; border: 1px solid #dadce0; }}
  .btn-apple  {{ background: #000000; color: #ffffff; }}
  .btn-line   {{ background: #06c755; color: #ffffff; }}
  #status {{ margin-top: 16px; font-size: 13px; color: #555; min-height: 24px; }}
  .err {{ color: #b00020; }}
  .ok  {{ color: #137333; }}
  .divider {{ text-align: center; color: #888; font-size: 12px; margin: 8px 0; }}
</style>
</head><body>
<h1>{title}</h1>
<p>{intro}</p>
<button id="signin-google" class="btn-google">Google でログイン</button>
<button id="signin-apple"  class="btn-apple">Apple でログイン</button>
<button id="signin-line"   class="btn-line">LINE でログイン</button>
<p id="status"></p>

<script type="module">
  import {{ initializeApp }} from "https://www.gstatic.com/firebasejs/10.13.0/firebase-app.js";
  import {{
    getAuth, GoogleAuthProvider, OAuthProvider,
    signInWithPopup, signInWithRedirect, getRedirectResult,
    signInWithCustomToken,
  }} from "https://www.gstatic.com/firebasejs/10.13.0/firebase-auth.js";

  const cfg = {firebase_cfg};
  const provider_label = "{provider_label}";
  const consume_url = "{consume_url}";
  const link_token = "{link_token}";
  const provider_slug = "{provider_slug}";

  const app = initializeApp(cfg);
  const auth = getAuth(app);
  const goog = new GoogleAuthProvider();
  const apple = new OAuthProvider("apple.com");
  apple.addScope("email");
  apple.addScope("name");

  const status = document.getElementById("status");
  const btnG = document.getElementById("signin-google");
  const btnA = document.getElementById("signin-apple");
  const btnL = document.getElementById("signin-line");
  const allBtns = [btnG, btnA, btnL];

  function setBusy(busy) {{
    allBtns.forEach((b) => {{ b.disabled = busy; }});
  }}

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
      setBusy(true);
    }} else {{
      const body = await r.text();
      status.className = "err";
      status.textContent = "連携に失敗しました (" + r.status + "): " + body;
      setBusy(false);
    }}
  }}

  async function runProviderLogin(label, providerObj) {{
    setBusy(true);
    status.className = "";
    status.textContent = label + " ログイン画面を表示します…";
    try {{
      const result = await signInWithPopup(auth, providerObj);
      const tok = await result.user.getIdToken();
      await consume(tok);
    }} catch (e) {{
      console.warn("popup failed for " + label + ", trying redirect", e);
      try {{
        await signInWithRedirect(auth, providerObj);
      }} catch (e2) {{
        status.className = "err";
        status.textContent = label + " ログインに失敗しました: " + (e2.message || e2);
        setBusy(false);
      }}
    }}
  }}

  btnG.addEventListener("click", () => runProviderLogin("Google", goog));
  btnA.addEventListener("click", () => runProviderLogin("Apple",  apple));

  // LINE login: server-side OAuth → Firebase custom token returned via
  // ``?customToken=...`` query param on this same page.
  btnL.addEventListener("click", () => {{
    setBusy(true);
    status.className = "";
    status.textContent = "LINE 認証画面を開きます…";
    const startUrl = "/auth/line/web?redirect=" + encodeURIComponent(
      window.location.origin + "/integrations/" + provider_slug +
      "/login?token=" + encodeURIComponent(link_token) + "&from=line"
    ) + "&botlink=1";
    window.location.href = startUrl;
  }});

  // Pick up ``?lineToken=...`` (LINE redirect from auth/line/web/callback)
  // and finish sign-in via Firebase custom token.
  (async () => {{
    const params = new URLSearchParams(window.location.search);
    const ct = params.get("lineToken") || params.get("customToken");
    const lineErr = params.get("lineError");
    if (lineErr) {{
      status.className = "err";
      status.textContent = "LINE ログインに失敗しました: " + lineErr;
      return;
    }}
    if (ct) {{
      setBusy(true);
      status.textContent = "LINE 認証を確定しています…";
      try {{
        const result = await signInWithCustomToken(auth, ct);
        const tok = await result.user.getIdToken();
        await consume(tok);
      }} catch (e) {{
        status.className = "err";
        status.textContent = "LINE 認証エラー: " + (e.message || e);
        setBusy(false);
      }}
      return;
    }}
    // Handle Google/Apple redirect-result on page load.
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
        link_token=token,
        provider_slug=provider,
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
