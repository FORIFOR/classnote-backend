"""Session detail projection (read model).

UI 向けに session + derived/* + jobs + members + folder + share を 1 レスポンスに
集約する read-only 層。Desktop / iOS の SessionDetailScreen が共通に消費する。

Design notes:
- projection はフロントで寄せ集めるロジックの代替。重い payload (transcript 全文 /
  summary markdown / quiz 全問) は含めず、**メタとプレビューのみ**を返す。全文は
  /v1/session-details/{id}/overview|transcript|quiz|notes で個別取得する。
- source-of-truth は `sessions/{id}/derived/*`。`sessions/{id}.summaryMarkdown` 等
  は legacy shadow copy として存在するが projection は読まない (Phase 2 で session
  doc 側を削除予定)。
- permissions は compute_permissions() を single source に一本化する。
- evidence は常に空配列にフォールバックして返す (Summary v2 migration Phase 1)。
- partial failure 前提: あるセクションの読み込みが例外になっても、そのセクションを
  `status=failed` + `partials.xxx=true` に倒し、他セクションは返す。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.firebase import db
from app.services import project_folders


logger = logging.getLogger(__name__)

PROJECTION_VERSION = 1


class ProjectionError(Exception):
    pass


class NotFoundError(ProjectionError):
    pass


class ForbiddenError(ProjectionError):
    pass


# ---------------------------------------------------------------------------
# Permissions — single source of truth
# ---------------------------------------------------------------------------


def compute_permissions(session_data: Dict[str, Any], user) -> Dict[str, Any]:
    """Derive role + per-action flags from session doc and current user.

    user: app.dependencies.CurrentUser (has .uid and .account_id)
    """
    owner_uid = session_data.get("ownerUid") or session_data.get("ownerUserId")
    owner_account = session_data.get("ownerAccountId")
    shared_accounts = session_data.get("sharedWithAccountIds") or []
    shared_uids = session_data.get("sharedUserIds") or session_data.get("sharedWithUserIds") or []

    user_uid = getattr(user, "uid", None)
    user_account = getattr(user, "account_id", None)

    is_owner = bool(
        (owner_account and user_account and owner_account == user_account)
        or (owner_uid and user_uid and owner_uid == user_uid)
    )
    is_shared = bool(
        (user_account and user_account in shared_accounts)
        or (user_uid and user_uid in shared_uids)
    )
    can_view = is_owner or is_shared

    if is_owner:
        role = "owner"
    elif is_shared:
        role = "shared_viewer"
    else:
        role = "none"

    return {
        "role": role,
        "canView": can_view,
        "canEditTitle": is_owner,
        "canEditNotes": is_owner,
        "canEditTags": is_owner,
        "canMoveFolder": is_owner,
        "canShare": is_owner,
        "canDelete": is_owner,
        "canLeaveShared": role == "shared_viewer",
        "canRegenerateSummary": is_owner,
        "canRegenerateTranscript": is_owner,
        "canGenerateQuiz": is_owner,
        "canExport": can_view,
    }


# ---------------------------------------------------------------------------
# Evidence normalization (Summary v2 migration Phase 1)
# ---------------------------------------------------------------------------


def _normalize_evidence(raw: Any) -> List[Dict[str, Any]]:
    """Always return a list of EvidenceRef dicts.

    Legacy summaries may have:
      - no evidence field at all
      - evidence: null
      - evidence: "string"  (malformed)
      - evidence: [{startMs:..., endMs:...}, ...]
    """
    if not raw:
        return []
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ref: Dict[str, Any] = {}
        if "segmentId" in item and item["segmentId"] is not None:
            ref["segmentId"] = str(item["segmentId"])
        if "startMs" in item and isinstance(item["startMs"], (int, float)):
            ref["startMs"] = int(item["startMs"])
        if "endMs" in item and isinstance(item["endMs"], (int, float)):
            ref["endMs"] = int(item["endMs"])
        qp = item.get("quotePreview") or item.get("quote")
        if qp:
            ref["quotePreview"] = str(qp)[:200]
        if ref:
            out.append(ref)
    return out


def normalize_summary_payload(payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Normalize a SummaryV2 payload so every bullet carries evidence: [] minimum.

    Safe to call with legacy data; returns None if payload is missing.
    """
    if not isinstance(payload, dict):
        return None

    result = dict(payload)
    result.setdefault("schemaVersion", 2)

    def _normalize_list(key: str):
        items = result.get(key)
        if not isinstance(items, list):
            result[key] = []
            return
        normalized: List[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            it = dict(it)
            it["evidence"] = _normalize_evidence(it.get("evidence"))
            normalized.append(it)
        result[key] = normalized

    for key in (
        "keyPoints",
        "decisions",
        "todos",
        "openQuestions",
        "discussionPoints",
        "terms",
        "formulas",
        "contextNotes",
        "decisionLog",
    ):
        _normalize_list(key)

    # Phase 7.10: surface citation fields the llm hydrator populated
    # (sourceSegmentIds, segmentId, startSec/endSec, sourceCount) on
    # every bullet list. These are additive — existing `evidence: []`
    # behavior is preserved for clients that already consume it.
    for key in (
        "highlights",
        "keyPoints",
        "decisions",
        "todos",
        "openQuestions",
        "discussionPoints",
        "conversationHighlights",
    ):
        items = result.get(key)
        if not isinstance(items, list):
            continue
        normalized_citations: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            # Keep the existing shape; just make sure citation fields are
            # present and typed when the llm/anchor step put them there.
            cit_fields: Dict[str, Any] = {}
            src_ids = item.get("sourceSegmentIds")
            if isinstance(src_ids, list) and src_ids:
                cit_fields["sourceSegmentIds"] = [str(x) for x in src_ids]
            seg_id = item.get("segmentId")
            if isinstance(seg_id, (str, int)) and str(seg_id):
                cit_fields["segmentId"] = str(seg_id)
            for ms_key in ("startMs", "endMs"):
                v = item.get(ms_key)
                if isinstance(v, (int, float)):
                    cit_fields[ms_key] = int(v)
            for sec_key in ("startSec", "endSec"):
                v = item.get(sec_key)
                if isinstance(v, (int, float)):
                    cit_fields[sec_key] = round(float(v), 2)
            if "startMs" in cit_fields and "startSec" not in cit_fields:
                cit_fields["startSec"] = round(cit_fields["startMs"] / 1000.0, 2)
            if "endMs" in cit_fields and "endSec" not in cit_fields:
                cit_fields["endSec"] = round(cit_fields["endMs"] / 1000.0, 2)
            source_count = item.get("sourceCount")
            if isinstance(source_count, int):
                cit_fields["sourceCount"] = source_count
            item.update(cit_fields)
            normalized_citations.append(item)
        result[key] = normalized_citations

    # sections have nested bullets
    sections = result.get("sections")
    if isinstance(sections, list):
        normalized_sections = []
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            sec = dict(sec)
            bullets = sec.get("bullets") or []
            normalized_bullets = []
            for b in bullets:
                if not isinstance(b, dict):
                    continue
                b = dict(b)
                b["evidence"] = _normalize_evidence(b.get("evidence"))
                normalized_bullets.append(b)
            sec["bullets"] = normalized_bullets
            normalized_sections.append(sec)
        result["sections"] = normalized_sections
    else:
        result["sections"] = []

    # Ensure required top-level arrays are present
    for key in ("tldr", "keywords", "participants"):
        if not isinstance(result.get(key), list):
            result[key] = []

    # Phase 7.9: conversationHighlights normalization (natural-sentence cards
    # with a single primaryTimestampMs). Evidence always an array, importance
    # coerced to known enum, id auto-filled if missing.
    conv_highlights = result.get("conversationHighlights")
    if isinstance(conv_highlights, list):
        normalized_highlights: List[Dict[str, Any]] = []
        for idx, item in enumerate(conv_highlights):
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            importance = item.get("importance")
            if importance not in ("high", "medium", "low"):
                importance = "medium"
            hl: Dict[str, Any] = {
                "id": str(item.get("id") or f"hl_{idx + 1}"),
                "text": text.strip(),
                "importance": importance,
                "evidence": _normalize_evidence(item.get("evidence")),
            }
            if item.get("topic"):
                hl["topic"] = str(item["topic"]).strip()[:60]
            ts_ms = item.get("primaryTimestampMs")
            if isinstance(ts_ms, (int, float)) and ts_ms >= 0:
                hl["primaryTimestampMs"] = int(ts_ms)
            if isinstance(item.get("segmentIds"), list):
                hl["segmentIds"] = [str(s) for s in item["segmentIds"] if s]
            if item.get("evidenceHint"):
                hl["evidenceHint"] = str(item["evidenceHint"])[:80]
            normalized_highlights.append(hl)
        result["conversationHighlights"] = normalized_highlights
    else:
        result["conversationHighlights"] = []

    return result


# ---------------------------------------------------------------------------
# Projection builder
# ---------------------------------------------------------------------------


@dataclass
class ProjectionContext:
    user: Any  # CurrentUser
    session_id: str


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    # Firestore DatetimeWithNanoseconds / Timestamp
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return None
    return None


def _safe_get(ref):
    """Call .get() on a Firestore ref and swallow errors. Returns (snap|None, err|None)."""
    try:
        return ref.get(), None
    except Exception as e:  # pragma: no cover
        logger.warning(f"[projection] Firestore read failed for {ref.path}: {e}")
        return None, e


def _build_header(session: Dict[str, Any], folder_info: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "title": session.get("title") or "",
        "mode": session.get("mode") or "meeting",
        "lifecycleState": session.get("lifecycleState") or _infer_lifecycle(session),
        "folderId": session.get("folderId"),
        "folderName": (folder_info or {}).get("name"),
        "createdAt": _iso(session.get("createdAt")),
        "startedAt": _iso(session.get("startedAt") or session.get("startAt")),
        "endedAt": _iso(session.get("endedAt") or session.get("endAt")),
        "durationSec": int(session.get("durationSec") or 0),
        "tags": session.get("tags") or [],
        "autoTags": session.get("autoTags") or [],
    }


def _infer_lifecycle(session: Dict[str, Any]) -> str:
    """Map legacy Japanese `status` / `audioStatus` into english lifecycleState."""
    status = (session.get("status") or "").strip()
    if status in ("録音中",):
        return "recording"
    if status in ("処理中", "テスト生成"):
        return "processing"
    if status in ("録音済み", "要約済み", "テスト完了", "final"):
        return "finalized"
    if status in ("failed", "error"):
        return "error"
    audio = session.get("audioStatus")
    if audio == "pending" or audio == "uploading":
        return "recording"
    if audio in ("processing", "transcribing"):
        return "processing"
    if audio in ("completed", "uploaded"):
        return "finalized"
    return "processing"


def _artifact_status(snap_data: Optional[Dict[str, Any]], fallback: str = "idle") -> str:
    if not snap_data:
        return fallback
    s = snap_data.get("status")
    if s in ("succeeded", "completed"):
        return "completed"
    if s == "running":
        return "processing"
    if s == "failed":
        return "failed"
    if s == "locked":
        return "failed"
    if s == "pending":
        return "processing"
    return fallback


def _build_context(session: Dict[str, Any], summary: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    topic_summary = session.get("topicSummary")
    suggested_title = session.get("suggestedTitle")
    if summary:
        res = (summary.get("result") or {})
        topic_summary = topic_summary or res.get("topicSummary")
        suggested_title = suggested_title or res.get("suggestedTitle")
    participants = []
    payload = ((summary or {}).get("result") or {}).get("json") or {}
    if isinstance(payload, dict):
        participants = payload.get("participants") or []
    return {
        "topicSummary": topic_summary,
        "suggestedTitle": suggested_title,
        "participants": participants if isinstance(participants, list) else [],
    }


def _build_audio(session: Dict[str, Any]) -> Dict[str, Any]:
    audio_status = (session.get("audioStatus") or "missing").lower()
    status_map = {
        "pending": "processing",
        "uploading": "processing",
        "processing": "processing",
        "transcribing": "processing",
        "uploaded": "completed",
        "completed": "completed",
        "failed": "failed",
        "missing": "missing",
    }
    meta = session.get("audioMeta") or {}
    return {
        "status": status_map.get(audio_status, "missing"),
        "hasAudio": bool(session.get("audioStatus") and session.get("audioStatus") != "missing"),
        "sizeBytes": int(meta.get("sizeBytes") or meta.get("size") or 0),
        "durationSec": int(meta.get("durationSec") or session.get("durationSec") or 0),
        "codec": meta.get("codec"),
        "transcriptionMode": session.get("transcriptionMode"),
    }


def _build_overview(session: Dict[str, Any], summary: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    status = _artifact_status(summary)
    result = (summary or {}).get("result") or {}
    payload = result.get("json")
    normalized = normalize_summary_payload(payload) if isinstance(payload, dict) else None

    preview_bullets: List[Dict[str, Any]] = []
    if normalized:
        source = normalized.get("keyPoints") or normalized.get("tldr") or []
        for item in source[:3]:
            if isinstance(item, dict) and item.get("text"):
                preview_bullets.append({"text": item["text"], "evidence": item.get("evidence", [])})
            elif isinstance(item, str):
                preview_bullets.append({"text": item, "evidence": []})

    meta = (summary or {}).get("meta") or {}
    return {
        "status": status,
        "schemaVersion": int(meta.get("schemaVersion") or (normalized.get("schemaVersion") if normalized else 2) or 2),
        "type": meta.get("type") or session.get("mode") or "meeting",
        "hasPayload": bool(normalized),
        "previewBullets": preview_bullets,
        "lastGeneratedAt": _iso((summary or {}).get("updatedAt")),
        "sourceTranscriptVersion": int(session.get("transcriptVersion") or 1),
        "updatedAt": _iso((summary or {}).get("updatedAt")),
    }


def _build_transcript(session: Dict[str, Any], chunks_count: int) -> Dict[str, Any]:
    t_status = session.get("transcriptionStatus") or (
        "completed" if session.get("transcriptText") or chunks_count else "idle"
    )
    status_map = {
        "idle": "idle",
        "pending": "processing",
        "running": "processing",
        "processing": "processing",
        "completed": "completed",
        "failed": "failed",
    }
    return {
        "status": status_map.get(t_status, "idle"),
        "available": bool(chunks_count) or bool(session.get("transcriptText")),
        "chunkCount": chunks_count,
        "segmentCount": int(session.get("segmentCount") or 0),
        "durationSec": int(session.get("durationSec") or 0),
        "source": session.get("transcriptSource") or session.get("transcriptionMode"),
        "version": int(session.get("transcriptVersion") or 1),
        "lastGeneratedAt": _iso(session.get("transcriptUpdatedAt")),
    }


def _build_notes(session: Dict[str, Any]) -> Dict[str, Any]:
    notes = session.get("notes") or ""
    excerpt = notes[:140] if isinstance(notes, str) and notes else None
    return {
        "status": "completed" if notes else "idle",
        "hasNotes": bool(notes),
        "excerpt": excerpt,
        "updatedAt": _iso(session.get("notesUpdatedAt") or session.get("updatedAt")) if notes else None,
    }


def _build_quiz(
    session: Dict[str, Any],
    quiz: Optional[Dict[str, Any]],
    attempt_count: int,
    best_score: Optional[int],
) -> Dict[str, Any]:
    status = _artifact_status(quiz)
    result = (quiz or {}).get("result") or {}
    questions = 0
    if isinstance(result.get("json"), dict):
        qlist = result["json"].get("questions") or []
        questions = len(qlist)
    elif isinstance(result.get("count"), int):
        questions = result["count"]
    return {
        "status": status,
        "questionCount": questions,
        "hasAttempts": attempt_count > 0,
        "bestScore": best_score,
        "version": int((result.get("json") or {}).get("version") or 1) if isinstance(result.get("json"), dict) else 1,
        "lastGeneratedAt": _iso((quiz or {}).get("updatedAt")),
        "sourceTranscriptVersion": int(session.get("transcriptVersion") or 1),
    }


def _build_playlist(playlist: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    status = _artifact_status(playlist)
    result = (playlist or {}).get("result") or {}
    items = result.get("items") or []
    return {
        "status": status,
        "itemCount": len(items) if isinstance(items, list) else 0,
        "lastGeneratedAt": _iso((playlist or {}).get("updatedAt")),
    }


def _job_summary(doc) -> Dict[str, Any]:
    d = doc.to_dict() or {}
    return {
        "jobId": doc.id,
        "type": d.get("type") or d.get("jobType") or "unknown",
        "status": d.get("status") or "unknown",
        "progress": float(d.get("progress") or 0.0),
        "errorCode": d.get("errorCode") or d.get("errorReason"),
        "startedAt": _iso(d.get("createdAt") or d.get("startedAt")),
        "updatedAt": _iso(d.get("updatedAt")),
    }


def _build_share(session: Dict[str, Any], member_count: int) -> Dict[str, Any]:
    share = session.get("share") or {}
    return {
        "hasActiveLink": bool(share.get("token") and not share.get("revokedAt")),
        "linkRole": share.get("role"),
        "expiresAt": _iso(share.get("expiresAt")),
        "memberCount": member_count,
    }


def _build_ui_hints(
    overview: Dict[str, Any],
    quiz: Dict[str, Any],
    transcript: Dict[str, Any],
    permissions: Dict[str, Any],
) -> Dict[str, Any]:
    primary = None
    if permissions["canRegenerateSummary"] and overview["status"] in ("idle", "failed"):
        primary = "regenerate_summary"
    elif permissions["canGenerateQuiz"] and quiz["status"] == "idle":
        primary = "generate_quiz"
    elif permissions["canShare"] and overview["status"] == "completed":
        primary = "share"
    overview_outdated = (
        overview.get("status") == "completed"
        and transcript.get("version", 1) > (overview.get("sourceTranscriptVersion") or 1)
    )
    quiz_outdated = (
        quiz.get("status") == "completed"
        and transcript.get("version", 1) > (quiz.get("sourceTranscriptVersion") or 1)
    )
    return {
        "primaryCta": primary,
        "showSummaryOutdatedBadge": overview_outdated,
        "showQuizOutdatedBadge": quiz_outdated,
        "showNoTranscriptState": not transcript.get("available"),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def build_session_detail(ctx: ProjectionContext) -> Dict[str, Any]:
    session_ref = db.collection("sessions").document(ctx.session_id)

    # 1. core session doc
    session_snap, err = await asyncio.to_thread(_safe_get, session_ref)
    if err:
        raise ProjectionError(f"session read failed: {err}")
    if not session_snap or not session_snap.exists:
        raise NotFoundError("session not found")
    session = session_snap.to_dict() or {}

    # 2. permissions gate
    perms = compute_permissions(session, ctx.user)
    if not perms["canView"]:
        raise ForbiddenError("permission denied")

    partials: Dict[str, bool] = {}

    # 3. parallel fetch derived + members + jobs + folder meta
    derived = session_ref.collection("derived")
    jobs_ref = session_ref.collection("jobs")
    members_ref = session_ref.collection("members")
    chunks_ref = session_ref.collection("transcript_chunks")
    attempts_ref = session_ref.collection("quiz_attempts")

    def _list_stream(ref, **kw):
        try:
            if kw:
                q = ref
                for op, val in kw.items():
                    if op == "limit":
                        q = q.limit(val)
                    elif op == "order_by_desc":
                        q = q.order_by(val, direction="DESCENDING")
                    elif op == "where":
                        q = q.where(*val)
                return list(q.stream())
            return list(ref.stream())
        except Exception as e:
            logger.warning(f"[projection] list failed for {ref.path if hasattr(ref, 'path') else ref}: {e}")
            return None

    (
        summary_snap_result,
        quiz_snap_result,
        playlist_snap_result,
        members_docs,
        chunks_count,
        attempts_docs,
    ) = await asyncio.gather(
        asyncio.to_thread(_safe_get, derived.document("summary")),
        asyncio.to_thread(_safe_get, derived.document("quiz")),
        asyncio.to_thread(_safe_get, derived.document("playlist")),
        asyncio.to_thread(_list_stream, members_ref),
        asyncio.to_thread(
            lambda: sum(1 for _ in chunks_ref.select([]).stream())
            if hasattr(chunks_ref, "select")
            else len(_list_stream(chunks_ref) or [])
        ),
        asyncio.to_thread(_list_stream, attempts_ref, limit=10),
    )

    summary_snap, err = summary_snap_result
    partials["overview"] = err is not None
    summary = (summary_snap.to_dict() if summary_snap and summary_snap.exists else None) if not err else None

    quiz_snap, err = quiz_snap_result
    partials["quiz"] = err is not None
    quiz = (quiz_snap.to_dict() if quiz_snap and quiz_snap.exists else None) if not err else None

    playlist_snap, err = playlist_snap_result
    partials["playlist"] = err is not None
    playlist = (playlist_snap.to_dict() if playlist_snap and playlist_snap.exists else None) if not err else None

    if members_docs is None:
        members_docs = []
    if chunks_count is None:
        partials["transcript"] = True
        chunks_count = 0

    # best quiz score
    best_score: Optional[int] = None
    if attempts_docs:
        scores = []
        for d in attempts_docs:
            dd = d.to_dict() or {}
            s = dd.get("score")
            if isinstance(s, (int, float)):
                scores.append(int(s))
        if scores:
            best_score = max(scores)

    # active / recent jobs
    active_jobs = (
        _list_stream(
            jobs_ref,
            where=("status", "in", ["queued", "running", "pending"]),
            limit=5,
        )
        or []
    )
    recent_jobs = (
        _list_stream(jobs_ref, order_by_desc="updatedAt", limit=10) or []
    )

    # folder meta (account-aware)
    folder_info: Optional[Dict[str, Any]] = None
    folder_id = session.get("folderId")
    if folder_id:
        try:
            owner_uid = project_folders.find_folder_owner_uid(
                ctx.user.uid, ctx.user.account_id, folder_id
            )
            if owner_uid:
                fsnap = project_folders.folder_ref(owner_uid, folder_id).get()
                if fsnap.exists:
                    folder_info = fsnap.to_dict() or {}
        except Exception as e:
            logger.warning(f"[projection] folder read failed: {e}")

    # 4. assemble
    header = _build_header(session, folder_info)
    context = _build_context(session, summary)
    audio = _build_audio(session)
    overview = _build_overview(session, summary)
    transcript = _build_transcript(session, chunks_count)
    notes = _build_notes(session)
    quiz_out = _build_quiz(session, quiz, len(attempts_docs or []), best_score)
    playlist_out = _build_playlist(playlist)
    share = _build_share(session, len(members_docs or []))
    ui_hints = _build_ui_hints(overview, quiz_out, transcript, perms)

    # revision fallback: derive a string from updatedAt timestamp if no explicit field
    revision_val = session.get("revision")
    if revision_val is None:
        upd = session.get("updatedAt")
        revision_val = str(int(upd.timestamp() * 1000)) if hasattr(upd, "timestamp") else "0"
    else:
        revision_val = str(revision_val)

    return {
        "sessionId": ctx.session_id,
        "projectionVersion": PROJECTION_VERSION,
        "revision": revision_val,
        "updatedAt": _iso(session.get("updatedAt")) or _iso(datetime.now(timezone.utc)),
        "partials": partials,
        "header": header,
        "context": context,
        "audio": audio,
        "overview": overview,
        "transcript": transcript,
        "notes": notes,
        "quiz": quiz_out,
        "playlist": playlist_out,
        "permissions": perms,
        "jobs": {
            "active": [_job_summary(d) for d in active_jobs],
            "recent": [_job_summary(d) for d in recent_jobs],
        },
        "share": share,
        "uiHints": ui_hints,
    }


# ---------------------------------------------------------------------------
# Per-tab full fetches (heavy payloads)
# ---------------------------------------------------------------------------


async def fetch_overview_full(ctx: ProjectionContext) -> Dict[str, Any]:
    session_ref = db.collection("sessions").document(ctx.session_id)
    session_snap, err = await asyncio.to_thread(_safe_get, session_ref)
    if err or not session_snap or not session_snap.exists:
        raise NotFoundError("session not found")
    session = session_snap.to_dict() or {}
    if not compute_permissions(session, ctx.user)["canView"]:
        raise ForbiddenError("permission denied")

    snap, err = await asyncio.to_thread(
        _safe_get, session_ref.collection("derived").document("summary")
    )
    if err:
        raise ProjectionError(f"summary read failed: {err}")
    if not snap or not snap.exists:
        return {
            "sessionId": ctx.session_id,
            "status": "idle",
            "payload": None,
            "markdown": None,
            "schemaVersion": 2,
            "updatedAt": None,
        }
    data = snap.to_dict() or {}
    result = data.get("result") or {}
    payload = normalize_summary_payload(result.get("json"))
    return {
        "sessionId": ctx.session_id,
        "status": _artifact_status(data),
        "payload": payload,
        "markdown": result.get("markdown"),
        "schemaVersion": (data.get("meta") or {}).get("schemaVersion") or 2,
        "updatedAt": _iso(data.get("updatedAt")),
        "revision": str(session.get("revision") or ""),
    }


async def fetch_quiz_full(ctx: ProjectionContext) -> Dict[str, Any]:
    session_ref = db.collection("sessions").document(ctx.session_id)
    session_snap, err = await asyncio.to_thread(_safe_get, session_ref)
    if err or not session_snap or not session_snap.exists:
        raise NotFoundError("session not found")
    session = session_snap.to_dict() or {}
    if not compute_permissions(session, ctx.user)["canView"]:
        raise ForbiddenError("permission denied")

    snap, err = await asyncio.to_thread(
        _safe_get, session_ref.collection("derived").document("quiz")
    )
    if err:
        raise ProjectionError(f"quiz read failed: {err}")
    if not snap or not snap.exists:
        return {
            "sessionId": ctx.session_id,
            "status": "idle",
            "questions": [],
            "updatedAt": None,
        }
    data = snap.to_dict() or {}
    result = data.get("result") or {}
    payload = result.get("json")
    if not isinstance(payload, dict):
        payload = {"questions": []}
    # normalize evidence on each question
    questions = []
    for q in payload.get("questions") or []:
        if not isinstance(q, dict):
            continue
        q = dict(q)
        q["evidence"] = _normalize_evidence(q.get("evidence"))
        questions.append(q)
    return {
        "sessionId": ctx.session_id,
        "status": _artifact_status(data),
        "schemaVersion": payload.get("schemaVersion") or 1,
        "version": payload.get("version") or 1,
        "questions": questions,
        "updatedAt": _iso(data.get("updatedAt")),
    }


async def fetch_notes_full(ctx: ProjectionContext) -> Dict[str, Any]:
    session_ref = db.collection("sessions").document(ctx.session_id)
    session_snap, err = await asyncio.to_thread(_safe_get, session_ref)
    if err or not session_snap or not session_snap.exists:
        raise NotFoundError("session not found")
    session = session_snap.to_dict() or {}
    if not compute_permissions(session, ctx.user)["canView"]:
        raise ForbiddenError("permission denied")
    return {
        "sessionId": ctx.session_id,
        "markdown": session.get("notes") or "",
        "updatedAt": _iso(session.get("notesUpdatedAt") or session.get("updatedAt")),
        "revision": str(session.get("revision") or ""),
    }


async def fetch_transcript_slice(
    ctx: ProjectionContext, limit: int = 200, cursor: Optional[int] = None
) -> Dict[str, Any]:
    session_ref = db.collection("sessions").document(ctx.session_id)
    session_snap, err = await asyncio.to_thread(_safe_get, session_ref)
    if err or not session_snap or not session_snap.exists:
        raise NotFoundError("session not found")
    session = session_snap.to_dict() or {}
    if not compute_permissions(session, ctx.user)["canView"]:
        raise ForbiddenError("permission denied")

    chunks_ref = session_ref.collection("transcript_chunks")

    def _load():
        q = chunks_ref.order_by("index")
        if cursor is not None:
            q = q.start_after({"index": cursor})
        q = q.limit(max(1, min(limit, 500)))
        return list(q.stream())

    try:
        docs = await asyncio.to_thread(_load)
    except Exception as e:
        logger.warning(f"[projection] transcript chunks read failed: {e}")
        docs = []

    chunks: List[Dict[str, Any]] = []
    next_cursor: Optional[int] = None
    for d in docs:
        dd = d.to_dict() or {}
        chunks.append(
            {
                "index": int(dd.get("index") or 0),
                "startMs": int(dd.get("startMs") or 0),
                "endMs": int(dd.get("endMs") or 0),
                "text": dd.get("text") or "",
                "speaker": dd.get("speaker"),
                "segmentIds": dd.get("segmentIds") or [],
            }
        )
        if "index" in dd:
            next_cursor = int(dd["index"])

    return {
        "sessionId": ctx.session_id,
        "chunks": chunks,
        "nextCursor": next_cursor if len(chunks) == limit else None,
        "version": int(session.get("transcriptVersion") or 1),
    }
