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
from app.services import group_shared_briefing
from app.services import group_acl
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
    GROUP_NO_SHARED_DATA = (
        "このチャンネルに共有された会議はまだありません。\n"
        "（プライバシー保護のため、共有マークが付いた会議だけがこちらに表示されます）\n\n"
        "▼ 共有したい会議がある場合\n"
        "DeepNoteアプリ → 会議 → 共有 → 「このワークスペースに共有」\n\n"
        "▼ 要約完了の通知だけ受け取りたい場合\n"
        "DeepNote の Slack DM で「通知 ON」と送ってください（DM のみへ通知。チャンネルには自動投稿しません）。"
    )
    GROUP_NO_SHARED_DATA_WITH_HINT = (
        "このチャンネルに共有された会議はまだありません。\n"
        "（最新の会議「{title}」は未共有です）\n\n"
        "▼ この会議を共有するには\n"
        "DeepNoteアプリ → 会議「{title}」→ 共有 → 「このワークスペースに共有」\n\n"
        "▼ 要約完了の通知だけ受け取りたい場合\n"
        "DM で「通知 ON」と送ってください（自動でチャンネルには投稿しません）。"
    )
    AUTO_SHARE_DEPRECATED = (
        "🛑 「自動共有」(チャンネル自動投稿) は安全のため廃止しました。\n"
        "AI が生成した要約をユーザー確認なしにチャンネル投稿すると、誤認識・社外秘・個人情報を意図せず共有してしまう恐れがあるためです。\n\n"
        "代わりに次の安全な選択肢をご利用ください：\n"
        "・「通知 ON」(DMのみ): 要約完了をあなたの DM だけに通知\n"
        "・「自分要約 ON」(DMのみ): 要約 + TODO 概要をあなたの DM に送信\n"
        "・チームへ共有したい場合は、DeepNoteアプリで明示的にボタンを押して共有してください"
    )
    NOTIFY_ENABLED = (
        "✅ 要約完了通知を有効にしました。\n"
        "今後 DeepNote で記録した会議の要約が完了したら、この DM に通知します。\n"
        "（チャンネルには自動投稿しません。停止するには「通知 OFF」）"
    )
    NOTIFY_DISABLED = "🛑 要約完了通知を停止しました。"
    NOTIFY_ALREADY_ON = "要約完了通知は既に有効です。"
    NOTIFY_ALREADY_OFF = "要約完了通知は既にオフです。"
    DIGEST_ENABLED = (
        "✅ 自分要約を有効にしました。\n"
        "今後の会議の要約 + TODO 概要を、あなたの DM に送信します。\n"
        "（チャンネルには自動投稿しません。停止するには「自分要約 OFF」）"
    )
    DIGEST_DISABLED = "🛑 自分要約の自動送信を停止しました。"
    DIGEST_ALREADY_ON = "自分要約は既に有効です。"
    DIGEST_ALREADY_OFF = "自分要約は既にオフです。"
    SMART_SHARE_HELP = (
        "▼ Smart Share コマンド (DM で送信)\n"
        "・「通知 ON」: 要約完了を DM に通知 (内容は短く)\n"
        "・「自分要約 ON」: 要約 + TODO 概要を DM に送信\n"
        "・「通知 OFF」「自分要約 OFF」: 各停止\n"
        "▼ チーム共有はアプリから\n"
        "DeepNoteアプリ → 会議 → 共有 → 「このワークスペースに共有」を押してください。\n"
        "プライバシー保護のため、チャンネルへの自動投稿は行いません。"
    )
    GROUP_PRIVATE_REJECTED = (
        "クレジット残量や TODO は個人情報のため、チャンネルでは表示できません。\n"
        "DeepNote との DM でご確認ください。"
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
    if t in {
        "こんにちは", "こんばんは", "おはよう", "おはよ", "やあ", "どうも",
        "hello", "hi", "hey",
    }:
        return "greeting"
    # Forward any "?" / 「質問」 / "ask" to the Assistant Hub.
    if t.endswith("?") or t.endswith("？") or t.startswith("質問") or t.startswith("ask "):
        return "assistant_qna"
    if any(k in t for k in ("ヘルプ", "help", "使い方", "?", "？")):
        return "help"
    # Phase 1 channel ACL commands. Require an explicit DeepNote/Clow
    # mention so bare 「接続」 in chatter doesn't trigger admin ops.
    has_bot = any(k in t for k in ("deepnote", "ディープノート", "clow", "クロウ"))
    if has_bot and any(k in t for k in ("接続", "connect", "リンク", "link")):
        return "group_connect"
    if has_bot and any(k in t for k in ("切断", "解除", "disconnect", "unlink")):
        return "group_disconnect"
    if has_bot and any(k in t for k in ("メンバー追加", "管理者追加", "promote", "member add", "admin add")):
        return "group_member_add"
    if has_bot and any(k in t for k in ("メンバー削除", "管理者削除", "demote", "member remove", "admin remove")):
        return "group_member_remove"
    if has_bot and any(k in t for k in ("状態", "ステータス", "status")):
        return "group_status"
    if "自動共有" in t or "auto share" in t or "auto-share" in t or "autoshare" in t:
        # Lv4 retired (safety). Always answer with the migration notice.
        return "auto_share_deprecated"
    if t.startswith("通知") or t == "通知" or "notify" in t:
        if any(on in t for on in ("on", "オン", "有効", "enable")):
            return "notify_on"
        if any(off in t for off in ("off", "オフ", "無効", "停止", "disable")):
            return "notify_off"
        return "notify_status"
    if "自分要約" in t or "dm digest" in t or "self digest" in t:
        if any(on in t for on in ("on", "オン", "有効", "enable")):
            return "digest_on"
        if any(off in t for off in ("off", "オフ", "無効", "停止", "disable")):
            return "digest_off"
        return "digest_status"
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


def _build_reply_for_linked(account_id: str, command: str, *, slack_user_id: str = "", raw_text: str = "") -> str:
    if command == "greeting":
        return (
            "こんにちは。DeepNote Clow です。\n"
            "「最新」「TODO」「決定事項」「資料」「PDF」「クレジット」「ヘルプ」と送ってください。"
        )
    if command == "help":
        return M.HELP + "\n\n" + M.SMART_SHARE_HELP
    if command == "auto_share_deprecated":
        return M.AUTO_SHARE_DEPRECATED
    if command == "assistant_qna":
        q = (raw_text or "").strip()
        for prefix in ("質問:", "質問：", "質問", "ask "):
            if q.startswith(prefix):
                q = q[len(prefix):].strip()
                break
        if not q:
            return "質問を入力してください。例: 「決定事項は？」「TODO は？」"
        try:
            import asyncio
            from app.services import assistant_hub
            result = asyncio.run(assistant_hub.handle_message(
                account_id=account_id, owner_uid=slack_user_id, question=q,
                session_id=None, mode="session", channel="slack",
                idempotency_key=None,
            ))
            return result.get("answer") or "回答を生成できませんでした。"
        except Exception as _e:
            logger.warning("[slack.qna] hub call failed: %s", _e)
            return "Assistant へのリクエストに失敗しました。少し時間をおいて再度お試しください。"
    if command in ("notify_on", "notify_off", "notify_status"):
        from app.services import bot_smart_share
        if command == "notify_on":
            changed = bot_smart_share.set_notify("slack", slack_user_id, True)
            return M.NOTIFY_ENABLED if changed else M.NOTIFY_ALREADY_ON
        if command == "notify_off":
            changed = bot_smart_share.set_notify("slack", slack_user_id, False)
            return M.NOTIFY_DISABLED if changed else M.NOTIFY_ALREADY_OFF
        s = bot_smart_share.get_settings("slack", slack_user_id)
        return ("✅ 通知: 有効" if s["notifyOnSummaryReady"] else "⏸ 通知: オフ") + "\n\n" + M.SMART_SHARE_HELP
    if command in ("digest_on", "digest_off", "digest_status"):
        from app.services import bot_smart_share
        if command == "digest_on":
            changed = bot_smart_share.set_dm_digest("slack", slack_user_id, True)
            return M.DIGEST_ENABLED if changed else M.DIGEST_ALREADY_ON
        if command == "digest_off":
            changed = bot_smart_share.set_dm_digest("slack", slack_user_id, False)
            return M.DIGEST_DISABLED if changed else M.DIGEST_ALREADY_OFF
        s = bot_smart_share.get_settings("slack", slack_user_id)
        return ("✅ 自分要約: 有効" if s["dmDigestOnSummary"] else "⏸ 自分要約: オフ") + "\n\n" + M.SMART_SHARE_HELP
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


# ──────────────────────────────────────────────────────────────────────
# Phase 1 channel ACL command handlers (Slack)
# ──────────────────────────────────────────────────────────────────────
#
# Slack workspace_id format = ``f"{team_id}:{channel_id}"`` so each
# channel inside a workspace gets its own ACL row — same shape as
# LINE groups.

def _slack_workspace_id(team_id: str, channel_id: str) -> str:
    return f"{team_id}:{channel_id}"


def _slack_post(team_id: str, channel: str, text: str, thread_ts: Optional[str] = None) -> None:
    slack_client.post_message(team_id=team_id, channel=channel, text=text, thread_ts=thread_ts)


def _handle_slack_group_connect(team_id: str, channel: str, user: str, thread_ts: Optional[str]) -> None:
    requester = slack_link_tokens.get_link(team_id, user)
    if not requester:
        _slack_post(team_id, channel,
            "DeepNote と Slack の連携が必要です。\n"
            "DM で DeepNote と連携した後、もう一度このチャンネルで「DeepNote 接続」と送ってください。",
            thread_ts)
        bot_audit.record(provider="slack", source_type="channel",
                         source_user_id=user, team_id=team_id,
                         command="group_connect", outcome="requester_not_linked")
        return
    ws_id = _slack_workspace_id(team_id, channel)
    existing = group_acl.get_group_link("slack", ws_id)
    if existing:
        _slack_post(team_id, channel,
            "このチャンネルは既に DeepNote と接続されています。\n"
            "「DeepNote 状態」で現在の設定を確認できます。", thread_ts)
        bot_audit.record(provider="slack", source_type="channel",
                         source_user_id=user, team_id=team_id,
                         account_id=existing.get("ownerAccountId"),
                         command="group_connect", outcome="already_connected")
        return
    try:
        group_acl.create_group_link(
            "slack", ws_id,
            owner_deepnote_uid=requester.get("deepnoteUid", ""),
            owner_account_id=requester["accountId"],
            created_by_source_user_id=user,
        )
    except Exception as e:
        logger.warning("[slack.group_connect] create_link failed: %s", e)
        _slack_post(team_id, channel,
            "チャンネル接続に失敗しました。少し時間をおいてから再度お試しください。", thread_ts)
        return
    limits = group_acl.daily_limits()
    _slack_post(team_id, channel,
        "✅ DeepNote をこのチャンネルに接続しました。\n"
        f"・代表アカウント: {requester['accountId'][:8]}…\n"
        f"・1日の利用上限: {limits['max_runs']} 回 (うち AI 質問は {limits['max_paid_runs']} 回まで)\n"
        "・他のメンバーは「最新」「決定事項」など読み取り操作のみ可能です\n"
        "・AI 質問はオーナー / 管理者のみ実行できます\n"
        "「DeepNote メンバー追加 @user」で管理者を追加できます。", thread_ts)
    bot_audit.record(provider="slack", source_type="channel",
                     source_user_id=user, team_id=team_id,
                     account_id=requester["accountId"],
                     deepnote_uid=requester.get("deepnoteUid"),
                     command="group_connect", outcome="ok")


def _handle_slack_group_status(team_id: str, channel: str, user: str, thread_ts: Optional[str]) -> None:
    ws_id = _slack_workspace_id(team_id, channel)
    glink = group_acl.get_group_link("slack", ws_id)
    if not glink:
        _slack_post(team_id, channel,
            "このチャンネルはまだ DeepNote と接続されていません。\n"
            "「DeepNote 接続」と送って代表アカウントを登録してください。", thread_ts)
        return
    me = group_acl.get_member("slack", ws_id, user)
    role = (me or {}).get("role", "未登録")
    members = group_acl.list_members("slack", ws_id, limit=20)
    owner_count = sum(1 for m in members if m.get("role") == "owner")
    admin_count = sum(1 for m in members if m.get("role") == "admin")
    member_count = sum(1 for m in members if m.get("role") == "member")
    limits = group_acl.daily_limits()
    _slack_post(team_id, channel,
        "📋 DeepNote 接続状態\n"
        f"・代表アカウント: {glink.get('ownerAccountId', '')[:8]}…\n"
        f"・あなたのロール: {role}\n"
        f"・メンバー数: owner {owner_count} / admin {admin_count} / member {member_count}\n"
        f"・1日の利用上限: {limits['max_runs']} 回 / AI 質問 {limits['max_paid_runs']} 回\n"
        f"・1人当たり上限: {limits['max_runs_per_user']} 回",
        thread_ts)
    bot_audit.record(provider="slack", source_type="channel",
                     source_user_id=user, team_id=team_id,
                     account_id=glink.get("ownerAccountId"),
                     command="group_status", outcome="ok")


def _handle_slack_group_disconnect(team_id: str, channel: str, user: str, thread_ts: Optional[str]) -> None:
    ws_id = _slack_workspace_id(team_id, channel)
    glink = group_acl.get_group_link("slack", ws_id)
    if not glink:
        _slack_post(team_id, channel, "このチャンネルは接続されていません。", thread_ts)
        return
    me = group_acl.get_member("slack", ws_id, user)
    if not me or me.get("role") != "owner":
        _slack_post(team_id, channel, "切断は owner ロールのみ実行できます。", thread_ts)
        bot_audit.record(provider="slack", source_type="channel",
                         source_user_id=user, team_id=team_id,
                         account_id=glink.get("ownerAccountId"),
                         command="group_disconnect", outcome="blocked_not_owner")
        return
    group_acl.deactivate_group_link("slack", ws_id)
    _slack_post(team_id, channel,
        "✅ DeepNote とこのチャンネルの接続を解除しました。\n"
        "再度接続したい場合は「DeepNote 接続」と送ってください。", thread_ts)
    bot_audit.record(provider="slack", source_type="channel",
                     source_user_id=user, team_id=team_id,
                     account_id=glink.get("ownerAccountId"),
                     command="group_disconnect", outcome="ok")


def _extract_slack_user_mention(text: str) -> Optional[str]:
    """``<@U0123ABCDE>`` → ``"U0123ABCDE"`` (Slack mention syntax)."""
    import re
    m = re.search(r"<@([A-Z0-9]+)(?:\|[^>]*)?>", text or "")
    if m:
        return m.group(1)
    m2 = re.search(r"\b(U[A-Z0-9]{8,})\b", text or "")
    return m2.group(1) if m2 else None


def _handle_slack_group_member_add(team_id: str, channel: str, user: str, text: str, thread_ts: Optional[str]) -> None:
    ws_id = _slack_workspace_id(team_id, channel)
    glink = group_acl.get_group_link("slack", ws_id)
    if not glink:
        _slack_post(team_id, channel,
            "先に「DeepNote 接続」でチャンネルを接続してください。", thread_ts)
        return
    me = group_acl.get_member("slack", ws_id, user)
    if not me or me.get("role") != "owner":
        _slack_post(team_id, channel,
            "メンバー追加は owner ロールのみ実行できます。", thread_ts)
        return
    target = _extract_slack_user_mention(text)
    if not target:
        _slack_post(team_id, channel,
            "対象ユーザーを @メンション で指定してください。\n"
            "例: 「@DeepNote メンバー追加 @taka」", thread_ts)
        return
    target_link = slack_link_tokens.get_link(team_id, target)
    group_acl.set_member_role(
        "slack", ws_id, target,
        role="admin",
        deepnote_uid=(target_link or {}).get("deepnoteUid"),
        added_by=user,
    )
    _slack_post(team_id, channel,
        f"✅ <@{target}> を admin として追加しました。\n"
        "admin は AI 質問などクレジットを消費する操作も実行できます。", thread_ts)
    bot_audit.record(provider="slack", source_type="channel",
                     source_user_id=user, team_id=team_id,
                     account_id=glink.get("ownerAccountId"),
                     command="group_member_add", outcome="ok")


def _handle_slack_group_member_remove(team_id: str, channel: str, user: str, text: str, thread_ts: Optional[str]) -> None:
    ws_id = _slack_workspace_id(team_id, channel)
    glink = group_acl.get_group_link("slack", ws_id)
    if not glink:
        _slack_post(team_id, channel,
            "先に「DeepNote 接続」でチャンネルを接続してください。", thread_ts)
        return
    me = group_acl.get_member("slack", ws_id, user)
    if not me or me.get("role") != "owner":
        _slack_post(team_id, channel,
            "メンバー削除は owner ロールのみ実行できます。", thread_ts)
        return
    target = _extract_slack_user_mention(text)
    if not target:
        _slack_post(team_id, channel, "対象ユーザーを @メンション で指定してください。", thread_ts)
        return
    if target == user:
        _slack_post(team_id, channel,
            "自分自身を owner から外すことはできません。先に「DeepNote 切断」をご検討ください。", thread_ts)
        return
    if group_acl.remove_member("slack", ws_id, target):
        _slack_post(team_id, channel, f"✅ <@{target}> のロールを解除しました。", thread_ts)
    else:
        _slack_post(team_id, channel, "対象メンバーは登録されていません。", thread_ts)
    bot_audit.record(provider="slack", source_type="channel",
                     source_user_id=user, team_id=team_id,
                     account_id=glink.get("ownerAccountId"),
                     command="group_member_remove", outcome="ok")


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
        # Phase 7: channel / group can request *shared* sessions, but never
        # private data. Public channels are noisy, so we only respond to
        # explicit @mentions, not every message.
        if event.get("type") != "app_mention":
            return
        cmd = _classify_command(_strip_app_mentions(text))
        # Greeting / unknown handling — earlier versions ran the
        # shared-session lookup unconditionally so even @DeepNote
        # こんにちは got the "no shared data" reply. Greet politely
        # instead and explain what's available.
        if cmd in ("greeting",) or cmd == "unknown":
            slack_client.post_message(team_id=team_id, channel=channel, thread_ts=thread_ts, text=(
                "こんにちは。DeepNote Clow です。\n"
                "このチャンネルでは、共有された会議の要約や TODO を確認できます。\n\n"
                "▼ 使い方\n"
                "・「最新」: 共有された最新会議の要約\n"
                "・「決定事項」: 最新会議の決定事項\n"
                "・「資料」「PDF」「DOCX」「PPTX」: 資料リンク\n"
                "・「クレジット」「TODO」: 個人情報のため Slack DM で\n"
                "・「ヘルプ」: この案内"
            ))
            bot_audit.record(
                provider="slack", source_type=channel_type or "unknown",
                source_user_id=user, team_id=team_id, command=cmd, outcome="greeting",
            )
            return
        if cmd in ("credit", "todos"):
            slack_client.post_message(team_id=team_id, channel=channel,
                                      text=M.GROUP_PRIVATE_REJECTED, thread_ts=thread_ts)
            bot_audit.record(
                provider="slack", source_type=channel_type or "unknown",
                source_user_id=user, team_id=team_id, command=cmd,
                outcome="blocked_private_in_group",
            )
            return

        # Phase 1 admin commands (connect / disconnect / status / member ops).
        # Connect / status / disconnect / member-* bypass the ACL gate.
        if cmd == "group_connect":
            _handle_slack_group_connect(team_id, channel, user, thread_ts)
            return
        if cmd == "group_status":
            _handle_slack_group_status(team_id, channel, user, thread_ts)
            return
        if cmd == "group_disconnect":
            _handle_slack_group_disconnect(team_id, channel, user, thread_ts)
            return
        if cmd == "group_member_add":
            _handle_slack_group_member_add(team_id, channel, user, text, thread_ts)
            return
        if cmd == "group_member_remove":
            _handle_slack_group_member_remove(team_id, channel, user, text, thread_ts)
            return

        link = slack_link_tokens.get_link(team_id, user)
        ws_id = _slack_workspace_id(team_id, channel)
        ctx = group_acl.resolve_group_execution_context(
            provider="slack", workspace_id=ws_id,
            source_user_id=user, intent=cmd,
        )
        if isinstance(ctx, group_acl.RequireGroupConnect):
            slack_client.post_message(team_id=team_id, channel=channel,
                                      text=ctx.connect_hint, thread_ts=thread_ts)
            bot_audit.record(
                provider="slack", source_type=channel_type or "unknown",
                source_user_id=user, team_id=team_id,
                account_id=(link or {}).get("accountId"),
                command=cmd, outcome=ctx.audit_outcome,
            )
            return
        if isinstance(ctx, group_acl.Denied):
            slack_client.post_message(team_id=team_id, channel=channel,
                                      text=ctx.reason, thread_ts=thread_ts)
            bot_audit.record(
                provider="slack", source_type=channel_type or "unknown",
                source_user_id=user, team_id=team_id,
                account_id=(link or {}).get("accountId"),
                command=cmd, outcome=ctx.audit_outcome,
            )
            return
        data_account_id = ctx.data_owner_account_id
        data_uid = ctx.data_owner_deepnote_uid
        ws_key = f"slack:{team_id}"
        # 「自動共有」 (Lv4) is retired for safety.
        if cmd == "auto_share_deprecated":
            slack_client.post_message(team_id=team_id, channel=channel,
                                      text=M.AUTO_SHARE_DEPRECATED, thread_ts=thread_ts)
            bot_audit.record(
                provider="slack", source_type=channel_type or "unknown",
                source_user_id=user, team_id=team_id,
                account_id=data_account_id, deepnote_uid=data_uid,
                command=cmd, outcome="auto_share_deprecated",
            )
            return
        # Smart Share notify/digest must be configured in DM.
        if cmd in ("notify_on", "notify_off", "notify_status",
                   "digest_on", "digest_off", "digest_status"):
            slack_client.post_message(team_id=team_id, channel=channel,
                                      text="Smart Share の通知設定は DeepNote bot との DM で行ってください。\n\n" + M.SMART_SHARE_HELP,
                                      thread_ts=thread_ts)
            bot_audit.record(
                provider="slack", source_type=channel_type or "unknown",
                source_user_id=user, team_id=team_id,
                account_id=data_account_id, deepnote_uid=data_uid,
                command=cmd, outcome="redirect_to_dm",
            )
            return
        # Paid action: route to assistant_hub charging billing_owner.
        if cmd == "assistant_qna":
            q = (text or "").strip()
            for prefix in ("質問:", "質問：", "質問", "ask "):
                if q.startswith(prefix):
                    q = q[len(prefix):].strip()
                    break
            # bot mention will leave a leading "<@Uxxx> " — strip that too
            q = _strip_app_mentions(q)
            if not q:
                slack_client.post_message(team_id=team_id, channel=channel,
                    text="質問を入力してください。例: 「決定事項は？」「TODO は？」",
                    thread_ts=thread_ts)
                return
            try:
                import asyncio
                from app.services import assistant_hub
                result = asyncio.run(assistant_hub.handle_message(
                    account_id=ctx.billing_owner_account_id,
                    owner_uid=ctx.billing_owner_deepnote_uid,
                    question=q, session_id=None, mode="session",
                    channel="slack", idempotency_key=None,
                ))
                answer = result.get("answer") or "回答を生成できませんでした。"
            except Exception as _e:
                logger.warning("[slack.qna.group] hub call failed: %s", _e)
                answer = "Assistant へのリクエストに失敗しました。少し時間をおいて再度お試しください。"
            slack_client.post_message(team_id=team_id, channel=channel,
                                      text=answer, thread_ts=thread_ts)
            bot_audit.record(
                provider="slack", source_type=channel_type or "unknown",
                source_user_id=user, team_id=team_id,
                account_id=ctx.billing_owner_account_id,
                deepnote_uid=ctx.billing_owner_deepnote_uid,
                command=cmd, outcome="ok_paid",
            )
            return

        def _no_data_text() -> str:
            try:
                latest = group_shared_briefing.get_latest_any_session(data_account_id)
                if latest and latest.get("title"):
                    return M.GROUP_NO_SHARED_DATA_WITH_HINT.format(title=latest["title"][:40])
            except Exception:
                pass
            return M.GROUP_NO_SHARED_DATA

        def _send_proactive_share_offer() -> bool:
            try:
                latest = group_shared_briefing.get_latest_any_session(data_account_id)
                if not latest or not latest.get("id"):
                    return False
                blocks = slack_client.build_share_confirm_blocks(
                    session_id=latest["id"],
                    title=(latest.get("title") or "(無題)"),
                    summary_blurb="",
                    decision_count=0,
                    todo_count=0,
                    target_channel=channel,
                    attach_pdf=False,
                )
                ok = slack_client.post_blocks(
                    team_id=team_id, channel=channel,
                    blocks=blocks,
                    fallback_text=f"「{latest.get('title') or '(無題)'}」をこのチャンネルに共有しますか？",
                )
                return bool(ok)
            except Exception as _e:
                logger.warning("[slack.proactive] offer failed: %s", _e)
                return False

        sent_blocks = False
        if cmd == "decisions":
            decisions = group_shared_briefing.get_recent_shared_decisions(
                data_account_id, ws_key, limit=3
            )
            if decisions:
                reply_text = _format_decisions(decisions)
            else:
                sent_blocks = _send_proactive_share_offer()
                reply_text = "" if sent_blocks else _no_data_text()
        elif cmd in ("latest", "help"):
            shared = group_shared_briefing.get_latest_shared_session(data_account_id, ws_key)
            if shared:
                reply_text = _format_latest(shared)
            else:
                sent_blocks = _send_proactive_share_offer()
                reply_text = "" if sent_blocks else _no_data_text()
        else:
            reply_text = _no_data_text()
        if not sent_blocks:
            slack_client.post_message(team_id=team_id, channel=channel,
                                      text=reply_text, thread_ts=thread_ts)
        bot_audit.record(
            provider="slack", source_type=channel_type or "unknown",
            source_user_id=user, team_id=team_id,
            account_id=data_account_id, deepnote_uid=data_uid,
            command=cmd,
            outcome="ok_shared_only" if "共有" not in reply_text else "no_shared_data",
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

    # Phase B: ``pdf`` in Slack DM uploads the PDF directly into the
    # conversation via files.uploadV2. We fall back to the URL-only
    # text reply if upload fails (no token, files:write missing, etc.).
    if command == "pdf":
        try:
            bundle = asset_delivery.get_latest_export_links(link["accountId"])
        except Exception:
            bundle = None
        if bundle and (bundle.get("links") or {}).get("pdf"):
            pdf_url = bundle["links"]["pdf"]
            title = bundle.get("title") or "DeepNote 議事録"
            try:
                import requests as _rq
                rr = _rq.get(pdf_url, timeout=30)
                if rr.status_code == 200 and rr.content:
                    ok = slack_client.upload_file(
                        team_id=team_id, channel=channel,
                        file_bytes=rr.content,
                        filename=f"{title[:60]}.pdf",
                        title=title,
                        initial_comment=f"📄 最新会議「{title}」の PDF を添付します",
                    )
                    if ok:
                        bot_audit.record(
                            provider="slack", source_type="im",
                            source_user_id=user, team_id=team_id,
                            account_id=link.get("accountId"),
                            deepnote_uid=link.get("deepnoteUid"),
                            command=command, outcome="ok_pdf_attached",
                        )
                        return
            except Exception as _e:
                logger.warning("[slack.pdf] direct attach failed, falling back to URL: %s", _e)
        # fall-through → URL reply

    reply = _build_reply_for_linked(link["accountId"], command, slack_user_id=user, raw_text=text or "")
    slack_client.post_message(team_id=team_id, channel=channel, text=reply)
    bot_audit.record(
        provider="slack", source_type="im",
        source_user_id=user, team_id=team_id,
        account_id=link.get("accountId"),
        deepnote_uid=link.get("deepnoteUid"),
        command=command, outcome="ok",
    )


# ──────────────────────────────────────────────────────────────────────
# Block Kit interactions — Smart Share Lv3 confirm/cancel buttons
# ──────────────────────────────────────────────────────────────────────

@router.post("/interactions", include_in_schema=False)
async def slack_interactions(
    request: Request,
    x_slack_request_timestamp: Optional[str] = Header(None, alias="X-Slack-Request-Timestamp"),
    x_slack_signature: Optional[str] = Header(None, alias="X-Slack-Signature"),
):
    """Slack POSTs an ``application/x-www-form-urlencoded`` body with a
    single ``payload=<JSON>`` field whenever a user clicks a button or
    submits a modal. We verify the same X-Slack-Signature, decode the
    payload, and dispatch on the action_id.
    """
    body = await request.body()
    if not slack_client.is_configured():
        raise HTTPException(status_code=503, detail="slack_not_configured")
    if not slack_client.verify_signature(
        body=body,
        timestamp=x_slack_request_timestamp or "",
        signature=x_slack_signature or "",
    ):
        raise HTTPException(status_code=401, detail="invalid_signature")

    from urllib.parse import parse_qs
    try:
        form = parse_qs(body.decode("utf-8"))
        raw_payload = (form.get("payload") or [""])[0]
        data = json.loads(raw_payload) if raw_payload else {}
    except Exception:
        raise HTTPException(status_code=400, detail="malformed_payload")

    actions = data.get("actions") or []
    if not actions:
        return JSONResponse({"ok": True})
    action = actions[0]
    action_id = action.get("action_id") or ""
    raw_value = action.get("value") or "{}"
    try:
        value = json.loads(raw_value)
    except Exception:
        value = {}

    user = (data.get("user") or {}).get("id") or ""
    team_id = (data.get("team") or {}).get("id") or ""
    response_url = data.get("response_url") or ""

    if action_id == "deepnote_share_confirm" and value.get("action") == "share_confirm":
        # Resolve the link → account, then post the share into the
        # destination channel. We re-derive ``sd`` here to keep the
        # ownership check identical to the REST share:confirm route.
        link = slack_link_tokens.get_link(team_id, user)
        if not link:
            return JSONResponse({"text": "DeepNote と Slack の連携が必要です。", "response_type": "ephemeral"})
        from app.firebase import db as _db
        sid = value.get("sessionId") or ""
        snap = _db.collection("sessions").document(sid).get() if sid else None
        sd = snap.to_dict() if snap and snap.exists else {}
        if not sd or sd.get("ownerAccountId") != link.get("accountId"):
            return JSONResponse({"text": "対象会議が見つからない、または共有権限がありません。",
                                 "response_type": "ephemeral"})
        channel = value.get("channel") or ""
        title = sd.get("title") or "(無題)"
        lines = [f"📝 {title}"]
        topic = sd.get("topicSummary") or ""
        if topic:
            lines.append(topic[:300])
        for d in ((sd.get("summaryJson") or {}).get("decisions") or [])[:5]:
            txt = d.get("text") if isinstance(d, dict) else str(d)
            if txt:
                lines.append(f"・{txt}")
        text = "\n".join(lines)
        slack_client.post_message(team_id=team_id, channel=channel, text=text)

        # Append workspace key so future group-bot 「最新」 surfaces it.
        try:
            existing = list(sd.get("sharedToWorkspaceTeams") or [])
            ws_key = f"slack:{team_id}"
            if ws_key not in existing:
                _db.collection("sessions").document(sid).update(
                    {"sharedToWorkspaceTeams": existing + [ws_key]}
                )
        except Exception:
            pass

        bot_audit.record(
            provider="slack", source_type="interaction",
            source_user_id=user, team_id=team_id,
            account_id=link.get("accountId"), deepnote_uid=link.get("deepnoteUid"),
            command="share_confirm", outcome="ok",
        )
        # Replace the original card with a confirmation receipt.
        return JSONResponse({
            "replace_original": True,
            "text": f"✅ 会議「{title}」を <#{channel}> に共有しました。",
        })

    if action_id == "deepnote_share_cancel" and value.get("action") == "share_cancel":
        return JSONResponse({
            "replace_original": True,
            "text": "キャンセルしました。",
        })

    return JSONResponse({"ok": True})


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
    body = (
        f"<p>{M.CONNECT_LOGIN_PROMPT}</p>"
        f"<p><a href=\"/integrations/slack/login?token={token}\">DeepNote にログインして連携を完了する</a></p>"
    )
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
