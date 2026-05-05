"""Session export endpoint — generates PDF / DOCX / PPTX files server-side."""
import io
import os
import uuid
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Literal, Optional, Any, Dict

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from app.firebase import db, storage_client, MEDIA_BUCKET_NAME
from app.dependencies import get_current_user, CurrentUser, ensure_can_view
from app.routes.sessions import _get_cached_signing_credentials
from app.services.cost_guard import cost_guard
from app.services.ai_credits import ai_credits, estimate_cost

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Request / Response
# ---------------------------------------------------------------------------

class ExportRequest(BaseModel):
    format: Literal["docx", "pptx", "pdf"]
    includeTranscript: bool = False


class ExportResponse(BaseModel):
    downloadUrl: str
    expiresAt: str
    filename: str


class ExportReserveResponse(BaseModel):
    allowed: bool
    remaining: int
    limit: int


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/sessions/{session_id}/export/reserve", response_model=ExportReserveResponse)
async def reserve_export(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Reserve one export quota unit (for client-side PDF generation).

    Call this before generating a PDF locally to ensure the user has quota.
    The reservation is consumed even if the client-side generation fails.
    """
    allowed, detail = await cost_guard.guard_can_consume(
        current_user.account_id, "export_generated", 1.0,
        mode="account", user_id=current_user.uid,
    )
    if not allowed:
        limit = (detail or {}).get("limit", 0)
        used = (detail or {}).get("used", 0)
        raise HTTPException(
            status_code=429,
            detail={
                "code": "EXPORT_LIMIT",
                "message": f"今月のエクスポート回数（{int(limit)}回）の上限に達しました。",
                "limit": int(limit),
                "used": int(used),
            },
        )

    # Get remaining after reservation
    report = await cost_guard.get_usage_report(
        current_user.account_id, mode="account", user_id=current_user.uid,
    )
    export_used = report.get("exportGenerated", 0)
    from app.services.cost_guard import FREE_LIMITS, BASIC_LIMITS, _normalize_plan
    plan = report.get("plan", "free")
    export_limit = BASIC_LIMITS["export_generated"] if plan == "basic" else FREE_LIMITS["export_generated"]

    # AI Credits consumption
    ai_credits.consume(current_user.account_id, estimate_cost("export_generated"), "export_generated")

    logger.info(f"[Export] Reserved PDF quota for session={session_id}, user={current_user.uid}")

    return ExportReserveResponse(
        allowed=True,
        remaining=max(0, int(export_limit) - int(export_used)),
        limit=int(export_limit),
    )


@router.post("/sessions/{session_id}/export", response_model=ExportResponse)
async def export_session(
    session_id: str,
    body: ExportRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Export session summary as PDF, DOCX, or PPTX.

    The file is generated in-memory, uploaded to GCS, and a signed download
    URL is returned.
    """
    # 0. Quota gate — uses cost_guard (Free: 3/month, Standard: unlimited)
    allowed, detail = await cost_guard.guard_can_consume(
        current_user.account_id, "export_generated", 1.0,
        mode="account", user_id=current_user.uid,
    )
    if not allowed:
        limit = (detail or {}).get("limit", 0)
        used = (detail or {}).get("used", 0)
        raise HTTPException(
            status_code=429,
            detail={
                "code": "EXPORT_LIMIT",
                "message": f"今月のエクスポート回数（{int(limit)}回）の上限に達しました。",
                "limit": int(limit),
                "used": int(used),
            },
        )

    # AI Credits consumption
    ai_credits.consume(current_user.account_id, estimate_cost("export_generated"), "export_generated")

    # 1. Load session from Firestore
    doc_ref = db.collection("sessions").document(session_id)
    snapshot = doc_ref.get()
    if not snapshot.exists:
        raise HTTPException(404, "Session not found")

    data = snapshot.to_dict()
    ensure_can_view(data, current_user, session_id)

    # 2. Build normalised export payload
    payload = _build_export_payload(data, body.includeTranscript)

    # 3. Render — renderers accept **kwargs and pick what they need
    if body.format == "pdf":
        from app.services.export_pdf import render_pdf
        file_bytes = render_pdf(**payload)
        content_type = "application/pdf"
        ext = "pdf"
    elif body.format == "docx":
        from app.services.export_docx import render_docx
        file_bytes = render_docx(**payload)
        content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ext = "docx"
    else:
        # EXPORT_PPTX_V2_ENABLED=1 routes to the presentation-IR based renderer
        # (export_pptx_v2). Default keeps the original `render_pptx(**payload)`
        # path untouched so rollbacks are a single env-var flip.
        if os.environ.get("EXPORT_PPTX_V2_ENABLED") == "1":
            from app.services.presentation_ir import build_presentation_ir
            from app.services.export_pptx_v2 import render_pptx_from_ir
            ir = build_presentation_ir(payload)
            file_bytes = render_pptx_from_ir(ir)
            logger.info(f"[Export] pptx v2 rendered session={session_id} slides={len(ir.get('slides') or [])}")
        else:
            from app.services.export_pptx import render_pptx
            file_bytes = render_pptx(**payload)
        content_type = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ext = "pptx"

    # 4. Upload to GCS (temp path, auto-deleted by lifecycle)
    safe_title = _safe_filename(data.get("title", "export"))
    filename = f"{safe_title}.{ext}"
    blob_path = f"exports/{current_user.uid}/{session_id}/{uuid.uuid4().hex}.{ext}"

    bucket = storage_client.bucket(MEDIA_BUCKET_NAME)
    blob = bucket.blob(blob_path)
    blob.upload_from_string(file_bytes, content_type=content_type)

    # 5. Generate signed download URL (1 hour) with proper filename
    creds = _get_cached_signing_credentials()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    signed_url = blob.generate_signed_url(
        version="v4",
        expiration=expires_at,
        method="GET",
        credentials=creds,
        response_disposition=f'attachment; filename="{filename}"',
    )

    logger.info(f"[Export] Generated {ext} for session={session_id}, user={current_user.uid}, size={len(file_bytes)}")

    return ExportResponse(
        downloadUrl=signed_url,
        expiresAt=expires_at.isoformat(),
        filename=filename,
    )


# ---------------------------------------------------------------------------
# Payload Builder
# ---------------------------------------------------------------------------

def _build_export_payload(data: dict, include_transcript: bool) -> dict:
    """Convert Firestore session data into the kwargs expected by renderers.

    Returns a rich dict that preserves every field of the summary JSON so that
    PDF / DOCX / PPTX renderers can show the full content.
    """
    title = data.get("title", "Untitled")
    mode = (data.get("summaryType") or data.get("type", "meeting")).lower()

    # Date
    created = data.get("createdAt")
    date_text = None
    if created:
        if hasattr(created, "isoformat"):
            date_text = created.strftime("%Y年%m月%d日 %H:%M")
        elif isinstance(created, str):
            date_text = created[:16]

    # Duration
    dur = data.get("durationSec")
    duration_text = _fmt_duration(dur) if dur else None

    # Summary JSON
    summary_json = data.get("summaryJson") or {}
    root = summary_json.get("summary", summary_json) if isinstance(summary_json, dict) else {}

    highlights = _highlights_rich(root.get("highlights"))
    overview = _extract_text_or_obj(root.get("overview")) or _extract_text_or_obj(root.get("theme"))
    keywords = _extract_keywords(root)

    # Top-level meeting headline fields (new — previously dropped)
    bottom_line = _extract_text_or_obj(root.get("bottomLine")) if isinstance(root, dict) else None
    why_it_matters = _extract_text_or_obj(root.get("whyItMatters")) if isinstance(root, dict) else None
    outcome_status = _extract_text_or_obj(root.get("outcomeStatus")) if isinstance(root, dict) else None
    participants = _obj_array(root.get("participants")) if isinstance(root, dict) else []

    if mode == "lecture":
        sections = _lecture_sections(root)
    else:
        sections = _meeting_sections(root)

    # Fallback to markdown
    if not highlights and not sections and not bottom_line:
        md = data.get("summaryMarkdown") or ""
        if md:
            sections = [{"type": "text", "title": "サマリー", "body": md}]

    transcript = data.get("transcriptText") if include_transcript else None

    # Raw structured data (kept for backward compat with PPTX renderer path)
    decisions_raw = _obj_array(root.get("decisions")) if isinstance(root, dict) else []
    todos_raw = _obj_array(root.get("todos")) if isinstance(root, dict) else []
    open_questions_raw = _obj_array(root.get("openQuestions")) if isinstance(root, dict) else []

    return dict(
        title=title,
        date_text=date_text,
        duration_text=duration_text,
        mode=mode,
        highlights=highlights,
        overview=overview,
        keywords=keywords,
        sections=sections,
        transcript=transcript,
        # Rich headline fields
        bottom_line=bottom_line,
        why_it_matters=why_it_matters,
        outcome_status=outcome_status,
        participants=participants or None,
        # Raw blocks still passed through (PPTX uses them for its custom card layout)
        decisions_raw=decisions_raw or None,
        todos_raw=todos_raw or None,
        open_questions_raw=open_questions_raw or None,
        session_id=data.get("id") or "",
    )


# ---------------------------------------------------------------------------
# Meeting sections
# ---------------------------------------------------------------------------

def _meeting_sections(root: dict) -> List[Dict[str, Any]]:
    sections: List[Dict[str, Any]] = []

    # Decisions — expanded table (decision / reason / owner / due / status)
    decisions = _obj_array(root.get("decisions"))
    if decisions:
        rows = []
        for d in decisions:
            text = d.get("text") or d.get("what", "")
            reason = d.get("reason") or d.get("why", "")
            owner = d.get("owner") or d.get("assignee") or d.get("by", "")
            due = d.get("due", "")
            status = d.get("status", "")
            rows.append([text, reason, owner, due, status])
        sections.append({
            "type": "table",
            "title": "決定事項",
            "columns": ["決定事項", "理由", "担当", "期限", "状態"],
            "rows": rows,
            "emphasis": "primary",
        })

    # Todos — expanded table with priority / blocking
    todos = _obj_array(root.get("todos"))
    if todos:
        rows = []
        for t in todos:
            text = t.get("text") or t.get("task", "")
            owner = t.get("owner") or t.get("assignee") or t.get("by", "")
            due = t.get("due", "")
            priority = t.get("priority", "")
            blocking = t.get("blocking", "")
            rows.append([text, owner, due, priority, blocking])
        sections.append({
            "type": "table",
            "title": "TODO / アクション",
            "columns": ["タスク", "担当", "期限", "優先度", "ブロッカー"],
            "rows": rows,
            "emphasis": "success",
        })

    # Open questions — rich cards with impact / whyOpen / nextCheck
    oq = _obj_array(root.get("openQuestions"))
    if oq:
        cards = []
        for q in oq:
            cards.append({
                "title": q.get("text", ""),
                "fields": [
                    ("影響", q.get("impact", "")),
                    ("未解決の理由", q.get("whyOpen", "")),
                    ("担当", q.get("owner", "")),
                    ("次の確認", q.get("nextCheck", "")),
                ],
            })
        sections.append({
            "type": "cards",
            "title": "未決事項 / 要確認",
            "cards": cards,
            "emphasis": "warning",
        })
    else:
        # Some models return openQuestions as plain strings
        oq_text = _text_array(root.get("openQuestions"))
        if oq_text:
            sections.append({
                "type": "bullets",
                "title": "未決事項 / 要確認",
                "items": oq_text,
                "emphasis": "warning",
            })

    # Decision log — structured history
    dlog = _obj_array(root.get("decisionLog"))
    if dlog:
        rows = []
        for d in dlog:
            rows.append([
                d.get("topic", ""),
                d.get("conclusion", ""),
                d.get("reason", ""),
                d.get("remainingIssues", ""),
            ])
        sections.append({
            "type": "table",
            "title": "決定ログ",
            "columns": ["議題", "結論", "理由", "残課題"],
            "rows": rows,
            "emphasis": "primary",
        })

    # Discussion points — cards with conclusion/next-action
    dps = _obj_array(root.get("discussionPoints"))
    if dps:
        cards = []
        for dp in dps:
            pt = dp.get("point") or dp.get("topic") or dp.get("text", "")
            cards.append({
                "title": pt,
                "fields": [
                    ("結論", dp.get("conclusion") or dp.get("summary", "")),
                    ("次アクション", dp.get("nextAction") or dp.get("next", "")),
                ],
            })
        sections.append({
            "type": "cards",
            "title": "議論のポイント",
            "cards": cards,
            "emphasis": "primary",
        })

    # Context notes — background/context cards
    ctx = _obj_array(root.get("contextNotes"))
    if ctx:
        cards = []
        for c in ctx:
            cards.append({
                "title": c.get("topic", ""),
                "fields": [("", c.get("summary", ""))],
            })
        sections.append({
            "type": "cards",
            "title": "背景メモ",
            "cards": cards,
            "emphasis": "neutral",
        })

    return sections


# ---------------------------------------------------------------------------
# Lecture sections
# ---------------------------------------------------------------------------

def _lecture_sections(root: dict) -> List[Dict[str, Any]]:
    sections: List[Dict[str, Any]] = []

    # Theme
    theme = _extract_text_or_obj(root.get("theme"))
    if theme:
        sections.append({
            "type": "text",
            "title": "今日のテーマ",
            "body": theme,
            "emphasis": "primary",
        })

    # Terms — 3-column table with examples
    terms = _obj_array(root.get("terms")) or _obj_array(root.get("concepts"))
    if terms:
        rows = []
        for t in terms:
            term = t.get("term") or t.get("text", "")
            definition = t.get("definition", "")
            examples = _flex_text_array(t.get("examples"))
            examples_text = "\n".join(f"・{e}" for e in examples) if examples else ""
            rows.append([term, definition, examples_text])
        sections.append({
            "type": "table",
            "title": "用語・概念",
            "columns": ["用語", "定義", "例"],
            "rows": rows,
            "emphasis": "primary",
        })

    # Sections — each becomes a rich bullet block with categorized items
    for sec in _obj_array(root.get("sections")):
        sec_title = sec.get("title") or sec.get("text", "")
        items = _flex_text_array(sec.get("bullets"))
        mistakes = _flex_text_array(sec.get("commonMistakes") or sec.get("pitfalls"))
        examples = _flex_text_array(sec.get("examples"))
        grouped: List[Dict[str, Any]] = []
        if items:
            grouped.append({"label": None, "items": items})
        if examples:
            grouped.append({"label": "例", "items": examples})
        if mistakes:
            grouped.append({"label": "よくある間違い", "items": mistakes})
        if grouped:
            sections.append({
                "type": "grouped_bullets",
                "title": sec_title,
                "groups": grouped,
                "emphasis": "neutral",
            })

    # Formulas
    formulas = root.get("formulasOrProcedures") or root.get("formulas")
    formula_list = _obj_array(formulas) if isinstance(formulas, list) else []
    if not formula_list and formulas:
        formula_list = [{"content": t} for t in _text_array(formulas)]
    if formula_list:
        cards = []
        for f in formula_list:
            cards.append({
                "title": f.get("title", "") or "手順",
                "fields": [("", f.get("content", ""))],
            })
        sections.append({
            "type": "cards",
            "title": "式・手順",
            "cards": cards,
            "emphasis": "primary",
        })

    # Exercises
    ex = root.get("exercises")
    if isinstance(ex, dict):
        grouped = []
        examples = _flex_text_array(ex.get("examples"))
        if examples:
            grouped.append({"label": "例題", "items": examples})
        hw = _flex_text_array(ex.get("homework"))
        if hw:
            grouped.append({"label": "宿題", "items": hw})
        scope = _flex_text_array(ex.get("examScope"))
        if scope:
            grouped.append({"label": "試験範囲", "items": scope})
        if grouped:
            sections.append({
                "type": "grouped_bullets",
                "title": "演習 / 宿題 / 試験範囲",
                "groups": grouped,
                "emphasis": "success",
            })

    return sections


# ---------------------------------------------------------------------------
# JSON helpers (mirror SummaryTab extraction logic)
# ---------------------------------------------------------------------------

def _highlights_rich(val) -> List[Dict[str, Any]]:
    """Highlights preserved as dicts with text + category + needConfirm."""
    if val is None:
        return []
    if isinstance(val, str) and val:
        return [{"text": val, "category": None, "needConfirm": False}]
    if isinstance(val, list):
        out = []
        for item in val:
            if isinstance(item, str) and item:
                out.append({"text": item, "category": None, "needConfirm": False})
            elif isinstance(item, dict):
                text = item.get("text") or item.get("title") or item.get("point", "")
                if text:
                    out.append({
                        "text": text,
                        "category": item.get("category") or None,
                        "needConfirm": bool(item.get("needConfirm")),
                    })
        return out
    return []


def _text_array(val) -> List[str]:
    """Extract list of strings from various JSON shapes."""
    if val is None:
        return []
    if isinstance(val, str) and val:
        return [val]
    if isinstance(val, list):
        result = []
        for item in val:
            if isinstance(item, str) and item:
                result.append(item)
            elif isinstance(item, dict):
                t = item.get("text") or item.get("title") or item.get("point", "")
                if t:
                    result.append(t)
        return result
    return []


def _flex_text_array(val) -> List[str]:
    if val is None:
        return []
    if isinstance(val, str) and val:
        return [val]
    if isinstance(val, list):
        result = []
        keys = ["text", "title", "point", "term", "definition"]
        for item in val:
            if isinstance(item, str) and item:
                result.append(item)
            elif isinstance(item, dict):
                for k in keys:
                    if item.get(k):
                        result.append(item[k])
                        break
        return result
    return []


def _obj_array(val) -> List[dict]:
    if not isinstance(val, list):
        return []
    return [item for item in val if isinstance(item, dict)]


def _extract_text_or_obj(val) -> Optional[str]:
    if isinstance(val, str) and val:
        return val
    if isinstance(val, dict):
        return val.get("text", "")
    return None


def _extract_keywords(root: dict) -> List[str]:
    kw = _text_array(root.get("keywords"))
    if not kw:
        terms = root.get("terms") or root.get("concepts")
        if isinstance(terms, list):
            kw = [t.get("term") or t.get("text", "") for t in terms if isinstance(t, dict)]
            kw = [k for k in kw if k]
    return kw


def _fmt_duration(seconds) -> str:
    try:
        total = int(float(seconds))
    except (ValueError, TypeError):
        return ""
    h = total // 3600
    m = (total % 3600) // 60
    if h > 0:
        return f"{h}時間{m}分"
    return f"{m}分"


def _safe_filename(name: str, max_len: int = 100) -> str:
    illegal = set('/\\?%*|"<>:')
    safe = "".join(c if c not in illegal else "_" for c in name)
    return safe[:max_len] if safe else "export"
