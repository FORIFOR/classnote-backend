"""Phase 8 — self-service settings & audit view for bot users.

Endpoints:
  GET  /integrations/me/settings       (HTML: shows links + digest toggle)
  GET  /integrations/me/links          (JSON: my LINE / Slack links)
  POST /integrations/me/digest         (toggle digestDisabled on every link)
  GET  /integrations/me/audit          (JSON: my recent bot_audit_logs rows)
  DELETE /integrations/me/links/line   (revoke LINE link for caller)
  DELETE /integrations/me/links/slack/{team_id}/{slack_user_id}
                                       (revoke Slack link for caller)

All require Firebase Bearer auth — every action is scoped to current_user.
"""
from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Body
from fastapi.responses import HTMLResponse
from google.cloud import firestore

from app.dependencies import CurrentUser, get_current_user
from app.firebase import db
from app.services import line_link_tokens, slack_link_tokens

logger = logging.getLogger("app.routes.bot_settings")
router = APIRouter(prefix="/integrations/me", tags=["Integrations:UserSettings"])


def _list_my_line_links(uid: str) -> List[dict]:
    out = []
    for snap in (
        db.collection(line_link_tokens.USER_LINKS_COLLECTION)
        .where("deepnoteUid", "==", uid)
        .stream()
    ):
        d = snap.to_dict() or {}
        out.append({
            "lineUserId": snap.id,
            "linkedAt": d.get("linkedAt"),
            "digestDisabled": bool(d.get("digestDisabled")),
        })
    return out


def _list_my_slack_links(uid: str) -> List[dict]:
    out = []
    for snap in (
        db.collection(slack_link_tokens.USER_LINKS_COLLECTION)
        .where("deepnoteUid", "==", uid)
        .stream()
    ):
        d = snap.to_dict() or {}
        out.append({
            "teamId": d.get("teamId"),
            "slackUserId": d.get("slackUserId"),
            "linkedAt": d.get("linkedAt"),
            "digestDisabled": bool(d.get("digestDisabled")),
        })
    return out


@router.get("/links")
async def list_my_links(current_user: CurrentUser = Depends(get_current_user)):
    return {
        "line": _list_my_line_links(current_user.uid),
        "slack": _list_my_slack_links(current_user.uid),
    }


@router.post("/digest")
async def toggle_digest(
    body: dict = Body(...),
    current_user: CurrentUser = Depends(get_current_user),
):
    enabled = bool(body.get("enabled", True))
    flag = not enabled  # disabledDisabled means digest off
    updated = 0
    for snap in (
        db.collection(line_link_tokens.USER_LINKS_COLLECTION)
        .where("deepnoteUid", "==", current_user.uid).stream()
    ):
        snap.reference.set({"digestDisabled": flag}, merge=True)
        updated += 1
    for snap in (
        db.collection(slack_link_tokens.USER_LINKS_COLLECTION)
        .where("deepnoteUid", "==", current_user.uid).stream()
    ):
        snap.reference.set({"digestDisabled": flag}, merge=True)
        updated += 1
    return {"enabled": enabled, "linksUpdated": updated}


@router.get("/audit")
async def my_audit(
    limit: int = 20,
    current_user: CurrentUser = Depends(get_current_user),
):
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="invalid_limit")
    rows = []
    for snap in (
        db.collection("bot_audit_logs")
        .where("deepnoteUid", "==", current_user.uid)
        .limit(200)
        .stream()
    ):
        d = snap.to_dict() or {}
        rows.append({
            "id": snap.id,
            "provider": d.get("provider"),
            "sourceType": d.get("sourceType"),
            "command": d.get("command"),
            "outcome": d.get("outcome"),
            "at": d.get("at"),
            "teamId": d.get("teamId"),
        })
    rows.sort(key=lambda r: r.get("at") or 0, reverse=True)
    return {"items": rows[:limit]}


@router.delete("/links/line", status_code=204)
async def unlink_line(current_user: CurrentUser = Depends(get_current_user)):
    for snap in (
        db.collection(line_link_tokens.USER_LINKS_COLLECTION)
        .where("deepnoteUid", "==", current_user.uid).stream()
    ):
        snap.reference.delete()
    return None


@router.delete("/links/slack/{team_id}/{slack_user_id}", status_code=204)
async def unlink_slack(
    team_id: str, slack_user_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    doc_id = f"{team_id}:{slack_user_id}"
    snap = db.collection(slack_link_tokens.USER_LINKS_COLLECTION).document(doc_id).get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="link_not_found")
    data = snap.to_dict() or {}
    if data.get("deepnoteUid") != current_user.uid:
        # Don't reveal existence — uniform 404.
        raise HTTPException(status_code=404, detail="link_not_found")
    snap.reference.delete()
    return None


_SETTINGS_HTML = """<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DeepNote 連携設定</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Hiragino Kaku Gothic ProN", sans-serif;
          max-width: 560px; margin: 0 auto; padding: 24px; color: #1a1a1a; background: #fafafa; }}
  h1 {{ font-size: 18px; }}
  section {{ background: #fff; border: 1px solid #e0e0e0; border-radius: 8px;
             padding: 16px; margin-bottom: 16px; }}
  button {{ padding: 10px 14px; border: none; border-radius: 6px; cursor: pointer;
            background: #2563eb; color: #fff; font-size: 14px; }}
  .danger {{ background: #b00020; }}
  table {{ width: 100%; font-size: 13px; border-collapse: collapse; }}
  td, th {{ border-bottom: 1px solid #eee; padding: 6px 4px; text-align: left; }}
  .err {{ color: #b00020; }} .ok {{ color: #137333; }}
</style>
</head><body>
<h1>DeepNote 連携設定</h1>
<section id="login">
  <p>DeepNote にログインしてください。</p>
  <button id="signin">Google でログイン</button>
  <p id="loginStatus"></p>
</section>
<section id="panel" style="display:none">
  <h2>朝のダイジェスト</h2>
  <p><label><input type="checkbox" id="digestEnabled"> 毎朝の自動配信を受け取る</label></p>
  <button id="saveDigest">保存</button>
  <p id="digestStatus"></p>

  <h2>連携中のアカウント</h2>
  <div id="links"></div>

  <h2>最近の利用履歴</h2>
  <div id="audit"></div>
</section>

<script type="module">
  import {{ initializeApp }} from "https://www.gstatic.com/firebasejs/10.13.0/firebase-app.js";
  import {{ getAuth, GoogleAuthProvider, signInWithPopup }}
    from "https://www.gstatic.com/firebasejs/10.13.0/firebase-auth.js";

  const cfg = {firebase_cfg};
  const app = initializeApp(cfg);
  const auth = getAuth(app);
  const goog = new GoogleAuthProvider();

  let idToken = null;

  async function api(path, method="GET", body=null) {{
    const opts = {{ method, headers: {{ "Authorization": "Bearer " + idToken }} }};
    if (body) {{
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }}
    const r = await fetch(path, opts);
    if (!r.ok) throw new Error(r.status + ": " + (await r.text()));
    if (r.status === 204) return null;
    return r.json();
  }}

  function fmtRows(rows, formatter) {{
    if (!rows.length) return "<p>なし</p>";
    return "<table>" + rows.map(formatter).join("") + "</table>";
  }}

  async function refresh() {{
    const links = await api("/integrations/me/links");
    let html = "";
    if (links.line.length) {{
      html += "<h3>LINE</h3>" + fmtRows(links.line, l =>
        `<tr><td>${{l.lineUserId}}</td><td>${{l.linkedAt||""}}</td>` +
        `<td><button class="danger" data-line>解除</button></td></tr>`);
    }}
    if (links.slack.length) {{
      html += "<h3>Slack</h3>" + fmtRows(links.slack, l =>
        `<tr><td>${{l.teamId}} / ${{l.slackUserId}}</td><td>${{l.linkedAt||""}}</td>` +
        `<td><button class="danger" data-slack data-team="${{l.teamId}}" data-user="${{l.slackUserId}}">解除</button></td></tr>`);
    }}
    if (!html) html = "<p>連携中のアカウントはありません。</p>";
    document.getElementById("links").innerHTML = html;
    document.querySelectorAll("button[data-line]").forEach(b => b.onclick = async () => {{
      await api("/integrations/me/links/line", "DELETE");
      refresh();
    }});
    document.querySelectorAll("button[data-slack]").forEach(b => b.onclick = async () => {{
      await api(`/integrations/me/links/slack/${{b.dataset.team}}/${{b.dataset.user}}`, "DELETE");
      refresh();
    }});

    const digestOn = links.line.some(l => !l.digestDisabled) ||
                     links.slack.some(l => !l.digestDisabled) ||
                     (!links.line.length && !links.slack.length);
    document.getElementById("digestEnabled").checked = digestOn;

    const audit = await api("/integrations/me/audit?limit=20");
    document.getElementById("audit").innerHTML = fmtRows(audit.items, r =>
      `<tr><td>${{r.provider}}</td><td>${{r.sourceType}}</td><td>${{r.command}}</td>` +
      `<td>${{r.outcome}}</td><td>${{r.at||""}}</td></tr>`);
  }}

  document.getElementById("signin").onclick = async () => {{
    try {{
      const result = await signInWithPopup(auth, goog);
      idToken = await result.user.getIdToken();
      document.getElementById("login").style.display = "none";
      document.getElementById("panel").style.display = "block";
      await refresh();
    }} catch (e) {{
      const s = document.getElementById("loginStatus");
      s.className = "err";
      s.textContent = "ログインに失敗しました: " + e.message;
    }}
  }};

  document.getElementById("saveDigest").onclick = async () => {{
    const enabled = document.getElementById("digestEnabled").checked;
    const r = await api("/integrations/me/digest", "POST", {{ enabled }});
    const s = document.getElementById("digestStatus");
    s.className = "ok";
    s.textContent = `${{r.linksUpdated}} 件のリンクを更新しました。`;
  }};
</script>
</body></html>
"""

_NO_FIREBASE_HTML = """<!doctype html><html lang="ja"><body style="font-family:sans-serif;padding:24px">
<h1>連携設定を表示できません</h1><p>サーバー側の Firebase Web 設定が完了していません。</p>
</body></html>"""


@router.get("/settings", include_in_schema=False)
async def settings_page():
    import os, json as _json
    cfg = {
        "apiKey": os.environ.get("FIREBASE_WEB_API_KEY"),
        "authDomain": os.environ.get("FIREBASE_WEB_AUTH_DOMAIN"),
        "projectId": (
            os.environ.get("FIREBASE_WEB_PROJECT_ID")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("GCP_PROJECT")
        ),
    }
    if not cfg["apiKey"] or not cfg["authDomain"] or not cfg["projectId"]:
        return HTMLResponse(_NO_FIREBASE_HTML, status_code=503)
    return HTMLResponse(_SETTINGS_HTML.format(firebase_cfg=_json.dumps(cfg)))
