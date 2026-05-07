"""DeepNote Assistant Hub — REST surface (Phase A).

Endpoints:
    POST /v1/assistant/messages
        Submit a question. Routes through assistant_hub → assistant_qna.

    GET /v1/assistant/conversations/{conversationId}
        Stub for Phase A. Returns 200 + empty list so iOS callers
        don't hit 404. Conversation tree lands in Phase B.

    POST /v1/assistant/actions
        Phase B placeholder. Returns 501 in Phase A.

Auth: every route requires a Firebase ID token; the request runs in the
caller's session-owner scope. Cross-account access is impossible
because ``assistant_qna.resolve_session_id`` filters on
``ownerUserId == current_user.uid``.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_current_user, CurrentUser
from app.services import assistant_hub

logger = logging.getLogger("app.routes.assistant")

router = APIRouter(prefix="/v1/assistant", tags=["Assistant"])


class AssistantMessageRequest(BaseModel):
    sessionId: Optional[str] = Field(None, description="Target session id; defaults to caller's latest session")
    question: str = Field(..., description="Free-form question, e.g. 「決定事項は？」")
    mode: Optional[str] = Field("session", description="'session' (default) or 'general'")
    channel: Optional[str] = Field("ios", description="ios / desktop / slack / line — for audit")
    idempotencyKey: Optional[str] = Field(None, description="Dedupe key for retries")


class AssistantCitation(BaseModel):
    type: str
    id: str
    snippet: str


class AssistantTokenUsage(BaseModel):
    prompt: int = 0
    completion: int = 0


class AssistantMessageResponse(BaseModel):
    messageId: Optional[str]
    intent: Optional[str]
    answer: str
    citations: List[AssistantCitation] = []
    sessionId: Optional[str]
    tokenUsage: AssistantTokenUsage = AssistantTokenUsage()
    createdAt: Optional[str]
    cached: bool = False


@router.post("/messages", response_model=AssistantMessageResponse)
async def post_message(
    req: AssistantMessageRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    if not (req.question or "").strip():
        raise HTTPException(status_code=422, detail="question is required")

    account_id = getattr(current_user, "account_id", None) or current_user.uid
    result = await assistant_hub.handle_message(
        account_id=account_id,
        owner_uid=current_user.uid,
        question=req.question,
        session_id=req.sessionId,
        mode=(req.mode or "session"),
        channel=(req.channel or "ios"),
        idempotency_key=req.idempotencyKey,
    )
    # Pydantic will coerce the dict; citations field tolerates missing
    # keys via the AssistantCitation model.
    return AssistantMessageResponse(
        messageId=result.get("messageId"),
        intent=result.get("intent"),
        answer=result.get("answer") or "",
        citations=[AssistantCitation(**c) for c in (result.get("citations") or []) if isinstance(c, dict)],
        sessionId=result.get("sessionId"),
        tokenUsage=AssistantTokenUsage(**(result.get("tokenUsage") or {})),
        createdAt=result.get("createdAt"),
        cached=bool(result.get("cached")),
    )


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    account_id = getattr(current_user, "account_id", None) or current_user.uid
    msgs = assistant_hub.list_conversation_messages(conversation_id, account_id)
    return {"conversationId": conversation_id, "messages": msgs}


# ──────────────────────────────────────────────────────────────────────
# Share confirmation cards (Smart Share Lv3 — Phase B)
# ──────────────────────────────────────────────────────────────────────

class SharePreviewRequest(BaseModel):
    sessionId: str = Field(..., description="Session to share")
    channel: str = Field(..., description="'slack' | 'line'")
    destination: dict = Field(..., description="{teamId, channelId} for slack; {groupId} for line")
    includeSummary: bool = True
    includeTodos: bool = True
    includeDecisions: bool = True
    attachPdf: bool = False


@router.post("/share:preview")
async def preview_share(req: SharePreviewRequest, current_user: CurrentUser = Depends(get_current_user)):
    """Build the exact card the user is about to send and return it for
    explicit human review. The actual ``share:confirm`` step (which
    posts into the destination) is intentionally a separate request so
    a misclick or stale page never publishes.
    """
    from app.firebase import db as _db
    snap = _db.collection("sessions").document(req.sessionId).get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="session_not_found")
    sd = snap.to_dict() or {}
    if (sd.get("ownerUserId") != current_user.uid
            and sd.get("ownerAccountId") != getattr(current_user, "account_id", None)):
        raise HTTPException(status_code=403, detail="not_owner")

    title = sd.get("title") or "(無題)"
    blocks = [f"📝 {title} を共有予定:"]
    if req.includeSummary:
        topic = sd.get("topicSummary") or ""
        if topic:
            blocks.append(f"・要約: {topic[:160]}")
    if req.includeDecisions:
        decisions = (sd.get("summaryJson") or {}).get("decisions") or []
        if decisions:
            blocks.append(f"・決定事項: {len(decisions)} 件")
    if req.includeTodos:
        try:
            account_id = sd.get("ownerAccountId") or ""
            if account_id:
                t_q = (
                    _db.collection("accounts").document(account_id)
                    .collection("todos").where("sessionId", "==", req.sessionId).limit(20)
                )
                cnt = sum(1 for _ in t_q.stream())
                if cnt:
                    blocks.append(f"・TODO: {cnt} 件")
        except Exception:
            pass
    if req.attachPdf:
        blocks.append("・PDF を添付")

    return {
        "sessionId": req.sessionId,
        "channel": req.channel,
        "destination": req.destination,
        "preview": "\n".join(blocks),
        "warning": (
            "共有先のメンバー全員が閲覧可能になります。プライバシー保護のため、"
            "共有を確定するには share:confirm を別途呼び出してください。"
        ),
    }


class ShareConfirmRequest(SharePreviewRequest):
    confirm: bool = Field(..., description="MUST be true; explicit human ack")


@router.post("/share:confirm")
async def confirm_share(req: ShareConfirmRequest, current_user: CurrentUser = Depends(get_current_user)):
    if not req.confirm:
        raise HTTPException(status_code=400, detail="confirm flag must be true")
    # Re-do ownership check identical to preview.
    from app.firebase import db as _db
    snap = _db.collection("sessions").document(req.sessionId).get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="session_not_found")
    sd = snap.to_dict() or {}
    if (sd.get("ownerUserId") != current_user.uid
            and sd.get("ownerAccountId") != getattr(current_user, "account_id", None)):
        raise HTTPException(status_code=403, detail="not_owner")

    title = sd.get("title") or "(無題)"
    text_lines = [f"📝 {title}"]
    if req.includeSummary and sd.get("topicSummary"):
        text_lines.append(sd["topicSummary"][:300])
    if req.includeDecisions:
        for d in ((sd.get("summaryJson") or {}).get("decisions") or [])[:5]:
            txt = d.get("text") if isinstance(d, dict) else str(d)
            if txt:
                text_lines.append(f"・{txt}")
    text = "\n".join(text_lines)

    posted = False
    if req.channel == "slack":
        try:
            from app.services.integrations import slack_client
            slack_client.post_message(
                team_id=req.destination.get("teamId") or req.destination.get("workspaceId") or "",
                channel=req.destination.get("channelId") or "",
                text=text,
            )
            posted = True
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"slack_post_failed: {e}")
    elif req.channel == "line":
        try:
            from app.services import line_messaging
            target = req.destination.get("groupId") or req.destination.get("lineUserId") or ""
            if not target or not line_messaging.is_configured():
                raise HTTPException(status_code=400, detail="line_destination_or_config_missing")
            line_messaging.push(target, [line_messaging.text_message(text)])
            posted = True
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"line_push_failed: {e}")
    else:
        raise HTTPException(status_code=400, detail="unsupported_channel")

    # Append the destination workspace key to the session's
    # sharedToWorkspaceTeams so the corresponding group bot can later
    # reference this meeting via 「最新」 / 「決定事項」 commands.
    try:
        ws_key = ""
        if req.channel == "slack":
            tid = req.destination.get("teamId") or req.destination.get("workspaceId")
            if tid:
                ws_key = f"slack:{tid}"
        elif req.channel == "line":
            gid = req.destination.get("groupId")
            if gid:
                ws_key = f"line:{gid}"
        if ws_key:
            existing = list(sd.get("sharedToWorkspaceTeams") or [])
            if ws_key not in existing:
                _db.collection("sessions").document(req.sessionId).update(
                    {"sharedToWorkspaceTeams": existing + [ws_key]}
                )
    except Exception:
        pass

    return {"sessionId": req.sessionId, "channel": req.channel, "posted": posted}


class ShareSuggestRequest(BaseModel):
    sessionId: str = Field(..., description="Session to find share targets for")


@router.post("/share:suggest")
async def suggest_share(req: ShareSuggestRequest, current_user: CurrentUser = Depends(get_current_user)):
    """AI-assisted share-target suggestion. Ranks workspace destinations
    by past sharing behaviour for the same account."""
    account_id = getattr(current_user, "account_id", None) or current_user.uid
    from app.services import assistant_briefing
    return {
        "sessionId": req.sessionId,
        "suggestions": assistant_briefing.suggest_share_targets(account_id, req.sessionId),
    }


class AssistantAction(BaseModel):
    type: str = Field(..., description="'query' | 'share' | 'schedule' | 'export'")
    payload: dict = Field(default_factory=dict)
    idempotencyKey: Optional[str] = None


@router.post("/actions")
async def post_action(
    req: AssistantAction,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Phase C: tool execution dispatch.

    Supported types:
      - ``query``    → forwards payload.question (etc.) to assistant_hub
      - ``share``    → forwards to share:confirm (requires payload.confirm)
      - ``schedule`` → forwards to scheduled_tasks.create
      - ``export``   → enqueues an export job (PDF/DOCX/PPTX). Returns
                       the export id; download URL becomes available
                       when the export pipeline completes (existing
                       /sessions/{id}/exports flow).
    """
    account_id = getattr(current_user, "account_id", None) or current_user.uid
    t = (req.type or "").lower()
    payload = req.payload or {}

    if t == "query":
        from app.services import assistant_hub as _hub
        result = await _hub.handle_message(
            account_id=account_id,
            owner_uid=current_user.uid,
            question=payload.get("question") or "",
            session_id=payload.get("sessionId"),
            mode=payload.get("mode") or "session",
            channel=payload.get("channel") or "ios",
            idempotency_key=req.idempotencyKey,
        )
        return {"action": "query", "result": result}

    if t == "share":
        if not payload.get("confirm"):
            raise HTTPException(status_code=400, detail="payload.confirm must be true")
        sub = ShareConfirmRequest(
            sessionId=payload.get("sessionId") or "",
            channel=payload.get("channel") or "",
            destination=payload.get("destination") or {},
            includeSummary=bool(payload.get("includeSummary", True)),
            includeTodos=bool(payload.get("includeTodos", True)),
            includeDecisions=bool(payload.get("includeDecisions", True)),
            attachPdf=bool(payload.get("attachPdf", False)),
            confirm=True,
        )
        return await confirm_share(sub, current_user=current_user)

    if t == "schedule":
        from app.services import scheduled_tasks as _st
        try:
            return {"action": "schedule", "result": _st.create(account_id, body=payload)}
        except ValueError as ve:
            raise HTTPException(status_code=400, detail=str(ve))

    if t == "email":
        provider = (payload.get("provider") or "google").lower()
        to = payload.get("to") or []
        subject = payload.get("subject") or ""
        body_text = payload.get("bodyText") or ""
        body_html = payload.get("bodyHtml")
        cc = payload.get("cc") or None
        bcc = payload.get("bcc") or None
        if not to:
            raise HTTPException(status_code=400, detail="payload.to is required")
        try:
            if provider == "google":
                from app.services.integrations import google_client
                res = google_client.send_gmail_message(
                    uid=current_user.uid, to=to, subject=subject,
                    body_text=body_text, cc=cc, bcc=bcc, body_html=body_html,
                )
                return {"action": "email", "provider": "google", "result": res}
            if provider == "microsoft":
                from app.services.integrations import microsoft_client
                res = microsoft_client.send_mail(
                    uid=current_user.uid, to=to, subject=subject,
                    body_text=body_text, cc=cc, bcc=bcc, body_html=body_html,
                )
                return {"action": "email", "provider": "microsoft", "result": res}
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"email_send_failed: {e}")
        raise HTTPException(status_code=400, detail=f"unsupported email provider: {provider}")

    if t == "calendar_event":
        provider = (payload.get("provider") or "google").lower()
        summary = payload.get("summary") or payload.get("subject") or ""
        start = payload.get("start") or ""
        end = payload.get("end") or ""
        attendees = payload.get("attendees") or None
        location = payload.get("location") or None
        description = payload.get("description") or payload.get("bodyText") or None
        if not summary or not start or not end:
            raise HTTPException(status_code=400, detail="summary/start/end are required")
        try:
            if provider == "google":
                from app.services.integrations import google_client
                res = google_client.create_calendar_event(
                    uid=current_user.uid, summary=summary, start=start, end=end,
                    description=description, attendees=attendees, location=location,
                )
                return {"action": "calendar_event", "provider": "google", "result": res}
            if provider == "microsoft":
                from app.services.integrations import microsoft_client
                res = microsoft_client.create_calendar_event(
                    uid=current_user.uid, subject=summary, start=start, end=end,
                    body_text=description, attendees=attendees, location=location,
                    timezone_name=payload.get("timezone") or "Asia/Tokyo",
                )
                return {"action": "calendar_event", "provider": "microsoft", "result": res}
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"calendar_create_failed: {e}")
        raise HTTPException(status_code=400, detail=f"unsupported calendar provider: {provider}")

    if t == "export":
        # Hand off to the existing exports pipeline rather than re-implementing
        # the work here. We just enqueue and return the job id; the export
        # flow already supports ``format=pdf|docx|pptx``.
        sid = payload.get("sessionId") or ""
        fmt = (payload.get("format") or "pdf").lower()
        if fmt not in ("pdf", "docx", "pptx"):
            raise HTTPException(status_code=400, detail="format must be pdf | docx | pptx")
        try:
            from app.task_queue import enqueue_summarize_task  # type: ignore  # noqa: F401  (warm import)
            from google.cloud import tasks_v2
            import os, json as _json
            project = os.environ.get("GCP_PROJECT", "classnote-x-dev")
            location = os.environ.get("TASKS_LOCATION", "asia-northeast1")
            queue = os.environ.get("EXPORT_QUEUE", "summarize-queue")
            url = f"{os.environ.get('CLOUD_RUN_SERVICE_URL', '')}/internal/tasks/export"
            client = tasks_v2.CloudTasksClient()
            parent = client.queue_path(project, location, queue)
            task = {"http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": url,
                "headers": {"Content-Type": "application/json"},
                "body": _json.dumps({"sessionId": sid, "format": fmt,
                                      "userId": current_user.uid,
                                      "accountId": account_id}).encode(),
            }}
            client.create_task(parent=parent, task=task)
            return {"action": "export", "status": "enqueued", "format": fmt, "sessionId": sid}
        except Exception as e:
            logger.warning("[assistant.action.export] enqueue failed: %s", e)
            raise HTTPException(status_code=502, detail=f"export_enqueue_failed: {e}")

    raise HTTPException(status_code=400, detail=f"unsupported action type: {req.type}")
