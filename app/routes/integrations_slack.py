"""Slack bot endpoints (Phase 1, 1:1 DM only).

Routes
  - POST /integrations/slack/events                     (signed, hidden)
  - GET  /integrations/slack/oauth/start                (workspace install)
  - GET  /integrations/slack/oauth/callback             (state + token save)
  - GET  /integrations/slack/link-tokens/{token}        (frontend resolver)
  - POST /integrations/slack/link-tokens/{token}:consume (Firebase Bearer)
  - GET  /integrations/slack/connect?token=...          (HTML, hidden)

Channel / group messages get the SLACK_GROUP_NOT_SUPPORTED reply only —
no personal data leaks across non-DM contexts.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse

from app.dependencies import CurrentUser, get_current_user
from app.services import slack_link_tokens
from app.services import slack_briefing
from app.services import slack_oauth_state
from app.services import asset_delivery
from app.services import bot_audit
from app.services.integrations import slack_client

logger = logging.getLogger("app.routes.integrations.slack")
router = APIRouter(prefix="/integrations/slack", tags=["Integrations:Slack"])


# ──────────────────────────────────────────────────────────────────────
# Configuration helpers
# ──────────────────────────────────────────────────────────────────────

def _public_base_url() -> str:
    return (
        os.environ.get("SLACK_PUBLIC_BASE_URL")
        or os.environ.get("LINE_PUBLIC_BASE_URL")
        or os.environ.get("CLOUD_RUN_SERVICE_URL")
        or "https://deepnote-api-mur5rvqgga-an.a.run.app"
    ).rstrip("/")


def _frontend_login_url() -> Optional[str]:
    raw = os.environ.get("SLACK_CONNECT_FRONTEND_URL") or os.environ.get("LINE_CONNECT_FRONTEND_URL")
    return raw.strip() if raw and raw.strip() else None


def _is_slack_inapp(user_agent: Optional[str]) -> bool:
    if not user_agent:
        return False
    return "Slack" in user_agent  # Slack mobile in-app browser sets "Slack/" UA


# ──────────────────────────────────────────────────────────────────────
# Phase 1 message constants (centralised)
# ──────────────────────────────────────────────────────────────────────

class M:
    HELP = (
        "DeepNote と Slack の連携BOTです。\n"
        "DM で次の言葉を送ると応答します:\n"
        "・「クレジット」: あなたのDeepNoteアカウントのクレジット残量\n"
        "・「最新」: 最新の会議の要約\n"
        "・「TODO」: 直近のTODO（最大3件）\n"
        "・「決定事項」: 最新会議の決定事項\n"
        "・「資料」: 最新会議の PDF / DOCX / PPTX リンク\n"
        "・「PDF」「DOCX」「PPTX」: 個別フォーマット\n"
        "・「ヘルプ」: この案内"
    )

    NOT_LINKED_INTRO = (
        "DeepNote と Slack の連携が必要です。\n"
        "下のリンクを開いて DeepNote にログインしてください。"
    )

    GROUP_NOT_SUPPORTED = (
        "現在、チャンネルでのご利用には未対応です。\n"
        "DeepNote とのSlack DMでご利用ください。"
    )

    UNKNOWN_COMMAND = "認識できませんでした。「ヘルプ」と送ると使い方が表示されます。"

    CREDIT_TEMPLATE = (
        "あなたのDeepNoteアカウントのクレジット残量です。\n"
        "プラン: {plan}\n"
        "残量: {remaining} / {monthly_limit}\n"
        "{topup_line}"
    )
    CREDIT_UNLIMITED = "あなたのDeepNoteアカウントは無制限プランです。"
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
        "サーバー設定が未完了のため、Slack連携をご案内できません。"
        "管理者にお問い合わせください。"
    )

    INSTALL_OK = "DeepNote と Slack の連携が完了しました。Slack の DeepNote bot に DM を送ってご利用ください。"
    INSTALL_FAILED = "Slack 連携に失敗しました: {reason}"

    CONNECT_PAGE_TITLE = "DeepNote と Slack の連携"
    CONNECT_INTRO = (
        "DeepNote と Slack を連携します。Safari または Chrome で開いてください。"
    )
    CONNECT_COPY_LABEL = "URLをコピー"
    CONNECT_COPY_HINT = "コピー後、Safari または Chrome に貼り付けて開いてください。"
    CONNECT_INVALID = "リンクの有効期限が切れたか、無効です。Slack で再度連携をお試しください。"
    CONNECT_LOGIN_PROMPT = "DeepNote にログインして連携を完了してください。"


# ──────────────────────────────────────────────────────────────────────
# Helpers shared with handlers
# ──────────────────────────────────────────────────────────────────────

def _connect_url(token: str) -> str:
    return f"{_public_base_url()}/integrations/slack/connect?{urlencode({'token': token})}"


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


def _format_latest(latest):
    if not latest:
        return M.LATEST_NONE
    summary = (latest.get("summary") or "").strip() or "（要約はまだ生成されていません）"
    if len(summary) > 1000:
        summary = summary[:997] + "..."
    return M.LATEST_TEMPLATE.format(title=latest.get("title") or "(無題)", summary=summary)


def _format_todos(todos):
    if not todos:
        return M.TODOS_NONE
    from datetime import datetime
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


def _format_decisions(decisions):
    if not decisions:
        return M.DECISIONS_NONE
    return "\n".join([M.DECISIONS_HEADER] + [M.DECISIONS_LINE.format(text=d) for d in decisions])


def _classify_command(text: str) -> str:
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
    if "pdf" in t:
        return "pdf"
    if "docx" in t or "ワード" in t or "word" in t:
        return "docx"
    if "pptx" in t or "パワポ" in t or "ppt" in t or "powerpoint" in t:
        return "pptx"
    if "資料" in t or "asset" in t:
        return "assets"
    return "unknown"


def _build_reply_for_linked(account_id: str, command: str) -> str:
    if command == "help":
        return M.HELP
    if command == "credit":
        report = slack_briefing.get_credit_summary(account_id)
        return M.CREDIT_FAILED if not report else _format_credit(report)
    if command == "latest":
        return _format_latest(slack_briefing.get_latest_session(account_id))
    if command == "todos":
        return _format_todos(slack_briefing.get_recent_todos(account_id, limit=3))
    if command == "decisions":
        return _format_decisions(slack_briefing.get_latest_decisions(account_id))
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


# ──────────────────────────────────────────────────────────────────────
# Events API
# ──────────────────────────────────────────────────────────────────────

def _strip_app_mentions(text: str) -> str:
    """Remove leading <@U…> bot mention so command classifier sees the verb."""
    if not text:
        return text
    out = text
    while out.startswith("<@"):
        end = out.find(">")
        if end < 0:
            break
        out = out[end + 1:].lstrip()
    return out


def _handle_message_event(team_id: str, event: Dict[str, Any]) -> None:
    channel = event.get("channel")
    channel_type = event.get("channel_type")
    user = event.get("user")
    text = event.get("text") or ""
    thread_ts = event.get("thread_ts") or event.get("ts")

    if not user or not channel:
        return
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return  # never reply to bots / our own posts

    if channel_type != "im":
        # Channel / group / mpim → polite refusal, no personal data.
        if event.get("type") == "app_mention":
            slack_client.post_message(team_id=team_id, channel=channel,
                                      text=M.GROUP_NOT_SUPPORTED, thread_ts=thread_ts)
        bot_audit.record(
            provider="slack", source_type=channel_type or "unknown",
            source_user_id=user, team_id=team_id, command="unsupported",
            outcome="blocked_unsupported_source",
        )
        return

    link = slack_link_tokens.get_link(team_id, user)
    if not link:
        try:
            token = slack_link_tokens.issue(team_id=team_id, slack_user_id=user, slack_channel_id=channel)
        except Exception as e:
            logger.warning("[slack.events] link token issue failed: %s", e)
            slack_client.post_message(team_id=team_id, channel=channel, text=M.CONFIG_MISSING)
            bot_audit.record(
                provider="slack", source_type="im",
                source_user_id=user, team_id=team_id, command="unknown",
                outcome="config_missing",
            )
            return
        slack_client.post_message(
            team_id=team_id, channel=channel,
            text=f"{M.NOT_LINKED_INTRO}\n\n{_connect_url(token)}",
        )
        bot_audit.record(
            provider="slack", source_type="im",
            source_user_id=user, team_id=team_id, command="unknown",
            outcome="unlinked",
        )
        return

    command = _classify_command(_strip_app_mentions(text))
    reply = _build_reply_for_linked(link["accountId"], command)
    slack_client.post_message(team_id=team_id, channel=channel, text=reply)
    bot_audit.record(
        provider="slack", source_type="im",
        source_user_id=user, team_id=team_id,
        account_id=link.get("accountId"),
        deepnote_uid=link.get("deepnoteUid"),
        command=command, outcome="ok",
    )


@router.post("/events", include_in_schema=False)
async def slack_events(
    request: Request,
    x_slack_request_timestamp: Optional[str] = Header(None, alias="X-Slack-Request-Timestamp"),
    x_slack_signature: Optional[str] = Header(None, alias="X-Slack-Signature"),
):
    body = await request.body()
    if not slack_client.is_configured():
        raise HTTPException(status_code=503, detail="slack_not_configured")
    if not slack_client.verify_signature(
        body=body,
        timestamp=x_slack_request_timestamp or "",
        signature=x_slack_signature or "",
    ):
        raise HTTPException(status_code=401, detail="invalid_signature")

    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except Exception:
        raise HTTPException(status_code=400, detail="malformed_body")

    # URL verification handshake (one-time when configuring the request URL).
    if payload.get("type") == "url_verification":
        return PlainTextResponse(payload.get("challenge", ""))

    if payload.get("type") != "event_callback":
        return JSONResponse({"status": "ignored"})

    team_id = payload.get("team_id") or payload.get("team", {}).get("id") or ""
    event = payload.get("event") or {}
    ev_type = event.get("type")
    logger.info(
        "[slack.events] team=%s type=%s channel_type=%s user=%s",
        team_id, ev_type, event.get("channel_type"), event.get("user"),
    )
    try:
        if ev_type in ("message", "app_mention"):
            _handle_message_event(team_id, event)
        else:
            pass  # other event types ignored in Phase 1
    except Exception as e:
        logger.exception("[slack.events] handler failed: %s", e)
    return JSONResponse({"status": "ok"})


# ──────────────────────────────────────────────────────────────────────
# Workspace OAuth install
# ──────────────────────────────────────────────────────────────────────

@router.get("/oauth/start", include_in_schema=False)
async def slack_oauth_start(return_to: str = "/"):
    if not slack_client.is_configured():
        raise HTTPException(status_code=503, detail="slack_not_configured")
    state = slack_oauth_state.issue(return_to=return_to)
    params = {
        "client_id": slack_client.CLIENT_ID,
        "scope": slack_client.SCOPES,
        "redirect_uri": slack_client.REDIRECT_URI,
        "state": state,
    }
    return RedirectResponse(slack_client.SLACK_AUTH_URL + "?" + urlencode(params))


@router.get("/oauth/callback", include_in_schema=False)
async def slack_oauth_callback(code: str = Query(...), state: str = Query(...)):
    if not slack_client.is_configured():
        raise HTTPException(status_code=503, detail="slack_not_configured")
    try:
        slack_oauth_state.consume(state)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"state_invalid:{e}")
    try:
        body = slack_client.exchange_code(code)
    except slack_client.SlackAuthError as e:
        return HTMLResponse(
            f"<html><body><h1>Slack 連携エラー</h1><p>{M.INSTALL_FAILED.format(reason=str(e))}</p></body></html>",
            status_code=400,
        )
    try:
        slack_client.save_workspace(body)
    except slack_client.SlackAuthError as e:
        return HTMLResponse(
            f"<html><body><h1>Slack 連携エラー</h1><p>{M.INSTALL_FAILED.format(reason=str(e))}</p></body></html>",
            status_code=400,
        )
    return HTMLResponse(
        f"<html><body><h1>{M.CONNECT_PAGE_TITLE}</h1><p>{M.INSTALL_OK}</p></body></html>"
    )


# ──────────────────────────────────────────────────────────────────────
# Link-token API (mirror of LINE)
# ──────────────────────────────────────────────────────────────────────

@router.get("/link-tokens/{token}")
async def resolve_slack_link_token(token: str):
    try:
        data = slack_link_tokens.resolve(token)
    except slack_link_tokens.TokenError as e:
        raise HTTPException(status_code=e.status, detail=e.code)
    return {
        "token": token,
        "teamId": data.get("teamId"),
        "slackUserId": data.get("slackUserId"),
        "expiresAt": data.get("expiresAt"),
    }


@router.post("/link-tokens/{token}:consume")
async def consume_slack_link_token(
    token: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        data = slack_link_tokens.consume(
            token,
            deepnote_uid=current_user.uid,
            account_id=current_user.account_id,
        )
    except slack_link_tokens.TokenError as e:
        raise HTTPException(status_code=e.status, detail=e.code)
    return {
        "linked": True,
        "teamId": data["teamId"],
        "slackUserId": data["slackUserId"],
    }


# ──────────────────────────────────────────────────────────────────────
# Connect HTML (frontend bridge)
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
            background: #4a154b; color: #fff; border: none; border-radius: 8px; }}
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
    return _render_html(title=M.CONNECT_PAGE_TITLE,
                        body_html=f"<p class=\"err\">{M.CONNECT_INVALID}</p>",
                        status=400)


def _render_login_prompt_page(token: str) -> HTMLResponse:
    body = f"<p>{M.CONNECT_LOGIN_PROMPT}</p><p class=\"url\">token: {token}</p>"
    return _render_html(title=M.CONNECT_PAGE_TITLE, body_html=body)


@router.get("/connect", include_in_schema=False)
async def slack_connect_page(
    token: str = Query(...),
    user_agent: Optional[str] = Header(None, alias="User-Agent"),
):
    try:
        slack_link_tokens.resolve(token)
    except slack_link_tokens.TokenError:
        return _render_invalid_page()

    public_url = f"{_public_base_url()}/integrations/slack/connect?{urlencode({'token': token})}"

    if _is_slack_inapp(user_agent):
        return _render_inapp_copy_page(public_url)

    frontend = _frontend_login_url()
    if frontend:
        sep = "&" if "?" in frontend else "?"
        return RedirectResponse(f"{frontend}{sep}{urlencode({'slackToken': token})}")

    return _render_login_prompt_page(token)
