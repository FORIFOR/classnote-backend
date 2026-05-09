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
from app.services import group_acl

logger = logging.getLogger("app.routes.integrations.line")

router = APIRouter(prefix="/integrations/line", tags=["Integrations:LINE"])


def _run_coroutine(coro):
    """Run an async coroutine from inside this module's sync handlers.

    The LINE webhook handler is ``async def`` (FastAPI async path), but
    delegates to the sync ``_handle_message_event`` for per-event work.
    Calling ``asyncio.run(coro)`` directly from there raises
    ``RuntimeError: asyncio.run() cannot be called from a running event
    loop`` because the FastAPI loop is already active in this thread.

    Workaround: spin up a one-shot worker thread and execute
    ``asyncio.run`` there — the new thread has no running loop, so
    ``asyncio.run`` works as designed. Cost is one thread per call,
    acceptable for chat-bot QPS.
    """
    import asyncio
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


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
        "クイックコマンド:\n"
        "・「最新」: 最新の会議の要約\n"
        "・「TODO」: 直近のTODO（最大3件）\n"
        "・「決定事項」: 最新会議の決定事項\n"
        "・「資料」: 最新会議の PDF / DOCX / PPTX リンク\n"
        "・「PDF」「DOCX」「PPTX」: 個別フォーマット\n"
        "・「クレジット」: あなたのDeepNoteアカウントのクレジット残量\n"
        "・「ログアウト」: DeepNote 連携を解除（別アカウントで再連携する前に）\n"
        "・「ヘルプ」: この案内\n\n"
        "自然な質問・依頼にも答えます（DeepNote / iOS / Desktop と同じ AI Assist が応答）:\n"
        "・「先週のA社の決定事項は?」\n"
        "・「最新の会議の資料を送って」\n"
        "・「今週の TODO のうち期限が近いものは?」\n"
        "・「営業会議の参加者を教えて」"
    )

    USER_UNLINKED_OK = (
        "✅ DeepNote の連携を解除しました。\n"
        "別のアカウントで連携し直すには、もう一度何かメッセージを送ってください。"
    )

    USER_UNLINK_NOT_LINKED = (
        "現在 DeepNote と連携していません。\n"
        "連携するには何かメッセージを送ってください。"
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
    # 1:1 unlink (logout) — exact-match keywords so casual mentions in
    # group chatter don't accidentally remove links. has_bot is not
    # required here because the handler is gated to source_type=="user"
    # in _handle_message_event.
    if t in {"ログアウト", "logout", "サインアウト", "signout", "sign out"}:
        return "user_unlink"
    # Phase 1 group ACL commands. Require an explicit DeepNote/Clow
    # mention so that bare 「接続」「切断」 in group chatter doesn't
    # accidentally trigger admin operations.
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
    # Quick-info commands — exact match only. A bare "最新" is a quick-
    # reply command, but "最新の会議の資料を送って" is a natural-language
    # request that must fall through to assistant_qna so the LLM can
    # interpret intent (e.g., decide to surface asset links). Substring
    # match was previously hijacking every sentence containing 最新 /
    # 要約 etc., short-circuiting the AI Assist path.
    _EXACT_QUICK = {
        "credit":    {"クレジット", "残量", "credit", "credits"},
        "latest":    {"最新", "最新会議", "summary", "要約", "サマリ", "サマリー"},
        "todos":     {"todo", "todos", "タスク", "やること", "タスク一覧"},
        "decisions": {"決定", "決定事項", "decision", "decisions"},
        "pdf":       {"pdf", "ピーディーエフ"},
        "docx":      {"docx", "ワード", "word"},
        "pptx":      {"pptx", "パワポ", "ppt", "powerpoint"},
        "assets":    {"資料", "asset", "assets", "資料一覧", "ファイル"},
    }
    for _cmd, _words in _EXACT_QUICK.items():
        if t in _words:
            return _cmd
    # Anything else — natural-language request → forward to AI Assist
    # so the LLM can interpret intent. This is the same engine that
    # powers iOS / Desktop AI Assist; it has access to recent meetings,
    # decisions and TODOs and can pick the right artefact to surface.
    return "assistant_qna"


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
            from app.services import assistant_hub
            result = _run_coroutine(assistant_hub.handle_message(
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


# ──────────────────────────────────────────────────────────────────────
# Phase 1 group ACL command handlers (LINE)
# ──────────────────────────────────────────────────────────────────────

def _build_session_picker_bubble(candidates: List[Dict[str, Any]],
                                 *, group_id: str) -> Dict[str, Any]:
    """Phase 1.5: Flex bubble listing 3–5 recent meetings the data_owner
    can share into this group. Each row's button posts back
    ``share_confirm`` with the chosen ``sid`` so the picker tap is the
    explicit share consent.

    The ``group_id`` is encoded in each button's postback ``dest`` so the
    existing ``share_confirm`` handler (which re-validates session
    ownership server-side) does not need to consult external state.
    """
    from urllib.parse import urlencode as _qs

    rows: List[Dict[str, Any]] = []
    for s in candidates[:5]:
        sid = str(s.get("id") or "")
        if not sid:
            continue
        title = (s.get("title") or "(無題)")[:40]
        created = s.get("createdAt")
        try:
            date_str = created.strftime("%Y-%m-%d %H:%M") if created else ""
        except Exception:
            date_str = ""
        post_data = "action=share_confirm&" + _qs({
            "sid": sid, "dest": group_id, "attach": "0",
        })
        rows.append({
            "type": "box", "layout": "vertical", "spacing": "xs",
            "paddingAll": "sm",
            "borderColor": "#E0E0E0", "borderWidth": "1px",
            "cornerRadius": "md",
            "contents": [
                {"type": "text", "text": f"📝 {title}",
                 "weight": "bold", "size": "sm", "wrap": True},
                {"type": "text", "text": date_str or " ",
                 "size": "xxs", "color": "#888888"},
                {"type": "button", "style": "primary", "color": "#1A73E8",
                 "height": "sm",
                 "action": {
                     "type": "postback",
                     "label": "この会議を共有",
                     "data": post_data[:300],
                     "displayText": f"「{title}」を共有",
                 }},
            ],
        })
    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "text", "text": "共有する会議を選択",
                 "weight": "bold", "size": "lg", "color": "#1A73E8"},
                {"type": "text", "size": "xs", "color": "#888888", "wrap": True,
                 "text": "選択した会議だけがグループに共有されます。要約・決定事項・資料リンクが他のメンバーから参照可能になります。"},
            ],
            "paddingAll": "md", "spacing": "xs",
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": rows or [
                {"type": "text", "size": "sm",
                 "text": "共有可能な会議が見つかりません。"}
            ],
            "paddingAll": "lg",
        },
    }


def _build_group_connect_confirm_card(*, account_id_short: str, max_runs: int,
                                      max_paid: int, line_user_id: str) -> Dict[str, Any]:
    """Phase 1.5: Flex bubble that *requires* the speaker to tap ✅
    before the group binding is created. The card spells out the
    consequences (session access + credit consumption + owner role)
    so the speaker is making an informed decision."""
    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "DeepNote 接続確認",
                 "weight": "bold", "size": "lg", "color": "#1A73E8"},
            ],
            "paddingAll": "md",
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {"type": "text", "wrap": True, "size": "sm",
                 "text": (
                     f"このグループを DeepNote アカウント "
                     f"{account_id_short}… に接続します。"
                 )},
                {"type": "separator"},
                {"type": "text", "weight": "bold", "size": "sm",
                 "text": "⚠️ 接続後の影響"},
                {"type": "text", "wrap": True, "size": "xs", "color": "#555555",
                 "text": (
                     "・このアカウントの 議事録（共有許可されたもの）が"
                     "グループから参照されます\n"
                     "・グループの AI 質問は このアカウントのクレジットを"
                     "消費します\n"
                     f"・1日 {max_runs} 回 / うち AI 質問 {max_paid} 回まで"
                 )},
                {"type": "separator"},
                {"type": "text", "weight": "bold", "size": "sm",
                 "text": "あなたが owner として登録されます"},
                {"type": "text", "wrap": True, "size": "xs", "color": "#555555",
                 "text": (
                     "・admin / member の追加・削除を実行できます\n"
                     "・AI 質問の実行とクレジット消費の責任を持ちます\n"
                     "・「DeepNote 切断」でいつでも解除できます"
                 )},
            ],
            "paddingAll": "lg",
        },
        "footer": {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "contents": [
                {"type": "button", "style": "secondary", "height": "sm",
                 "action": {
                     "type": "postback",
                     "label": "キャンセル",
                     "data": f"action=group_connect_cancel&u={line_user_id}",
                     "displayText": "キャンセル",
                 }},
                {"type": "button", "style": "primary", "color": "#1A73E8", "height": "sm",
                 "action": {
                     "type": "postback",
                     "label": "✅ 接続する",
                     "data": f"action=group_connect_confirm&u={line_user_id}",
                     "displayText": "接続する",
                 }},
            ],
            "paddingAll": "md",
        },
    }


def _handle_group_connect(reply_token: str, group_id: str, line_user_id: str, _user_text: str) -> None:
    """`DeepNote 接続` — Phase 1.5: send a confirm card; the actual
    binding happens only when the requester taps ✅ (postback
    ``action=group_connect_confirm``).

    Pre-flight checks (DM-linked / already connected) still run here so
    we can short-circuit and avoid showing a card that would lead to a
    failed confirm. The card itself encodes the requester's
    ``line_user_id`` so the postback handler can verify the same user is
    pressing the button."""
    requester = line_link_tokens.get_link(line_user_id)
    if not requester:
        line_messaging.reply(reply_token, [line_messaging.text_message(
            "DeepNote と LINE の連携が必要です。\n"
            "個人チャットで DeepNote と連携した後、もう一度このグループで「DeepNote 接続」と送ってください。"
        )])
        bot_audit.record(
            provider="line", source_type="group", source_user_id=line_user_id,
            command="group_connect_request", outcome="requester_not_linked",
        )
        return
    existing = group_acl.get_group_link("line", group_id)
    if existing:
        line_messaging.reply(reply_token, [line_messaging.text_message(
            "このグループは既に DeepNote と接続されています。\n"
            "「DeepNote 状態」で現在の設定を確認できます。"
        )])
        bot_audit.record(
            provider="line", source_type="group", source_user_id=line_user_id,
            account_id=existing.get("ownerAccountId"),
            command="group_connect_request", outcome="already_connected",
        )
        return

    limits = group_acl.daily_limits()
    bubble = _build_group_connect_confirm_card(
        account_id_short=requester["accountId"][:8],
        max_runs=limits["max_runs"],
        max_paid=limits["max_paid_runs"],
        line_user_id=line_user_id,
    )
    line_messaging.reply(reply_token, [line_messaging.flex_message(
        alt_text="DeepNote 接続確認 — このグループを連携しますか?",
        contents=bubble,
    )])
    bot_audit.record(
        provider="line", source_type="group", source_user_id=line_user_id,
        account_id=requester["accountId"], deepnote_uid=requester.get("deepnoteUid"),
        command="group_connect_request", outcome="card_shown",
    )


def _handle_group_status(reply_token: str, group_id: str, line_user_id: str) -> None:
    glink = group_acl.get_group_link("line", group_id)
    if not glink:
        line_messaging.reply(reply_token, [line_messaging.text_message(
            "このグループはまだ DeepNote と接続されていません。\n"
            "「DeepNote 接続」と送って代表アカウントを登録してください。"
        )])
        return
    member = group_acl.get_member("line", group_id, line_user_id)
    role = (member or {}).get("role", "未登録")
    members = group_acl.list_members("line", group_id, limit=20)
    owner_count = sum(1 for m in members if m.get("role") == "owner")
    admin_count = sum(1 for m in members if m.get("role") == "admin")
    member_count = sum(1 for m in members if m.get("role") == "member")
    limits = group_acl.daily_limits()
    line_messaging.reply(reply_token, [line_messaging.text_message(
        "📋 DeepNote 接続状態\n"
        f"・代表アカウント: {glink.get('ownerAccountId', '')[:8]}…\n"
        f"・あなたのロール: {role}\n"
        f"・メンバー数: owner {owner_count} / admin {admin_count} / member {member_count}\n"
        f"・1日の利用上限: {limits['max_runs']} 回 / AI 質問 {limits['max_paid_runs']} 回\n"
        f"・1人当たり上限: {limits['max_runs_per_user']} 回"
    )])
    bot_audit.record(
        provider="line", source_type="group", source_user_id=line_user_id,
        account_id=glink.get("ownerAccountId"),
        command="group_status", outcome="ok",
    )


def _handle_group_disconnect(reply_token: str, group_id: str, line_user_id: str) -> None:
    glink = group_acl.get_group_link("line", group_id)
    if not glink:
        line_messaging.reply(reply_token, [line_messaging.text_message(
            "このグループは接続されていません。"
        )])
        return
    member = group_acl.get_member("line", group_id, line_user_id)
    if not member or member.get("role") != "owner":
        line_messaging.reply(reply_token, [line_messaging.text_message(
            "切断は owner ロールのみ実行できます。"
        )])
        bot_audit.record(
            provider="line", source_type="group", source_user_id=line_user_id,
            account_id=glink.get("ownerAccountId"),
            command="group_disconnect", outcome="blocked_not_owner",
        )
        return
    group_acl.deactivate_group_link("line", group_id)
    line_messaging.reply(reply_token, [line_messaging.text_message(
        "✅ DeepNote とこのグループの接続を解除しました。\n"
        "再度接続したい場合は「DeepNote 接続」と送ってください。"
    )])
    bot_audit.record(
        provider="line", source_type="group", source_user_id=line_user_id,
        account_id=glink.get("ownerAccountId"),
        command="group_disconnect", outcome="ok",
    )


def _extract_target_line_user_id(text: str) -> Optional[str]:
    """Pick out a LINE user id (Uxxxxx... 33 chars) from the message text."""
    import re
    m = re.search(r"\b(U[0-9a-f]{32})\b", text or "")
    return m.group(1) if m else None


def _handle_group_member_add(reply_token: str, group_id: str, line_user_id: str, user_text: str) -> None:
    glink = group_acl.get_group_link("line", group_id)
    if not glink:
        line_messaging.reply(reply_token, [line_messaging.text_message(
            "先に「DeepNote 接続」でグループを接続してください。"
        )])
        return
    me = group_acl.get_member("line", group_id, line_user_id)
    if not me or me.get("role") != "owner":
        line_messaging.reply(reply_token, [line_messaging.text_message(
            "メンバー追加は owner ロールのみ実行できます。"
        )])
        return
    target = _extract_target_line_user_id(user_text)
    if not target:
        line_messaging.reply(reply_token, [line_messaging.text_message(
            "対象の LINE ユーザー ID を指定してください。\n"
            "例: 「DeepNote メンバー追加 U1234567890abcdef…」\n"
            "(LINE ユーザー ID は対象者の DeepNote 連携画面で確認できます)"
        )])
        return
    target_link = line_link_tokens.get_link(target)
    group_acl.set_member_role(
        "line", group_id, target,
        role="admin",
        deepnote_uid=(target_link or {}).get("deepnoteUid"),
        added_by=line_user_id,
    )
    line_messaging.reply(reply_token, [line_messaging.text_message(
        f"✅ {target[:8]}… を admin として追加しました。\n"
        "admin は AI 質問などクレジットを消費する操作も実行できます。"
    )])
    bot_audit.record(
        provider="line", source_type="group", source_user_id=line_user_id,
        account_id=glink.get("ownerAccountId"),
        command="group_member_add", outcome="ok",
    )


def _handle_group_member_remove(reply_token: str, group_id: str, line_user_id: str, user_text: str) -> None:
    glink = group_acl.get_group_link("line", group_id)
    if not glink:
        line_messaging.reply(reply_token, [line_messaging.text_message(
            "先に「DeepNote 接続」でグループを接続してください。"
        )])
        return
    me = group_acl.get_member("line", group_id, line_user_id)
    if not me or me.get("role") != "owner":
        line_messaging.reply(reply_token, [line_messaging.text_message(
            "メンバー削除は owner ロールのみ実行できます。"
        )])
        return
    target = _extract_target_line_user_id(user_text)
    if not target:
        line_messaging.reply(reply_token, [line_messaging.text_message(
            "対象の LINE ユーザー ID を指定してください。"
        )])
        return
    if target == line_user_id:
        line_messaging.reply(reply_token, [line_messaging.text_message(
            "自分自身を owner から外すことはできません。先に「DeepNote 切断」をご検討ください。"
        )])
        return
    if group_acl.remove_member("line", group_id, target):
        line_messaging.reply(reply_token, [line_messaging.text_message(
            f"✅ {target[:8]}… のロールを解除しました。"
        )])
    else:
        line_messaging.reply(reply_token, [line_messaging.text_message(
            "対象メンバーは登録されていません。"
        )])
    bot_audit.record(
        provider="line", source_type="group", source_user_id=line_user_id,
        account_id=glink.get("ownerAccountId"),
        command="group_member_remove", outcome="ok",
    )


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

        # Phase 1 admin commands (connect / disconnect / status / member ops).
        # Connect needs a personally-linked requester but bypasses the
        # ACL gate because the group has no link yet.
        if cmd == "group_connect":
            _handle_group_connect(reply_token, group_id, line_user_id, user_text)
            return
        if cmd == "group_status":
            _handle_group_status(reply_token, group_id, line_user_id)
            return
        if cmd == "group_disconnect":
            _handle_group_disconnect(reply_token, group_id, line_user_id)
            return
        if cmd == "group_member_add":
            _handle_group_member_add(reply_token, group_id, line_user_id, user_text)
            return
        if cmd == "group_member_remove":
            _handle_group_member_remove(reply_token, group_id, line_user_id, user_text)
            return

        # Phase 1 — every other command goes through the group ACL gate.
        # The gate decides: connect required? private blocked? paid
        # admin-only? daily cap hit? On success it returns the
        # data_owner / billing_owner UID + accountId pair the rest of
        # this branch must use (NOT the requester's link).
        ctx = group_acl.resolve_group_execution_context(
            provider="line", workspace_id=group_id,
            source_user_id=line_user_id, intent=cmd,
        )
        if isinstance(ctx, group_acl.RequireGroupConnect):
            line_messaging.reply(reply_token, [line_messaging.text_message(ctx.connect_hint)])
            bot_audit.record(
                provider="line", source_type=source_type,
                source_user_id=line_user_id,
                account_id=(link or {}).get("accountId"),
                command=cmd, outcome=ctx.audit_outcome,
            )
            return
        if isinstance(ctx, group_acl.Denied):
            line_messaging.reply(reply_token, [line_messaging.text_message(ctx.reason)])
            bot_audit.record(
                provider="line", source_type=source_type,
                source_user_id=line_user_id,
                account_id=(link or {}).get("accountId"),
                command=cmd, outcome=ctx.audit_outcome,
            )
            return
        # ctx is now an ExecutionContext.
        data_account_id = ctx.data_owner_account_id
        data_uid = ctx.data_owner_deepnote_uid
        ws_key = f"line:{group_id}"

        # 「自動共有」 (Lv4) is retired for safety. Show the migration notice.
        if cmd == "auto_share_deprecated":
            line_messaging.reply(reply_token, [line_messaging.text_message(M.AUTO_SHARE_DEPRECATED)])
            bot_audit.record(
                provider="line", source_type=source_type,
                source_user_id=line_user_id,
                account_id=data_account_id, deepnote_uid=data_uid,
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
                account_id=data_account_id, deepnote_uid=data_uid,
                command=cmd, outcome="redirect_to_dm",
            )
            return
        # Paid action: route to assistant_hub charging billing_owner
        # (Phase 1 = data_owner). The hub doesn't accept the trio yet,
        # so we pass billing_owner as the account / uid pair — that's
        # the correct cost_guard target.
        if cmd == "assistant_qna":
            q = (user_text or "").strip()
            for prefix in ("質問:", "質問：", "質問", "ask "):
                if q.startswith(prefix):
                    q = q[len(prefix):].strip()
                    break
            if not q:
                line_messaging.reply(reply_token, [line_messaging.text_message(
                    "質問を入力してください。例: 「決定事項は？」「TODO は？」")])
                return
            try:
                from app.services import assistant_hub
                result = _run_coroutine(assistant_hub.handle_message(
                    account_id=ctx.billing_owner_account_id,
                    owner_uid=ctx.billing_owner_deepnote_uid,
                    question=q, session_id=None, mode="session",
                    channel="line", idempotency_key=None,
                ))
                answer = result.get("answer") or "回答を生成できませんでした。"
            except Exception as _e:
                logger.warning("[line.qna.group] hub call failed: %s", _e)
                answer = "Assistant へのリクエストに失敗しました。少し時間をおいて再度お試しください。"
            line_messaging.reply(reply_token, [line_messaging.text_message(answer)])
            bot_audit.record(
                provider="line", source_type=source_type,
                source_user_id=line_user_id,
                account_id=ctx.billing_owner_account_id,
                deepnote_uid=ctx.billing_owner_deepnote_uid,
                command=cmd, outcome="ok_paid",
            )
            return

        # Phase 1.5: when a group bot has nothing to show, surface a
        # *picker* of recent 3-5 sessions (instead of auto-offering only
        # the latest 1). The picker buttons each carry a share_confirm
        # postback so the tap itself = explicit consent. The share
        # handler re-validates session ownership at execution time.
        def _send_proactive_share_offer_or_text() -> bool:
            try:
                candidates = group_shared_briefing.get_recent_any_sessions(
                    data_account_id, limit=5,
                )
                if not candidates:
                    return False
                bubble = _build_session_picker_bubble(candidates, group_id=group_id)
                line_messaging.reply(reply_token, [line_messaging.flex_message(
                    alt_text="共有する会議を選んでください",
                    contents=bubble,
                )])
                bot_audit.record(
                    provider="line", source_type=source_type,
                    source_user_id=line_user_id,
                    account_id=data_account_id, deepnote_uid=data_uid,
                    command="session_picker",
                    outcome=f"shown_{len(candidates)}",
                )
                return True
            except Exception as _e:
                logger.warning("[line.picker] picker render failed: %s", _e)
                return False

        def _no_data_text() -> str:
            # Reused as the *fallback* string when the picker can't be
            # rendered (e.g. no sessions at all on the data_owner side).
            try:
                latest = group_shared_briefing.get_latest_any_session(data_account_id)
                if latest and latest.get("title"):
                    return M.GROUP_NO_SHARED_DATA_WITH_HINT.format(title=latest["title"][:40])
            except Exception:
                pass
            return M.GROUP_NO_SHARED_DATA

        if cmd == "decisions":
            decisions = group_shared_briefing.get_recent_shared_decisions(
                data_account_id, ws_key, limit=3
            )
            if decisions:
                text = _format_decisions(decisions)
                line_messaging.reply(reply_token, [line_messaging.text_message(text)])
            else:
                if not _send_proactive_share_offer_or_text():
                    line_messaging.reply(reply_token, [line_messaging.text_message(_no_data_text())])
                text = M.GROUP_NO_SHARED_DATA
        elif cmd == "latest" or cmd == "help":
            shared = group_shared_briefing.get_latest_shared_session(data_account_id, ws_key)
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
            account_id=data_account_id, deepnote_uid=data_uid,
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
    # 1:1 unlink: ログアウト / logout — delete the LINE userId ↔ account
    # link so the user can connect a different account next time. Only
    # reachable in 1:1 source_type=="user" path; group context never
    # gets here.
    if command == "user_unlink":
        deleted = line_link_tokens.delete_link(line_user_id)
        msg = M.USER_UNLINKED_OK if deleted else M.USER_UNLINK_NOT_LINKED
        line_messaging.reply(reply_token, [line_messaging.text_message(msg)])
        bot_audit.record(
            provider="line", source_type="user",
            source_user_id=line_user_id,
            account_id=link.get("accountId"),
            deepnote_uid=link.get("deepnoteUid"),
            command="user_unlink",
            outcome="ok" if deleted else "not_linked",
        )
        return

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
    """Handle confirmation postbacks.

    Phase 1.5 actions added:
      ``action=group_connect_confirm&u=<line_user_id>`` — finalise the
        group binding (only after the speaker has tapped ✅ on the
        confirm card emitted by ``DeepNote 接続``).
      ``action=group_connect_cancel&u=<line_user_id>`` — abandon the
        connect flow without writing anything.

    Lv3 share actions (existing):
      ``action=share_confirm&sid=<sessionId>&dest=<groupOrUserId>&attach=0|1``
      ``action=share_cancel&sid=...``

    The session picker (Phase 1.5) emits ``share_confirm`` directly so
    the user's tap is the explicit consent — no intermediate step.
    """
    source = event.get("source") or {}
    line_user_id = source.get("userId")
    group_id = source.get("groupId") or source.get("roomId")
    reply_token = event.get("replyToken")
    raw = (event.get("postback") or {}).get("data") or ""
    if not raw or not reply_token:
        return
    from urllib.parse import parse_qs
    parsed = parse_qs(raw)
    action = (parsed.get("action") or [""])[0]

    # ── Phase 1.5: group connect confirmation ────────────────────────
    if action in ("group_connect_confirm", "group_connect_cancel"):
        intended_user = (parsed.get("u") or [""])[0]
        # Verify the user pressing the button is the same one who
        # originally typed ``DeepNote 接続`` — prevents random group
        # members from registering the original speaker as owner.
        if not line_user_id or not group_id:
            return
        if intended_user and intended_user != line_user_id:
            line_messaging.reply(reply_token, [line_messaging.text_message(
                "この確認は接続を依頼した本人のみが操作できます。"
            )])
            bot_audit.record(
                provider="line", source_type="postback",
                source_user_id=line_user_id,
                command="group_connect", outcome="confirm_user_mismatch",
            )
            return

        if action == "group_connect_cancel":
            line_messaging.reply(reply_token, [line_messaging.text_message(
                "接続をキャンセルしました。"
            )])
            bot_audit.record(
                provider="line", source_type="postback",
                source_user_id=line_user_id,
                command="group_connect", outcome="cancelled",
            )
            return

        # group_connect_confirm: re-run pre-flight before writing
        requester = line_link_tokens.get_link(line_user_id)
        if not requester:
            line_messaging.reply(reply_token, [line_messaging.text_message(
                "DeepNote 連携が解除されたため接続できません。再度 DM で連携してから「DeepNote 接続」と送ってください。"
            )])
            bot_audit.record(
                provider="line", source_type="postback",
                source_user_id=line_user_id,
                command="group_connect", outcome="requester_not_linked_at_confirm",
            )
            return
        existing = group_acl.get_group_link("line", group_id)
        if existing:
            line_messaging.reply(reply_token, [line_messaging.text_message(
                "このグループは既に接続されています。"
            )])
            bot_audit.record(
                provider="line", source_type="postback",
                source_user_id=line_user_id,
                account_id=existing.get("ownerAccountId"),
                command="group_connect", outcome="already_connected_at_confirm",
            )
            return
        try:
            group_acl.create_group_link(
                "line", group_id,
                owner_deepnote_uid=requester.get("deepnoteUid", ""),
                owner_account_id=requester["accountId"],
                created_by_source_user_id=line_user_id,
            )
        except Exception as e:
            logger.warning("[line.group_connect] confirm create_link failed: %s", e)
            line_messaging.reply(reply_token, [line_messaging.text_message(
                "グループ接続に失敗しました。少し時間をおいてから再度お試しください。"
            )])
            return
        limits = group_acl.daily_limits()
        line_messaging.reply(reply_token, [line_messaging.text_message(
            "✅ DeepNote をこのグループに接続しました。\n"
            f"・代表アカウント: {requester['accountId'][:8]}…\n"
            f"・1日の利用上限: {limits['max_runs']} 回 (うち AI 質問は {limits['max_paid_runs']} 回まで)\n"
            "・他のメンバーは「最新」「決定事項」など読み取り操作のみ可能です\n"
            "・AI 質問はオーナー / 管理者のみ実行できます\n"
            "「DeepNote メンバー追加 <LINEユーザーID>」で管理者を追加できます。"
        )])
        bot_audit.record(
            provider="line", source_type="postback",
            source_user_id=line_user_id,
            account_id=requester["accountId"],
            deepnote_uid=requester.get("deepnoteUid"),
            command="group_connect", outcome="ok",
        )
        return

    if action == "share_cancel":
        line_messaging.reply(reply_token, [line_messaging.text_message("キャンセルしました。")])
        bot_audit.record(
            provider="line", source_type="postback",
            source_user_id=line_user_id or "",
            command="share_confirm", outcome="cancelled",
        )
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
        bot_audit.record(
            provider="line", source_type="postback",
            source_user_id=line_user_id,
            command="share_confirm", outcome="requester_not_linked",
        )
        return
    try:
        snap = db.collection("sessions").document(sid).get()
        sd = snap.to_dict() if snap.exists else {}
    except Exception:
        sd = {}
    # Phase 1.5: tighten owner re-verification at execution time.
    # Reject (a) missing session, (b) soft-deleted session, (c) session
    # whose ``ownerAccountId`` does not match the requester's link, and
    # (d) (when ``dest`` is a group) requester missing from the group
    # ACL — they may have been removed between picker render and tap.
    if not sd:
        line_messaging.reply(reply_token, [line_messaging.text_message(
            "対象会議が見つかりません。")])
        bot_audit.record(
            provider="line", source_type="postback",
            source_user_id=line_user_id,
            account_id=link.get("accountId"),
            command="share_confirm", outcome="session_not_found",
        )
        return
    if sd.get("isDeleted") or sd.get("deletedAt"):
        line_messaging.reply(reply_token, [line_messaging.text_message(
            "対象会議は既に削除されています。")])
        bot_audit.record(
            provider="line", source_type="postback",
            source_user_id=line_user_id,
            account_id=link.get("accountId"),
            command="share_confirm", outcome="session_deleted",
        )
        return
    if sd.get("ownerAccountId") != link.get("accountId"):
        line_messaging.reply(reply_token, [line_messaging.text_message(
            "対象会議の共有権限がありません。")])
        bot_audit.record(
            provider="line", source_type="postback",
            source_user_id=line_user_id,
            account_id=link.get("accountId"),
            command="share_confirm", outcome="ownership_mismatch",
        )
        return
    # If sharing into a group/room, require that the requester is on
    # the ACL (auto-promoted to ``member`` on first sight by
    # resolve_group_execution_context, so missing == they were
    # explicitly removed by an owner).
    if dest.startswith(("C", "G", "R")) and dest != line_user_id:
        glink = group_acl.get_group_link("line", dest)
        if glink:
            member = group_acl.get_member("line", dest, line_user_id)
            if not member:
                line_messaging.reply(reply_token, [line_messaging.text_message(
                    "このグループでの共有権限が解除されています。")])
                bot_audit.record(
                    provider="line", source_type="postback",
                    source_user_id=line_user_id,
                    account_id=link.get("accountId"),
                    command="share_confirm", outcome="not_on_group_acl",
                )
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
    just like the rest of the API. After a successful link the bot pushes
    a confirmation message to the LINE chat so the user sees they are
    signed in even if the deeplink-back-to-app step is silently lost.
    """
    try:
        data = line_link_tokens.consume(
            token,
            deepnote_uid=current_user.uid,
            account_id=current_user.account_id,
        )
    except line_link_tokens.TokenError as e:
        raise HTTPException(status_code=e.status, detail=e.code)

    line_user_id = data.get("lineUserId")
    if line_user_id and line_messaging.is_configured():
        identity = (
            current_user.email
            or current_user.display_name
            or f"uid:{current_user.uid[:12]}…"
        )
        msg = (
            "✅ DeepNote にログインできました。\n"
            f"アカウント: {identity}\n\n"
            "「ヘルプ」と送ると使えるコマンド一覧が出ます。\n"
            "別のアカウントに切り替えるには「ログアウト」と送ってください。"
        )
        try:
            line_messaging.push(line_user_id, [line_messaging.text_message(msg)])
        except Exception as e:
            # Push failure must not fail the link confirmation; the
            # browser success page already shows the same info.
            logger.warning("[line.consume] post-link push failed lineUserId=%s err=%s",
                           line_user_id, e)

    return {
        "linked": True,
        "lineUserId": line_user_id,
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
