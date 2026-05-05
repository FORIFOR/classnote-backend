"""Backend-hosted export bridge for LINE / Slack chat asset URLs.

When a bot reply contains a URL like
    {DEEPNOTE_APP_BASE_URL}/sessions/{id}/export?format=pdf
we cannot assume the frontend has that page. Phase 6 implements a
backend-hosted bridge at the same path so the link works without a
frontend repo.

Flow:
  1. User taps the URL from LINE / Slack.
  2. We render an HTML page that asks the user to sign in (Firebase Web
     popup), then JS calls POST /sessions/{id}/export?format=... with the
     Firebase Bearer.
  3. Existing /sessions/{id}/export endpoint runs cost_guard, generates
     the file, uploads to GCS, and returns a 1-hour signed URL.
  4. JS triggers a download with that signed URL.

Env required (same as bot_login.py):
  FIREBASE_WEB_API_KEY, FIREBASE_WEB_AUTH_DOMAIN, FIREBASE_WEB_PROJECT_ID
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

logger = logging.getLogger("app.routes.bot_export_bridge")
router = APIRouter(tags=["Integrations:ExportBridge"])


def _firebase_web_config():
    api_key = os.environ.get("FIREBASE_WEB_API_KEY")
    auth_domain = os.environ.get("FIREBASE_WEB_AUTH_DOMAIN")
    project_id = (
        os.environ.get("FIREBASE_WEB_PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCP_PROJECT")
    )
    if not api_key or not auth_domain or not project_id:
        return None
    return {"apiKey": api_key, "authDomain": auth_domain, "projectId": project_id}


_TEMPLATE = """<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DeepNote 資料ダウンロード</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Hiragino Kaku Gothic ProN", sans-serif;
          padding: 24px; max-width: 480px; margin: 0 auto; color: #1a1a1a; background: #fafafa; }}
  h1 {{ font-size: 18px; margin: 0 0 16px; }}
  p  {{ margin: 0 0 12px; font-size: 14px; }}
  button {{ width: 100%; padding: 14px 16px; font-size: 15px; background: #2563eb;
            color: #fff; border: none; border-radius: 8px; }}
  button:disabled {{ background: #ccc; }}
  #status {{ margin-top: 16px; font-size: 13px; min-height: 24px; color: #555; }}
  .ok {{ color: #137333; }} .err {{ color: #b00020; }}
</style>
</head><body>
<h1>DeepNote 資料ダウンロード</h1>
<p>セッション: <code>{session_id}</code> / 形式: <strong>{fmt}</strong></p>
<button id="go">DeepNote にログインしてダウンロード</button>
<p id="status"></p>

<script type="module">
  import {{ initializeApp }} from "https://www.gstatic.com/firebasejs/10.13.0/firebase-app.js";
  import {{ getAuth, GoogleAuthProvider, signInWithPopup }}
    from "https://www.gstatic.com/firebasejs/10.13.0/firebase-auth.js";

  const cfg = {firebase_cfg};
  const session = {session_json};
  const fmt = {fmt_json};
  const includeTranscript = {include_transcript};

  const app = initializeApp(cfg);
  const auth = getAuth(app);
  const goog = new GoogleAuthProvider();

  const status = document.getElementById("status");
  const btn = document.getElementById("go");

  async function exportAndDownload(idToken) {{
    status.textContent = "資料を生成しています…";
    const url = `/sessions/${{session}}/export`;
    const r = await fetch(url, {{
      method: "POST",
      headers: {{
        "Authorization": "Bearer " + idToken,
        "Content-Type": "application/json",
      }},
      body: JSON.stringify({{ format: fmt, includeTranscript }}),
    }});
    if (!r.ok) {{
      const body = await r.text();
      status.className = "err";
      status.textContent = "生成に失敗しました (" + r.status + "): " + body;
      btn.disabled = false;
      return;
    }}
    const data = await r.json();
    if (!data.downloadUrl) {{
      status.className = "err";
      status.textContent = "ダウンロードURLが取得できませんでした。";
      return;
    }}
    status.className = "ok";
    status.textContent = "ダウンロードを開始します。新しいタブが開かれます。";
    window.open(data.downloadUrl, "_blank");
  }}

  btn.addEventListener("click", async () => {{
    btn.disabled = true;
    try {{
      const result = await signInWithPopup(auth, goog);
      const tok = await result.user.getIdToken();
      await exportAndDownload(tok);
    }} catch (e) {{
      status.className = "err";
      status.textContent = "ログインに失敗しました: " + (e.message || e);
      btn.disabled = false;
    }}
  }});
</script>
</body></html>
"""

_NOT_CONFIGURED = """<!doctype html>
<html lang="ja"><body style="font-family:sans-serif;padding:24px;max-width:480px;margin:0 auto">
<h1>資料を取得できません</h1>
<p>サーバー側の Firebase Web 設定が完了していません。</p>
</body></html>
"""


@router.get("/sessions/{session_id}/export", include_in_schema=False)
async def export_bridge_page(
    session_id: str,
    format: str = Query("pdf", regex="^(pdf|docx|pptx)$"),
    include_transcript: bool = Query(False, alias="includeTranscript"),
):
    """HTML bridge that signs the user in and triggers POST /sessions/{id}/export.

    NOTE: The actual export endpoint is the existing
    POST /sessions/{session_id}/export (defined in app/routes/export.py).
    FastAPI matches GET vs POST distinctly, so this GET bridge does not
    collide with the POST exporter.
    """
    cfg = _firebase_web_config()
    if not cfg:
        return HTMLResponse(_NOT_CONFIGURED, status_code=503)
    html = _TEMPLATE.format(
        session_id=session_id,
        fmt=format,
        firebase_cfg=json.dumps(cfg),
        session_json=json.dumps(session_id),
        fmt_json=json.dumps(format),
        include_transcript="true" if include_transcript else "false",
    )
    return HTMLResponse(content=html)
