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
        "このグループに共有された会議はまだありません。\n"
        "（プライバシー保護のため、共有マークが付いた会議だけがこちらに表示されます）\n\n"
        "▼ 共有したい会議がある場合\n"
        "DeepNoteアプリ → 会議 → 共有 → 「このグループに共有」\n\n"
        "▼ 要約完了の通知だけ受け取りたい場合\n"
        "個人チャットで「通知 ON」と送ってください（DM のみへ通知。グループには自動投稿しません）。"
    )

    GROUP_NO_SHARED_DATA_WITH_HINT = (
        "このグループに共有された会議はまだありません。\n"
        "（最新の会議「{title}」は未共有です）\n\n"
        "▼ この会議を共有するには\n"
        "DeepNoteアプリ → 会議「{title}」→ 共有 → 「このグループに共有」\n\n"
        "▼ 要約完了の通知だけ受け取りたい場合\n"
        "個人チャットで「通知 ON」と送ってください（自動でグループには投稿しません）。"
    )

    AUTO_SHARE_DEPRECATED = (
        "🛑 「自動共有」(グループ自動投稿) は安全のため廃止しました。\n"
        "AI が生成した要約をユーザー確認なしにグループ投稿すると、誤認識・社外秘・個人情報を意図せず共有してしまう恐れがあるためです。\n\n"
        "代わりに次の安全な選択肢をご利用ください：\n"
        "・「通知 ON」(DMのみ): 要約完了をあなたの個人チャットだけに通知\n"
        "・「自分要約 ON」(DMのみ): 要約 + TODO の概要をあなたの DM に送信\n"
        "・チームへ共有したい場合は、DeepNoteアプリで明示的にボタンを押して共有してください"
    )

    NOTIFY_ENABLED = (
        "✅ 要約完了通知を有効にしました。\n"
        "今後 DeepNote で記録した会議の要約が完了したら、この個人チャットに通知します。\n"
        "（グループには自動投稿しません。停止するには「通知 OFF」）"
    )
    NOTIFY_DISABLED = "🛑 要約完了通知を停止しました。"
    NOTIFY_ALREADY_ON = "要約完了通知は既に有効です。"
    NOTIFY_ALREADY_OFF = "要約完了通知は既にオフです。"

    DIGEST_ENABLED = (
        "✅ 自分要約を有効にしました。\n"
        "今後の会議の要約 + TODO 概要を、あなたの個人チャットに送信します。\n"
        "（グループには自動投稿しません。停止するには「自分要約 OFF」）"
    )
    DIGEST_DISABLED = "🛑 自分要約の自動送信を停止しました。"
    DIGEST_ALREADY_ON = "自分要約は既に有効です。"
    DIGEST_ALREADY_OFF = "自分要約は既にオフです。"

    SMART_SHARE_HELP = (
        "▼ Smart Share コマンド (個人チャットで送信)\n"
        "・「通知 ON」: 要約完了を DM に通知 (内容は短く)\n"
        "・「自分要約 ON」: 要約 + TODO 概要を DM に送信\n"
        "・「通知 OFF」「自分要約 OFF」: 各停止\n"
        "▼ チーム共有はアプリから\n"
        "DeepNoteアプリ → 会議 → 共有 → 「このグループに共有」を押してください。\n"
        "プライバシー保護のため、グループへの自動投稿は行いません。"
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
    # Greetings — short conversational openers that previously fell
    # through to the "no shared data" branch in groups.
    if t in {
        "こんにちは", "こんばんは", "おはよう", "おはよ", "やあ", "どうも",
        "hello", "hi", "hey",
    }:
        return "greeting"
    # Q&A passthrough — anything that ends with "?" / "？" or starts with
    # 「質問」 is forwarded to the Assistant Hub so the user gets the
    # same grounded answers in chat as on iOS / Desktop.
    if t.endswith("?") or t.endswith("？") or t.startswith("質問") or t.startswith("ask "):
        return "assistant_qna"
    if any(k in t for k in ("ヘルプ", "help", "使い方", "?", "？")):
        return "help"
    # Auto-share toggle (group-only command). We accept several
    # phrasings so users don't have to remember the exact wording.
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
    if any(k in t for k in ("pdf", "ピーディーエフ")):
        return "pdf"
    if "docx" in t or "ワード" in t or "word" in t:
        return "docx"
    if "pptx" in t or "パワポ" in t or "ppt" in t or "powerpoint" in t:
        return "pptx"
    if any(k in t for k in ("資料", "asset", "asset")):
        return "assets"
    return "unknown"


def _build_reply_for_linked(account_id: str, command: str, *, line_user_id: str = "", raw_text: str = "") -> str:
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
        # Strip leading 「質問」 / "ask" so the hub sees the actual question.
        q = raw_text.strip()
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
                account_id=account_id, owner_uid=line_user_id, question=q,
                session_id=None, mode="session", channel="line",
                idempotency_key=None,
            ))
            return result.get("answer") or "回答を生成できませんでした。"
        except Exception as _e:
            logger.warning("[line.qna] hub call failed: %s", _e)
            return "Assistant へのリクエストに失敗しました。少し時間をおいて再度お試しください。"
    if command in ("notify_on", "notify_off", "notify_status"):
        from app.services import bot_smart_share
        if command == "notify_on":
            changed = bot_smart_share.set_notify("line", line_user_id, True)
            return M.NOTIFY_ENABLED if changed else M.NOTIFY_ALREADY_ON
        if command == "notify_off":
            changed = bot_smart_share.set_notify("line", line_user_id, False)
            return M.NOTIFY_DISABLED if changed else M.NOTIFY_ALREADY_OFF
        s = bot_smart_share.get_settings("line", line_user_id)
        return ("✅ 通知: 有効" if s["notifyOnSummaryReady"] else "⏸ 通知: オフ") + "\n\n" + M.SMART_SHARE_HELP
    if command in ("digest_on", "digest_off", "digest_status"):
        from app.services import bot_smart_share
        if command == "digest_on":
            changed = bot_smart_share.set_dm_digest("line", line_user_id, True)
            return M.DIGEST_ENABLED if changed else M.DIGEST_ALREADY_ON
        if command == "digest_off":
            changed = bot_smart_share.set_dm_digest("line", line_user_id, False)
            return M.DIGEST_DISABLED if changed else M.DIGEST_ALREADY_OFF
        s = bot_smart_share.get_settings("line", line_user_id)
        return ("✅ 自分要約: 有効" if s["dmDigestOnSummary"] else "⏸ 自分要約: オフ") + "\n\n" + M.SMART_SHARE_HELP
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
        # ──────────────────────────────────────────────────────────────
        # Group / room: only respond when the user explicitly
        # addresses DeepNote, OR sends one of the recognised commands.
        # Earlier versions ran the shared-session lookup unconditionally
        # so a casual 「こんにちは」 also got "このグループに共有された
        # 会議はまだありません" — that's spammy in a multi-member group.
        # ──────────────────────────────────────────────────────────────
        text_lower = (user_text or "").strip().lower()
        bot_addressed = (
            "deepnote" in text_lower
            or "clow" in text_lower
            or "ディープノート" in (user_text or "")
            or "クロウ" in (user_text or "")
        )
        cmd = _classify_command(user_text)
        # Recognised commands that warrant a reply even without an
        # explicit @DeepNote mention.
        actionable = {
            "greeting", "help", "latest", "todos", "decisions",
            "pdf", "docx", "pptx", "assets",
            "auto_share_deprecated",
            "notify_on", "notify_off", "notify_status",
            "digest_on", "digest_off", "digest_status",
            "credit", "assistant_qna",
        }
        if cmd == "unknown" and not bot_addressed:
            # Stay silent on unrelated chatter so the bot doesn't
            # interrupt regular group conversation.
            bot_audit.record(
                provider="line", source_type=source_type,
                source_user_id=line_user_id,
                account_id=(line_link_tokens.get_link(line_user_id) or {}).get("accountId"),
                command="ignored_chatter", outcome="silent",
            )
            return
        # If the user said hi to the bot (mention without command, or
        # exact 「こんにちは」), greet them and explain what's available.
        if cmd == "greeting" or (bot_addressed and cmd in ("unknown", "help")):
            line_messaging.reply(reply_token, [line_messaging.text_message(
                "こんにちは。DeepNote Clow です。\n"
                "このグループでは、共有された会議の要約や TODO を確認できます。\n\n"
                "▼ 使い方\n"
                "・「最新」: 共有された最新会議の要約\n"
                "・「決定事項」: 最新会議の決定事項\n"
                "・「資料」「PDF」「DOCX」「PPTX」: 資料リンク\n"
                "・「クレジット」「TODO」: 個人情報のため LINE 個人チャットで\n"
                "・「ヘルプ」: この案内"
            )])
            bot_audit.record(
                provider="line", source_type=source_type,
                source_user_id=line_user_id,
                account_id=(line_link_tokens.get_link(line_user_id) or {}).get("accountId"),
                command="greeting", outcome="ok",
            )
            return
        link = line_link_tokens.get_link(line_user_id)
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
        # 「自動共有」 (Lv4) is retired for safety. Show the migration notice.
        if cmd == "auto_share_deprecated":
            line_messaging.reply(reply_token, [line_messaging.text_message(M.AUTO_SHARE_DEPRECATED)])
            bot_audit.record(
                provider="line", source_type=source_type,
                source_user_id=line_user_id,
                account_id=link["accountId"], deepnote_uid=link.get("deepnoteUid"),
                command=cmd, outcome="auto_share_deprecated",
            )
            return
        # Smart Share notify/digest are DM-only. In a group, redirect.
        if cmd in ("notify_on", "notify_off", "notify_status",
                   "digest_on", "digest_off", "digest_status"):
            line_messaging.reply(reply_token, [line_messaging.text_message(
                "Smart Share の通知設定は LINE の個人チャットで設定してください。\n\n" + M.SMART_SHARE_HELP)])
            bot_audit.record(
                provider="line", source_type=source_type,
                source_user_id=line_user_id,
                account_id=link["accountId"], deepnote_uid=link.get("deepnoteUid"),
                command=cmd, outcome="redirect_to_dm",
            )
            return

        # Phase D+: when a group bot has nothing to show, proactively
        # offer Smart Share Lv3 in-group. The user types 「最新」 and we
        # respond with a confirm template ("Want to share your latest
        # meeting?"), so they can finish the share without leaving LINE.
        def _send_proactive_share_offer_or_text() -> bool:
            try:
                latest = group_shared_briefing.get_latest_any_session(link["accountId"])
                if not latest or not latest.get("id"):
                    return False
                from urllib.parse import urlencode as _qs
                yes_data = "action=share_confirm&" + _qs({"sid": latest["id"], "dest": group_id, "attach": "0"})
                no_data = "action=share_cancel&sid=" + latest["id"]
                title_short = (latest.get("title") or "(無題)")[:40]
                msg = line_messaging.confirm_template_message(
                    alt_text=f"会議「{title_short}」をこのグループに共有しますか？",
                    prompt=f"会議「{title_short}」をこのグループに共有しますか？",
                    yes_label="✅ 共有する", yes_data=yes_data,
                    no_label="キャンセル",   no_data=no_data,
                )
                line_messaging.reply(reply_token, [msg])
                return True
            except Exception as _e:
                logger.warning("[line.proactive] offer failed: %s", _e)
                return False

        def _no_data_text() -> str:
            try:
                latest = group_shared_briefing.get_latest_any_session(link["accountId"])
                if latest and latest.get("title"):
                    return M.GROUP_NO_SHARED_DATA_WITH_HINT.format(title=latest["title"][:40])
            except Exception:
                pass
            return M.GROUP_NO_SHARED_DATA

        if cmd == "decisions":
            decisions = group_shared_briefing.get_recent_shared_decisions(
                link["accountId"], ws_key, limit=3
            )
            if decisions:
                text = _format_decisions(decisions)
                line_messaging.reply(reply_token, [line_messaging.text_message(text)])
            else:
                if not _send_proactive_share_offer_or_text():
                    line_messaging.reply(reply_token, [line_messaging.text_message(_no_data_text())])
                text = M.GROUP_NO_SHARED_DATA
        elif cmd == "latest" or cmd == "help":
            shared = group_shared_briefing.get_latest_shared_session(link["accountId"], ws_key)
            if shared:
                text = _format_latest(shared)
                line_messaging.reply(reply_token, [line_messaging.text_message(text)])
            else:
                if not _send_proactive_share_offer_or_text():
                    line_messaging.reply(reply_token, [line_messaging.text_message(_no_data_text())])
                text = M.GROUP_NO_SHARED_DATA
        else:
            text = _no_data_text()
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
    text = _build_reply_for_linked(link["accountId"], command, line_user_id=line_user_id, raw_text=user_text or "")
    line_messaging.reply(reply_token, [line_messaging.text_message(text)])
    bot_audit.record(
        provider="line", source_type="user",
        source_user_id=line_user_id,
        account_id=link.get("accountId"),
        deepnote_uid=link.get("deepnoteUid"),
        command=command, outcome="ok",
    )


def _handle_postback_event(event: Dict[str, Any]) -> None:
    """Handle Smart Share Lv3 confirmation postbacks.

    Postback ``data`` is a query-string of the form
        action=share_confirm&sid=<sessionId>&dest=<groupOrUserId>&attach=0|1
    The button itself is rendered via a Flex / Template message that
    integrations_line emits when iOS / Desktop calls
    ``POST /v1/assistant/share:preview`` with channel="line".
    """
    source = event.get("source") or {}
    line_user_id = source.get("userId")
    reply_token = event.get("replyToken")
    raw = (event.get("postback") or {}).get("data") or ""
    if not raw or not reply_token:
        return
    from urllib.parse import parse_qs
    parsed = parse_qs(raw)
    action = (parsed.get("action") or [""])[0]

    if action == "share_cancel":
        line_messaging.reply(reply_token, [line_messaging.text_message("キャンセルしました。")])
        return

    if action != "share_confirm":
        return

    sid = (parsed.get("sid") or [""])[0]
    dest = (parsed.get("dest") or [""])[0]
    if not sid or not dest:
        line_messaging.reply(reply_token, [line_messaging.text_message("共有リクエストが不正です。")])
        return

    # Resolve link → account, ownership-check, build text, push to dest.
    if not line_user_id:
        return
    link = line_link_tokens.get_link(line_user_id)
    if not link:
        line_messaging.reply(reply_token, [line_messaging.text_message(
            "DeepNote と LINE の連携が必要です。個人チャットでセットアップしてください。")])
        return
    try:
        snap = db.collection("sessions").document(sid).get()
        sd = snap.to_dict() if snap.exists else {}
    except Exception:
        sd = {}
    if not sd or sd.get("ownerAccountId") != link.get("accountId"):
        line_messaging.reply(reply_token, [line_messaging.text_message(
            "対象会議が見つからない、または共有権限がありません。")])
        return

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

    if line_messaging.is_configured():
        line_messaging.push(dest, [line_messaging.text_message(text)])

    # Append workspace key so future group-bot 「最新」 surfaces it.
    try:
        existing = list(sd.get("sharedToWorkspaceTeams") or [])
        ws_key = f"line:{dest}"
        if ws_key not in existing:
            db.collection("sessions").document(sid).update(
                {"sharedToWorkspaceTeams": existing + [ws_key]}
            )
    except Exception:
        pass

    bot_audit.record(
        provider="line", source_type="postback",
        source_user_id=line_user_id,
        account_id=link.get("accountId"), deepnote_uid=link.get("deepnoteUid"),
        command="share_confirm", outcome="ok",
    )
    line_messaging.reply(reply_token, [line_messaging.text_message(
        f"✅ 「{title}」を共有しました。")])


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
            elif ev_type == "postback":
                _handle_postback_event(ev)
            else:
                # unfollow / leave / etc. — log + ignore.
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
