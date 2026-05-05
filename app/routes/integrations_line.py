"""LINE webhook + connect HTML + link-token endpoints (Phase 1, 1:1 only).

Routes
  - POST /integrations/line/webhook                    (signature-verified, hidden)
  - POST /integrations/line/link-tokens                (internal helper, hidden)
  - GET  /integrations/line/link-tokens/{token}        (frontend / connect resolver)
  - POST /integrations/line/link-tokens/{token}:consume
                                                       (frontend, requires Firebase auth)
  - GET  /integrations/line/connect                    (HTML, hidden)

Group / room messages are out of scope for Phase 1 — we reply with the
LINE_GROUP_NOT_SUPPORTED constant and never expose user data.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from app.dependencies import CurrentUser, get_current_user
from app.services import line_link_tokens
from app.services import line_messaging
from app.services import line_briefing
from app.services import asset_delivery
from app.services import bot_audit
from app.services import group_shared_briefing

logger = logging.getLogger("app.routes.integrations.line")

router = APIRouter(prefix="/integrations/line", tags=["Integrations:LINE"])


# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────

def _public_base_url() -> str:
    """The host LINE users will hit. Falls back to the canonical prod URL."""
    return (
        os.environ.get("LINE_PUBLIC_BASE_URL")
        or os.environ.get("CLOUD_RUN_SERVICE_URL")
        or "https://deepnote-api-mur5rvqgga-an.a.run.app"
    ).rstrip("/")


def _frontend_login_url() -> Optional[str]:
    raw = os.environ.get("LINE_CONNECT_FRONTEND_URL")
    return raw.strip() if raw and raw.strip() else None


def _is_line_inapp(user_agent: Optional[str]) -> bool:
    if not user_agent:
        return False
    return "Line" in user_agent  # case-sensitive on purpose: LINE UA contains "Line/"


# ──────────────────────────────────────────────────────────────────────
# Phase 1 message constants (centralized for future i18n).
# Keep terse; the user requested polite, instruction-clear tone.
# ──────────────────────────────────────────────────────────────────────

class M:
    HELP = (
        "DeepNote と LINE の連携BOTです。\n"
        "次のメッセージで応答します:\n"
        "・「クレジット」: あなたのDeepNoteアカウントのクレジット残量\n"
        "・「最新」: 最新の会議の要約\n"
        "・「TODO」: 直近のTODO（最大3件）\n"
        "・「決定事項」: 最新会議の決定事項\n"
        "・「資料」: 最新会議の PDF / DOCX / PPTX リンク\n"
        "・「PDF」「DOCX」「PPTX」: 個別フォーマット\n"
        "・「ヘルプ」: この案内"
    )

    NOT_LINKED_INTRO = (
        "DeepNote と LINE の連携が必要です。\n"
        "下のリンクを開いて DeepNote にログインしてください。\n"
        "LINE内ブラウザでうまく開けない場合は、Safari / Chrome で開いてください。"
    )

    GROUP_NOT_SUPPORTED = (
        "現在、LINEグループでのご利用には未対応です。\n"
        "DeepNote とLINEの個人チャットでご利用ください。"
    )

    GROUP_NO_SHARED_DATA = (
        "このグループに共有された会議データが見つかりませんでした。\n"
        "DeepNote 上で「このワークスペースに共有」を有効にしてからご利用ください。"
    )

    GROUP_PRIVATE_REJECTED = (
        "クレジット残量や TODO は個人情報のため、グループでは表示できません。\n"
        "DeepNote との個人チャットでご確認ください。"
    )

    UNKNOWN_COMMAND = (
        "認識できませんでした。「ヘルプ」と送ると使い方が表示されます。"
    )

    CREDIT_TEMPLATE = (
        "あなたのDeepNoteアカウントのクレジット残量です。\n"
        "プラン: {plan}\n"
        "残量: {remaining} / {monthly_limit}\n"
        "{topup_line}"
    )

    CREDIT_UNLIMITED = (
        "あなたのDeepNoteアカウントは無制限プランです。"
    )

    CREDIT_FAILED = "クレジット情報の取得に失敗しました。少し時間をおいて再度お試しください。"

    LATEST_NONE = "あなたのDeepNoteアカウントには、まだ会議の記録がありません。"
    LATEST_TEMPLATE = "最新の会議: {title}\n\n{summary}"

    TODOS_NONE = "現在、未完了のTODOはありません。"
    TODOS_HEADER = "直近のTODO（最大3件）:"
    TODOS_LINE = "・{title}{due}"

    DECISIONS_NONE = "最新の会議には決定事項が記録されていません。"
    DECISIONS_HEADER = "最新の会議の決定事項:"
    DECISIONS_LINE = "・{text}"

    ASSETS_NONE = "最新会議の記録がないため、資料リンクをお出しできません。"
    ASSETS_HEADER = "最新会議「{title}」の資料リンクです。"
    ASSETS_TEMPLATE = (
        "{header}\n"
        "Web: {web}\n"
        "PDF: {pdf}\n"
        "DOCX: {docx}\n"
        "PPTX: {pptx}"
    )

    CONFIG_MISSING = (
        "サーバー設定が未完了のため、LINE連携をご案内できません。"
        "管理者にお問い合わせください。"
    )

    CONNECT_PAGE_TITLE = "DeepNote と LINE の連携"
    CONNECT_INTRO = (
        "DeepNote と LINE を連携します。"
        "LINE内ブラウザではログインに失敗することがあるため、"
        "Safari または Chrome で開いてください。"
    )
    CONNECT_COPY_LABEL = "URLをコピー"
    CONNECT_COPY_HINT = "コピー後、Safari または Chrome に貼り付けて開いてください。"
    CONNECT_INVALID = "リンクの有効期限が切れたか、無効です。LINE で再度連携をお試しください。"
    CONNECT_LOGIN_PROMPT = "DeepNote にログインして連携を完了してください。"


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _connect_url(token: str) -> str:
    return f"{_public_base_url()}/integrations/line/connect?{urlencode({'token': token})}"


def _format_credit(report: Dict[str, Any]) -> str:
    if report.get("unlimited"):
        return M.CREDIT_UNLIMITED
    topup = int(report.get("topupCredits") or 0)
    topup_line = f"購入分: {topup}\n" if topup > 0 else ""
    return M.CREDIT_TEMPLATE.format(
        plan=report.get("plan") or "-",
        remaining=report.get("remaining") if report.get("remaining") is not None else "-",
        monthly_limit=report.get("monthlyLimit") if report.get("monthlyLimit") is not None else "-",
        topup_line=topup_line,
    ).rstrip()


def _format_latest(latest: Optional[Dict[str, Any]]) -> str:
    if not latest:
        return M.LATEST_NONE
    summary = (latest.get("summary") or "").strip()
    if not summary:
        summary = "（要約はまだ生成されていません）"
    if len(summary) > 1000:
        summary = summary[:997] + "..."
    return M.LATEST_TEMPLATE.format(title=latest.get("title") or "(無題)", summary=summary)


def _format_todos(todos: List[Dict[str, Any]]) -> str:
    if not todos:
        return M.TODOS_NONE
    lines = [M.TODOS_HEADER]
    for t in todos:
        due = t.get("dueDate")
        if isinstance(due, str):
            due_str = f"（期限: {due}）"
        elif due is not None:
            try:
                due_str = f"（期限: {due.strftime('%Y-%m-%d')}）"
            except Exception:
                due_str = ""
        else:
            due_str = ""
        lines.append(M.TODOS_LINE.format(title=t.get("title") or "(無題)", due=due_str))
    return "\n".join(lines)


def _format_decisions(decisions: List[str]) -> str:
    if not decisions:
        return M.DECISIONS_NONE
    return "\n".join([M.DECISIONS_HEADER] + [M.DECISIONS_LINE.format(text=d) for d in decisions])


def _classify_command(text: str) -> str:
    """Return a canonical command id from the user's free-text message."""
    if not text:
        return "help"
    t = text.strip().lower()
    if any(k in t for k in ("ヘルプ", "help", "使い方", "?", "？")):
        return "help"
    if any(k in t for k in ("クレジット", "残量", "credit")):
        return "credit"
    if any(k in t for k in ("最新", "会議", "summary", "要約")):
        return "latest"
    if any(k in t for k in ("todo", "タスク", "やること")):
        return "todos"
    if any(k in t for k in ("決定", "decision")):
        return "decisions"
    if any(k in t for k in ("pdf", "ピーディーエフ")):
        return "pdf"
    if "docx" in t or "ワード" in t or "word" in t:
        return "docx"
    if "pptx" in t or "パワポ" in t or "ppt" in t or "powerpoint" in t:
        return "pptx"
    if any(k in t for k in ("資料", "asset", "asset")):
        return "assets"
    return "unknown"


def _build_reply_for_linked(account_id: str, command: str) -> str:
    if command == "help":
        return M.HELP
    if command == "credit":
        report = line_briefing.get_credit_summary(account_id)
        if not report:
            return M.CREDIT_FAILED
        return _format_credit(report)
    if command == "latest":
        return _format_latest(line_briefing.get_latest_session(account_id))
    if command == "todos":
        return _format_todos(line_briefing.get_recent_todos(account_id, limit=3))
    if command == "decisions":
        return _format_decisions(line_briefing.get_latest_decisions(account_id))
    if command in ("assets", "pdf", "docx", "pptx"):
        bundle = asset_delivery.get_latest_export_links(account_id)
        if not bundle:
            return M.ASSETS_NONE
        if command == "pdf":
            return f"最新会議「{bundle['title']}」の PDF: {bundle['links']['pdf']}"
        if command == "docx":
            return f"最新会議「{bundle['title']}」の DOCX: {bundle['links']['docx']}"
        if command == "pptx":
            return f"最新会議「{bundle['title']}」の PPTX: {bundle['links']['pptx']}"
        return M.ASSETS_TEMPLATE.format(
            header=M.ASSETS_HEADER.format(title=bundle["title"]),
            web=bundle["links"]["web"],
            pdf=bundle["links"]["pdf"],
            docx=bundle["links"]["docx"],
            pptx=bundle["links"]["pptx"],
        )
    return M.UNKNOWN_COMMAND


def _handle_message_event(event: Dict[str, Any]) -> None:
    source = event.get("source") or {}
    source_type = source.get("type")
    reply_token = event.get("replyToken")
    message = event.get("message") or {}
    user_text = message.get("text", "") if message.get("type") == "text" else ""

    line_user_id = source.get("userId") or ""

    if source_type != "user":
        # Phase 7: groups / rooms can request *shared* data from a linked
        # speaker, but never private data (credit / TODO). If the speaker
        # is not linked, OR no shared session exists, we fall back to the
        # "未対応" notice so we still never leak private data.
        group_id = source.get("groupId") or source.get("roomId")
        if not group_id or not line_user_id:
            line_messaging.reply(reply_token, [line_messaging.text_message(M.GROUP_NOT_SUPPORTED)])
            bot_audit.record(
                provider="line", source_type=source_type or "unknown",
                source_user_id=line_user_id, command="unsupported",
                outcome="blocked_unsupported_source",
            )
            return
        link = line_link_tokens.get_link(line_user_id)
        cmd = _classify_command(user_text)
        if cmd in ("credit", "todos"):
            line_messaging.reply(reply_token, [line_messaging.text_message(M.GROUP_PRIVATE_REJECTED)])
            bot_audit.record(
                provider="line", source_type=source_type,
                source_user_id=line_user_id,
                account_id=(link or {}).get("accountId"),
                command=cmd, outcome="blocked_private_in_group",
            )
            return
        if not link:
            line_messaging.reply(reply_token, [line_messaging.text_message(M.GROUP_NOT_SUPPORTED)])
            bot_audit.record(
                provider="line", source_type=source_type,
                source_user_id=line_user_id, command=cmd,
                outcome="blocked_unsupported_source",
            )
            return
        ws_key = f"line:{group_id}"
        if cmd == "decisions":
            decisions = group_shared_briefing.get_recent_shared_decisions(
                link["accountId"], ws_key, limit=3
            )
            text = _format_decisions(decisions) if decisions else M.GROUP_NO_SHARED_DATA
        elif cmd == "latest" or cmd == "help":
            shared = group_shared_briefing.get_latest_shared_session(link["accountId"], ws_key)
            text = _format_latest(shared) if shared else M.GROUP_NO_SHARED_DATA
        else:
            text = M.GROUP_NO_SHARED_DATA
        line_messaging.reply(reply_token, [line_messaging.text_message(text)])
        bot_audit.record(
            provider="line", source_type=source_type,
            source_user_id=line_user_id,
            account_id=link["accountId"], deepnote_uid=link.get("deepnoteUid"),
            command=cmd,
            outcome="ok_shared_only" if "共有" not in text else "no_shared_data",
        )
        return

    if not line_user_id:
        return

    link = line_link_tokens.get_link(line_user_id)
    if not link:
        # Not yet linked → return connect URL.
        try:
            token = line_link_tokens.issue(line_user_id=line_user_id, line_source_type="user")
        except Exception as e:
            logger.warning("[line.webhook] link token issue failed: %s", e)
            line_messaging.reply(reply_token, [line_messaging.text_message(M.CONFIG_MISSING)])
            bot_audit.record(
                provider="line", source_type="user",
                source_user_id=line_user_id, command="unknown",
                outcome="config_missing",
            )
            return
        text = f"{M.NOT_LINKED_INTRO}\n\n{_connect_url(token)}"
        line_messaging.reply(reply_token, [line_messaging.text_message(text)])
        bot_audit.record(
            provider="line", source_type="user",
            source_user_id=line_user_id, command="unknown",
            outcome="unlinked",
        )
        return

    command = _classify_command(user_text)
    text = _build_reply_for_linked(link["accountId"], command)
    line_messaging.reply(reply_token, [line_messaging.text_message(text)])
    bot_audit.record(
        provider="line", source_type="user",
        source_user_id=line_user_id,
        account_id=link.get("accountId"),
        deepnote_uid=link.get("deepnoteUid"),
        command=command, outcome="ok",
    )


def _handle_follow_event(event: Dict[str, Any]) -> None:
    source = event.get("source") or {}
    if source.get("type") != "user":
        return
    line_user_id = source.get("userId")
    reply_token = event.get("replyToken")
    if not line_user_id or not reply_token:
        return
    if line_link_tokens.get_link(line_user_id):
        line_messaging.reply(reply_token, [line_messaging.text_message(M.HELP)])
        return
    try:
        token = line_link_tokens.issue(line_user_id=line_user_id, line_source_type="user")
    except Exception as e:
        logger.warning("[line.webhook] follow link token issue failed: %s", e)
        line_messaging.reply(reply_token, [line_messaging.text_message(M.CONFIG_MISSING)])
        return
    text = f"{M.NOT_LINKED_INTRO}\n\n{_connect_url(token)}"
    line_messaging.reply(reply_token, [line_messaging.text_message(text)])


# ──────────────────────────────────────────────────────────────────────
# Webhook
# ──────────────────────────────────────────────────────────────────────

@router.post("/webhook", include_in_schema=False)
async def line_webhook(
    request: Request,
    x_line_signature: Optional[str] = Header(None, alias="X-Line-Signature"),
):
    body = await request.body()
    if not line_messaging.is_configured():
        # Don't crash the whole service when LINE isn't configured.
        # Returning 503 makes "Verify" in the LINE Developers Console fail loudly.
        raise HTTPException(status_code=503, detail="line_messaging_not_configured")
    if not line_messaging.verify_signature(body=body, header_signature=x_line_signature or ""):
        raise HTTPException(status_code=401, detail="invalid_signature")

    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except Exception:
        raise HTTPException(status_code=400, detail="malformed_body")

    events = payload.get("events") or []
    for ev in events:
        ev_type = ev.get("type")
        source_type = (ev.get("source") or {}).get("type")
        logger.info(
            "[line.webhook] event=%s source=%s userId=%s",
            ev_type, source_type, (ev.get("source") or {}).get("userId"),
        )
        try:
            if ev_type == "message":
                _handle_message_event(ev)
            elif ev_type == "follow":
                _handle_follow_event(ev)
            elif ev_type in ("join", "memberJoined"):
                # Group / room joined → Phase 1 unsupported notice.
                rt = ev.get("replyToken")
                if rt:
                    line_messaging.reply(rt, [line_messaging.text_message(M.GROUP_NOT_SUPPORTED)])
            else:
                # postback / unfollow / leave / etc. — log + ignore for Phase 1.
                pass
        except Exception as e:
            logger.exception("[line.webhook] event handler failed: %s", e)
            # Never propagate per-event failures back to LINE; the platform
            # retries quickly and would amplify noise.
    return JSONResponse({"status": "ok"})


# ──────────────────────────────────────────────────────────────────────
# Link token API
# ──────────────────────────────────────────────────────────────────────

class LinkTokenIssueRequest(BaseModel):
    lineUserId: str
    lineGroupId: Optional[str] = None
    lineSourceType: str = "user"


@router.post("/link-tokens", include_in_schema=False)
async def issue_link_token_internal(
    request: Request,
    body: LinkTokenIssueRequest,
):
    """Internal helper. Caller authenticates via X-Internal-Token header
    matching LINE_INTERNAL_TOKEN env, OR runs from a trusted backend
    context (Cloud Tasks, etc.). For Phase 1 we keep it permissive when
    no internal token is configured but log a warning."""
    expected = os.environ.get("LINE_INTERNAL_TOKEN")
    provided = request.headers.get("X-Internal-Token")
    if expected and not (provided and provided == expected):
        raise HTTPException(status_code=401, detail="invalid_internal_token")
    if not expected:
        logger.warning("[line.link_tokens] LINE_INTERNAL_TOKEN not set — endpoint is unauthenticated")
    token = line_link_tokens.issue(
        line_user_id=body.lineUserId,
        line_group_id=body.lineGroupId,
        line_source_type=body.lineSourceType,
    )
    return {"token": token, "connectUrl": _connect_url(token)}


@router.get("/link-tokens/{token}")
async def resolve_link_token(token: str):
    """Public-but-narrow: returns only that the token is valid + the
    line_user_id it represents. Does NOT expose any DeepNote user data."""
    try:
        data = line_link_tokens.resolve(token)
    except line_link_tokens.TokenError as e:
        raise HTTPException(status_code=e.status, detail=e.code)
    return {
        "token": token,
        "lineUserId": data.get("lineUserId"),
        "lineSourceType": data.get("lineSourceType"),
        "expiresAt": data.get("expiresAt"),
    }


@router.post("/link-tokens/{token}:consume")
async def consume_link_token(
    token: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Consume the token and persist line_user_id ↔ deepnote uid link.

    Caller must present a valid Firebase ID token (Authorization: Bearer …)
    just like the rest of the API."""
    try:
        data = line_link_tokens.consume(
            token,
            deepnote_uid=current_user.uid,
            account_id=current_user.account_id,
        )
    except line_link_tokens.TokenError as e:
        raise HTTPException(status_code=e.status, detail=e.code)
    return {
        "linked": True,
        "lineUserId": data["lineUserId"],
        "lineSourceType": data.get("lineSourceType"),
    }


# ──────────────────────────────────────────────────────────────────────
# Connect HTML
# ──────────────────────────────────────────────────────────────────────

_HTML_TEMPLATE = """<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Hiragino Kaku Gothic ProN", sans-serif;
          padding: 24px; line-height: 1.6; color: #1a1a1a; background: #fafafa; }}
  h1 {{ font-size: 18px; margin: 0 0 16px; }}
  p  {{ margin: 0 0 12px; font-size: 14px; }}
  .url {{ font-family: ui-monospace, monospace; word-break: break-all;
          background: #fff; padding: 12px; border: 1px solid #e0e0e0; border-radius: 8px;
          font-size: 13px; }}
  button {{ margin-top: 12px; padding: 12px 16px; font-size: 14px;
            background: #06c755; color: #fff; border: none; border-radius: 8px; }}
  .err {{ color: #b00020; }}
</style>
</head><body>
<h1>{title}</h1>
{body_html}
</body></html>
"""


def _render_html(*, title: str, body_html: str, status: int = 200) -> HTMLResponse:
    html = _HTML_TEMPLATE.format(title=title, body_html=body_html)
    return HTMLResponse(content=html, status_code=status)


def _render_inapp_copy_page(connect_url: str) -> HTMLResponse:
    body = f"""
    <p>{M.CONNECT_INTRO}</p>
    <p class="url" id="u">{connect_url}</p>
    <button onclick="navigator.clipboard&&navigator.clipboard.writeText(document.getElementById('u').innerText)">
      {M.CONNECT_COPY_LABEL}
    </button>
    <p>{M.CONNECT_COPY_HINT}</p>
    """
    return _render_html(title=M.CONNECT_PAGE_TITLE, body_html=body)


def _render_invalid_page() -> HTMLResponse:
    body = f"<p class=\"err\">{M.CONNECT_INVALID}</p>"
    return _render_html(title=M.CONNECT_PAGE_TITLE, body_html=body, status=400)


def _render_login_prompt_page(token: str) -> HTMLResponse:
    """Redirect-style fallback page: send the user to the backend-hosted
    Firebase login (Phase 5). If the user lands on the connect URL from a
    desktop browser and no LINE_CONNECT_FRONTEND_URL is configured, we
    self-host the login flow at /integrations/line/login?token=...
    """
    body = (
        f"<p>{M.CONNECT_LOGIN_PROMPT}</p>"
        f"<p><a href=\"/integrations/line/login?token={token}\">DeepNote にログインして連携を完了する</a></p>"
    )
    return _render_html(title=M.CONNECT_PAGE_TITLE, body_html=body)


@router.get("/connect", include_in_schema=False)
async def line_connect_page(
    request: Request,
    token: str = Query(...),
    user_agent: Optional[str] = Header(None, alias="User-Agent"),
):
    """Entry page for LINE bot connect URLs.

    Behavior matrix:
      LINE in-app browser (UA contains "Line"): always show URL-copy page.
      Otherwise + LINE_CONNECT_FRONTEND_URL set : 302 to frontend?token=...&line=1
      Otherwise (no frontend configured)        : show fallback login-prompt page.
    """
    # Validate token early so we don't redirect into a broken flow.
    try:
        line_link_tokens.resolve(token)
    except line_link_tokens.TokenError:
        return _render_invalid_page()

    public_url = f"{_public_base_url()}/integrations/line/connect?{urlencode({'token': token})}"

    if _is_line_inapp(user_agent):
        return _render_inapp_copy_page(public_url)

    frontend = _frontend_login_url()
    if frontend:
        sep = "&" if "?" in frontend else "?"
        return RedirectResponse(f"{frontend}{sep}{urlencode({'lineToken': token})}")

    return _render_login_prompt_page(token)
