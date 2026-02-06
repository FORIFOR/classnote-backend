"""
todo_extractor.py - TODO Extraction Service (Idempotent Design)

Extracts TODOs from session transcripts and summaries using a 3-stage pipeline:
1. Generate: LLM extracts candidates (high recall)
2. Normalize: Resolve dates, clean titles, deduplicate
3. Reconcile: Differential update - upsert, archive, create candidates

Design Principles:
- Idempotent: Same input produces same output, safe to retry
- Differential: Only update what changed
- User-Respecting: Never overwrite userEdited/userMoved items
- sourceKey: Unique per summary version for idempotency
- semanticKey: Unique per TODO content for deduplication
"""

import json
import hashlib
import logging
import re
from datetime import datetime, timezone, date, timedelta
from typing import List, Dict, Optional, Tuple, Any

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.firebase import db
from app.util_models import (
    TodoStatus,
    TodoPriority,
    TodoCandidateStatus,
    TodoSourceType,
)

logger = logging.getLogger("app.todo_extractor")

# Confidence threshold for auto-confirmation
AUTO_CONFIRM_THRESHOLD = 0.8

# Extractor version for tracking
EXTRACTOR_VERSION = "todo_v2"

# Action verbs that indicate a TODO (Japanese) - Meeting
ACTION_VERBS_JA = [
    "確認する", "確認", "送る", "送付", "連絡", "調整", "作成", "準備",
    "検討", "決める", "決定", "報告", "共有", "対応", "修正", "更新",
    "追加", "削除", "設定", "実装", "テスト", "レビュー", "依頼",
    "手配", "予約", "発注", "提出", "完了", "フォロー", "フォローアップ",
]

# Action verbs (English) - Meeting
ACTION_VERBS_EN = [
    "confirm", "send", "contact", "schedule", "create", "prepare",
    "review", "decide", "report", "share", "fix", "update", "add",
    "delete", "setup", "implement", "test", "request", "book",
    "order", "submit", "complete", "follow up", "check",
]

# Action verbs for lectures (Japanese)
LECTURE_ACTION_VERBS_JA = [
    "復習", "予習", "読む", "読んでおく", "解く", "提出", "勉強",
    "覚える", "暗記", "理解", "確認", "調べる", "まとめる", "練習",
    "準備", "宿題", "課題", "レポート", "発表", "プレゼン",
]

# Action verbs for lectures (English)
LECTURE_ACTION_VERBS_EN = [
    "review", "study", "read", "solve", "submit", "memorize",
    "understand", "research", "summarize", "practice", "prepare",
    "homework", "assignment", "report", "presentation",
]


def _generate_source_key(session_id: str, artifact_hash: str) -> str:
    """
    Generate a sourceKey for idempotency.
    Same summary version -> same sourceKey -> same TODO updates.
    """
    return f"session:{session_id}:artifact:summary:{artifact_hash}"


def _generate_semantic_key(title: str, due_date: Optional[str] = None) -> str:
    """
    Generate a semantic key for deduplication.
    Normalized title + dueDate -> unique TODO identity.
    """
    normalized = title.lower().strip()
    # Remove common prefixes and particles (Japanese)
    normalized = re.sub(r'^(を|に|で|は|が|と|の|へ|から|まで)', '', normalized)
    # Include dueDate in key to differentiate same action on different dates
    content = f"{normalized}:{due_date or 'no-date'}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _has_action_verb(text: str, mode: str = "meeting") -> bool:
    """Check if text contains an action verb appropriate for the mode."""
    text_lower = text.lower()

    # Common verbs for all modes
    all_verbs = ACTION_VERBS_JA + ACTION_VERBS_EN

    # Add lecture-specific verbs when in lecture mode
    if mode == "lecture":
        all_verbs = all_verbs + LECTURE_ACTION_VERBS_JA + LECTURE_ACTION_VERBS_EN

    for verb in all_verbs:
        if verb in text_lower:
            return True
    return False


def _resolve_relative_date(text: str, base_date: date) -> Optional[str]:
    """
    Resolve relative date expressions to absolute dates.
    """
    text = text.strip()

    # Exact date patterns (YYYY-MM-DD)
    if re.match(r'^\d{4}-\d{2}-\d{2}$', text):
        return text

    # Japanese relative dates
    if "明日" in text:
        return (base_date + timedelta(days=1)).isoformat()
    if "明後日" in text:
        return (base_date + timedelta(days=2)).isoformat()
    if "来週" in text:
        days_until_monday = (7 - base_date.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        return (base_date + timedelta(days=days_until_monday)).isoformat()
    if "今週中" in text or "今週" in text:
        days_until_friday = (4 - base_date.weekday()) % 7
        if days_until_friday <= 0:
            days_until_friday = 0
        return (base_date + timedelta(days=days_until_friday)).isoformat()
    if "来月" in text:
        if base_date.month == 12:
            return date(base_date.year + 1, 1, 1).isoformat()
        return date(base_date.year, base_date.month + 1, 1).isoformat()
    if "月末" in text:
        if base_date.month == 12:
            return date(base_date.year, 12, 31).isoformat()
        return (date(base_date.year, base_date.month + 1, 1) - timedelta(days=1)).isoformat()

    # MM/DD or M月D日 patterns
    match = re.search(r'(\d{1,2})[/月](\d{1,2})', text)
    if match:
        month = int(match.group(1))
        day = int(match.group(2))
        try:
            candidate = date(base_date.year, month, day)
            if candidate < base_date:
                candidate = date(base_date.year + 1, month, day)
            return candidate.isoformat()
        except ValueError:
            pass

    return None


def _extract_date_from_text(text: str, base_date: date) -> Optional[str]:
    """Extract and resolve date from TODO text."""
    date_patterns = [
        r'\d{4}-\d{2}-\d{2}',
        r'\d{1,2}/\d{1,2}',
        r'\d{1,2}月\d{1,2}日',
        r'来週', r'今週', r'明日', r'明後日', r'来月', r'月末',
    ]

    for pattern in date_patterns:
        match = re.search(pattern, text)
        if match:
            resolved = _resolve_relative_date(match.group(), base_date)
            if resolved:
                return resolved

    return None


# =============================================================================
# LLM Extraction
# =============================================================================

EXTRACTION_PROMPT_MEETING = """以下の会議メモ/文字起こしから、TODOアイテム（やるべきこと）を抽出してください。

# 抽出ルール
1. 具体的なアクション（確認する、送る、作成する、調整する等）を含むものを抽出
2. 単なる議論や背景説明は除外
3. 各TODOに以下を含める：
   - title: 短い行動文（例：「代理店にOpenVLA対応状況を確認する」）
   - dueDate: 期限があれば（YYYY-MM-DD形式、相対表現も可）
   - owner: 担当者（わかれば）
   - confidence: 0.0-1.0（明確さ）
   - evidence: 根拠となる元テキスト

# 出力形式（JSON）
```json
{{
  "todos": [
    {{
      "title": "TODOの内容",
      "dueDate": "2026-02-15",
      "owner": "田中さん",
      "confidence": 0.85,
      "evidence": "「来週までに田中さんが確認する」"
    }}
  ]
}}
```

# 注意
- 確実にTODOと言えるものはconfidence >= 0.8
- 曖昧なものはconfidence 0.5-0.7
- 漏れを防ぐため、疑わしいものも低confidenceで含める

---

# 会議メモ/文字起こし:
{content}
"""

EXTRACTION_PROMPT_LECTURE = """以下の講義ノート/文字起こしから、学生がやるべきこと（TODO）を抽出してください。

# 抽出対象
以下のような内容を見逃さず抽出してください：
1. **宿題・課題**: 「○○を解いてきてください」「レポートを提出」「教科書○ページを読む」
2. **復習ポイント**: 「ここは重要なので復習しておいてください」「試験に出ます」「しっかり覚えてください」
3. **予習**: 「次回までに○○を読んでおいてください」「予習してきてください」
4. **確認事項**: 「各自確認しておいてください」「調べておいてください」
5. **提出物**: 「○日までに提出」「締め切りは○○」
6. **試験関連**: 「試験範囲は○○」「○○が出題されます」「ここはテストに出る」
7. **準備事項**: 「次回は○○を持ってきてください」「○○を準備しておいてください」

# 抽出ルール
- 教授・先生が学生に対して指示・推奨している内容を抽出
- 単なる説明や講義内容は除外
- 各TODOに以下を含める：
   - title: 短い行動文（例：「教科書第5章を復習する」「レポートを提出する」）
   - dueDate: 期限があれば（YYYY-MM-DD形式、「来週」「次回」等も可）
   - owner: 基本は空（学生全員が対象のため）
   - confidence: 0.0-1.0（明確さ）
   - evidence: 根拠となる元テキスト
   - category: homework（宿題）/ review（復習）/ preparation（準備）/ exam（試験）/ submission（提出）

# 出力形式（JSON）
```json
{{
  "todos": [
    {{
      "title": "教科書第5章の練習問題を解く",
      "dueDate": "次回の授業まで",
      "owner": "",
      "confidence": 0.9,
      "evidence": "「第5章の練習問題を解いてきてください」",
      "category": "homework"
    }},
    {{
      "title": "今日の内容を復習する（試験範囲）",
      "dueDate": "",
      "owner": "",
      "confidence": 0.85,
      "evidence": "「ここは試験に出るので復習しておいてください」",
      "category": "review"
    }}
  ]
}}
```

# 注意
- 確実にTODOと言えるものはconfidence >= 0.8
- 曖昧なものはconfidence 0.5-0.7
- 漏れを防ぐため、疑わしいものも低confidenceで含める
- 講義の要点説明は除外し、「やるべきこと」のみを抽出

---

# 講義ノート/文字起こし:
{content}
"""

# Default to meeting for backward compatibility
EXTRACTION_PROMPT = EXTRACTION_PROMPT_MEETING


async def _llm_extract_candidates(
    transcript: str,
    summary: Optional[str],
    session_title: str,
    mode: str = "meeting",
) -> List[Dict]:
    """
    Use LLM to extract TODO candidates from content.
    Returns list of raw candidates with confidence scores.

    Args:
        transcript: Transcript text
        summary: Summary text (optional)
        session_title: Session title
        mode: "meeting" or "lecture" - determines extraction prompt
    """
    from app.services.llm import _ensure_model, _model
    from vertexai.generative_models import GenerationConfig

    content_parts = []
    if summary:
        content_parts.append(f"## 要約:\n{summary}")
    if transcript:
        transcript_preview = transcript[:8000] if len(transcript) > 8000 else transcript
        content_parts.append(f"## 文字起こし:\n{transcript_preview}")

    if not content_parts:
        return []

    content = "\n\n".join(content_parts)

    # Use mode-specific prompt
    if mode == "lecture":
        prompt = EXTRACTION_PROMPT_LECTURE.format(content=content)
        logger.info(f"[todo_extractor] Using lecture extraction prompt")
    else:
        prompt = EXTRACTION_PROMPT_MEETING.format(content=content)
        logger.info(f"[todo_extractor] Using meeting extraction prompt")

    _ensure_model()

    try:
        response = await _model.generate_content_async(
            prompt,
            generation_config=GenerationConfig(
                temperature=0.3,
                max_output_tokens=2048,
                response_mime_type="application/json",
            ),
        )

        result_text = response.text or "{}"

        try:
            result = json.loads(result_text)
            todos = result.get("todos", [])
            logger.info(f"[todo_extractor] LLM extracted {len(todos)} candidates")
            return todos
        except json.JSONDecodeError as e:
            logger.warning(f"[todo_extractor] Failed to parse LLM response: {e}")
            return []

    except Exception as e:
        logger.error(f"[todo_extractor] LLM extraction failed: {e}")
        return []


# =============================================================================
# Normalization
# =============================================================================

def _normalize_candidates(
    raw_candidates: List[Dict],
    session_date: date,
    session_id: str,
    session_title: str,
    source_key: str,
    mode: str = "meeting",
) -> List[Dict]:
    """
    Normalize candidates:
    - Resolve relative dates
    - Clean titles
    - Add semantic keys and source keys
    """
    normalized = []

    for raw in raw_candidates:
        title = raw.get("title", "").strip()
        if not title:
            continue

        confidence = raw.get("confidence", 0.5)
        if not _has_action_verb(title, mode) and confidence < 0.6:
            logger.debug(f"[todo_extractor] Skipping non-actionable: {title}")
            continue

        # Resolve due date
        raw_date = raw.get("dueDate", "")
        due_date = None

        if raw_date:
            due_date = _resolve_relative_date(str(raw_date), session_date)

        if not due_date:
            due_date = _extract_date_from_text(title, session_date)

        if not due_date:
            due_date = session_date.isoformat()

        # Generate keys
        semantic_key = _generate_semantic_key(title, due_date)

        normalized.append({
            "title": title,
            "dueDate": due_date,
            "owner": raw.get("owner"),
            "confidence": confidence,
            "evidence": raw.get("evidence"),
            "semanticKey": semantic_key,
            "sourceKey": source_key,
            "sessionId": session_id,
            "sessionTitle": session_title,
        })

    return normalized


# =============================================================================
# Differential Update (Core Idempotent Logic)
# =============================================================================

def _load_existing_todos_for_session(account_id: str, session_id: str) -> Dict[str, Tuple[str, Dict]]:
    """
    Load existing TODOs created from this session.
    Returns {semanticKey: (doc_id, data)}
    """
    existing = {}

    docs = db.collection("todos").where(
        filter=FieldFilter("accountId", "==", account_id)
    ).where(
        filter=FieldFilter("source.sessionId", "==", session_id)
    ).stream()

    for doc in docs:
        data = doc.to_dict()
        dedupe = data.get("dedupe", {})
        semantic_key = dedupe.get("semanticKey")
        if semantic_key:
            existing[semantic_key] = (doc.id, data)

    return existing


def _load_rejected_keys(account_id: str) -> set:
    """Load semantic keys that user has rejected."""
    rejected = set()

    docs = db.collection("rejected_todo_keys").where(
        filter=FieldFilter("accountId", "==", account_id)
    ).stream()

    for doc in docs:
        rejected.add(doc.id)

    return rejected


def _differential_update(
    account_id: str,
    session_id: str,
    session_title: str,
    source_key: str,
    extracted: List[Dict],
    existing: Dict[str, Tuple[str, Dict]],
    rejected_keys: set,
    mode: str = "meeting",
) -> Dict[str, int]:
    """
    Perform differential update:
    1. Upsert: extracted items that exist or are new
    2. Archive: existing autoCreated items not in extracted (unless userEdited)
    3. Candidates: low confidence items for review

    Returns stats: {upserted, created, archived, candidates, skipped}
    """
    now = datetime.now(timezone.utc)
    batch = db.batch()

    # Separate high-confidence (auto) and low-confidence (candidates)
    auto_items = []
    candidate_items = []

    for item in extracted:
        semantic_key = item.get("semanticKey")
        confidence = item.get("confidence", 0.5)

        # Skip if rejected by user
        if semantic_key in rejected_keys:
            logger.debug(f"[todo_extractor] Skipping rejected: {item['title']}")
            continue

        if confidence >= AUTO_CONFIRM_THRESHOLD and _has_action_verb(item["title"], mode):
            auto_items.append(item)
        else:
            reasons = []
            if confidence < AUTO_CONFIRM_THRESHOLD:
                reasons.append(f"confidence {confidence:.2f}")
            if not _has_action_verb(item["title"], mode):
                reasons.append("no action verb")
            item["reason"] = "; ".join(reasons)
            candidate_items.append(item)

    # Build extracted map for comparison
    extracted_keys = {item["semanticKey"] for item in auto_items}

    stats = {"upserted": 0, "created": 0, "archived": 0, "candidates": 0, "skipped": 0}

    # 1. Upsert auto items
    for item in auto_items:
        semantic_key = item["semanticKey"]

        if semantic_key in existing:
            doc_id, cur = existing[semantic_key]
            origin = cur.get("origin", {})

            # Respect user edits/moves - only update sourceKey
            if origin.get("userEdited") or origin.get("userMoved"):
                batch.update(db.collection("todos").document(doc_id), {
                    "source.sourceKey": source_key,
                    "updatedAt": now,
                })
                stats["skipped"] += 1
                logger.debug(f"[todo_extractor] Preserving user-edited: {item['title']}")
            else:
                # Update existing TODO
                batch.update(db.collection("todos").document(doc_id), {
                    "title": item["title"],
                    "dueDate": item["dueDate"],
                    "source.sourceKey": source_key,
                    "source.evidence": {"quote": item.get("evidence")} if item.get("evidence") else None,
                    "origin.confidence": item["confidence"],
                    "updatedAt": now,
                })
                stats["upserted"] += 1
        else:
            # Create new TODO
            ref = db.collection("todos").document()
            batch.set(ref, {
                "accountId": account_id,
                "title": item["title"],
                "notes": None,
                "dueDate": item["dueDate"],
                "status": TodoStatus.OPEN.value,
                "priority": TodoPriority.NORMAL.value,
                "source": {
                    "sessionId": session_id,
                    "sessionTitle": session_title,
                    "sourceKey": source_key,
                    "createdFrom": TodoSourceType.MINUTES.value,
                    "evidence": {"quote": item.get("evidence")} if item.get("evidence") else None,
                },
                "origin": {
                    "extractorVersion": EXTRACTOR_VERSION,
                    "confidence": item["confidence"],
                    "autoCreated": True,
                    "userEdited": False,
                    "userMoved": False,
                },
                "dedupe": {
                    "semanticKey": semantic_key,
                    "rejectedByUser": False,
                },
                "createdAt": now,
                "updatedAt": now,
            })
            stats["created"] += 1

    # 2. Archive removed autos (only if autoCreated && not userEdited/userMoved)
    for semantic_key, (doc_id, cur) in existing.items():
        if semantic_key not in extracted_keys:
            origin = cur.get("origin", {})
            status = cur.get("status")

            # Only archive if: autoCreated, not userEdited, not already archived/done
            if (origin.get("autoCreated")
                and not origin.get("userEdited")
                and not origin.get("userMoved")
                and status == TodoStatus.OPEN.value):

                batch.update(db.collection("todos").document(doc_id), {
                    "status": TodoStatus.ARCHIVED.value,
                    "archivedReason": "removed_from_summary",
                    "updatedAt": now,
                })
                stats["archived"] += 1
                logger.debug(f"[todo_extractor] Archived removed: {cur.get('title')}")

    # 3. Create/update candidates
    for item in candidate_items:
        semantic_key = item["semanticKey"]

        # Skip if already exists as TODO
        if semantic_key in existing:
            stats["skipped"] += 1
            continue

        # Check if candidate already exists
        existing_cand = db.collection("todo_candidates").where(
            filter=FieldFilter("accountId", "==", account_id)
        ).where(
            filter=FieldFilter("semanticKey", "==", semantic_key)
        ).limit(1).stream()

        cand_exists = False
        for cand_doc in existing_cand:
            cand_exists = True
            # Update existing candidate
            batch.update(cand_doc.reference, {
                "sourceKey": source_key,
                "confidence": item["confidence"],
                "updatedAt": now,
            })
            break

        if not cand_exists:
            cand_ref = db.collection("todo_candidates").document()
            batch.set(cand_ref, {
                "accountId": account_id,
                "sessionId": session_id,
                "sessionTitle": session_title,
                "title": item["title"],
                "dueDateProposed": item["dueDate"],
                "confidence": item["confidence"],
                "reason": item.get("reason"),
                "evidence": {"quote": item.get("evidence")} if item.get("evidence") else None,
                "status": TodoCandidateStatus.PENDING.value,
                "semanticKey": semantic_key,
                "sourceKey": source_key,
                "createdAt": now,
            })
            stats["candidates"] += 1

    batch.commit()
    return stats


# =============================================================================
# Main Entry Point
# =============================================================================

async def update_todos_from_summary(
    session_id: str,
    account_id: str,
    source_key: str,
    summary_text: str,
    transcript_text: Optional[str] = None,
    mode: str = "meeting",
) -> Dict[str, Any]:
    """
    Update TODOs from a summary artifact (idempotent).

    Call this immediately after saving the summary artifact.
    Same source_key -> same result (idempotent).

    Args:
        session_id: Session to extract from
        account_id: Owner account
        source_key: Unique key for this summary version (use artifact hash)
        summary_text: Summary markdown/text
        transcript_text: Optional transcript for additional context
        mode: "meeting" or "lecture" - determines extraction prompt

    Returns:
        {"upserted": int, "created": int, "archived": int, "candidates": int}
    """
    logger.info(f"[todo_extractor] Starting update for session {session_id}, mode={mode}, sourceKey={source_key[:50]}...")

    # Load session for metadata
    session_doc = db.collection("sessions").document(session_id).get()
    if not session_doc.exists:
        raise ValueError(f"Session {session_id} not found")

    session_data = session_doc.to_dict()
    session_title = session_data.get("title", "Untitled")

    # Check if already processed with same sourceKey (idempotency)
    todo_extraction = session_data.get("todoExtraction", {})
    if todo_extraction.get("sourceKey") == source_key:
        logger.info(f"[todo_extractor] Same sourceKey already processed, skipping")
        return {
            "upserted": 0,
            "created": 0,
            "archived": 0,
            "candidates": 0,
            "skipped": 0,
            "idempotent_hit": True,
        }

    # Get session date
    created_at = session_data.get("createdAt")
    if isinstance(created_at, datetime):
        session_date = created_at.date()
    else:
        session_date = date.today()

    # Step 1: LLM extraction (mode-aware)
    raw_candidates = await _llm_extract_candidates(transcript_text, summary_text, session_title, mode)

    if not raw_candidates:
        logger.info(f"[todo_extractor] No candidates extracted for session {session_id}")
        # Update session metadata
        db.collection("sessions").document(session_id).update({
            "todoExtraction": {
                "completedAt": datetime.now(timezone.utc),
                "sourceKey": source_key,
                "extractorVersion": EXTRACTOR_VERSION,
                "candidateCount": 0,
                "createdCount": 0,
            },
            "todoUpdatedAt": datetime.now(timezone.utc),
        })
        return {
            "upserted": 0,
            "created": 0,
            "archived": 0,
            "candidates": 0,
            "skipped": 0,
        }

    # Step 2: Normalize (mode-aware)
    normalized = _normalize_candidates(
        raw_candidates,
        session_date,
        session_id,
        session_title,
        source_key,
        mode,
    )

    # Step 3: Load existing data
    existing = _load_existing_todos_for_session(account_id, session_id)
    rejected_keys = _load_rejected_keys(account_id)

    # Step 4: Differential update (mode-aware)
    stats = _differential_update(
        account_id=account_id,
        session_id=session_id,
        session_title=session_title,
        source_key=source_key,
        extracted=normalized,
        existing=existing,
        rejected_keys=rejected_keys,
        mode=mode,
    )

    # Step 5: Update session metadata
    db.collection("sessions").document(session_id).update({
        "todoExtraction": {
            "completedAt": datetime.now(timezone.utc),
            "sourceKey": source_key,
            "extractorVersion": EXTRACTOR_VERSION,
            "candidateCount": stats["candidates"],
            "createdCount": stats["created"],
            "upsertedCount": stats["upserted"],
            "archivedCount": stats["archived"],
        },
        "todoUpdatedAt": datetime.now(timezone.utc),
    })

    logger.info(
        f"[todo_extractor] Session {session_id}: "
        f"created={stats['created']}, upserted={stats['upserted']}, "
        f"archived={stats['archived']}, candidates={stats['candidates']}"
    )

    return stats


# =============================================================================
# Legacy Entry Point (for manual extraction endpoint)
# =============================================================================

async def extract_todos_from_session(
    session_id: str,
    account_id: str,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Legacy entry point for manual extraction.
    Generates sourceKey from current timestamp.
    """
    # Generate a unique sourceKey for this extraction
    timestamp = datetime.now(timezone.utc).isoformat()
    source_key = _generate_source_key(session_id, hashlib.sha256(timestamp.encode()).hexdigest()[:8])

    # Load transcript and summary
    session_doc = db.collection("sessions").document(session_id).get()
    if not session_doc.exists:
        raise ValueError(f"Session {session_id} not found")

    session_data = session_doc.to_dict()

    # Check if already extracted (unless force)
    if not force:
        todo_extraction = session_data.get("todoExtraction", {})
        if todo_extraction.get("completedAt"):
            return {
                "created_count": 0,
                "candidate_count": 0,
                "skipped_count": 0,
                "session_id": session_id,
                "already_extracted": True,
            }

    # Load summary
    summary_text = session_data.get("summaryMarkdown", "")

    # Load transcript
    transcript_text = ""
    transcript_doc = db.collection("sessions").document(session_id)\
        .collection("artifacts").document("transcript").get()
    if transcript_doc.exists:
        transcript_text = transcript_doc.to_dict().get("text", "")

    if not summary_text and not transcript_text:
        return {
            "created_count": 0,
            "candidate_count": 0,
            "skipped_count": 0,
            "session_id": session_id,
            "error": "No transcript or summary available",
        }

    # Get session mode for appropriate TODO extraction
    session_mode = session_data.get("mode", "meeting")

    # Call the idempotent update function (with mode)
    stats = await update_todos_from_summary(
        session_id=session_id,
        account_id=account_id,
        source_key=source_key,
        summary_text=summary_text,
        transcript_text=transcript_text,
        mode=session_mode,
    )

    # Convert to legacy format
    return {
        "created_count": stats.get("created", 0),
        "candidate_count": stats.get("candidates", 0),
        "skipped_count": stats.get("skipped", 0) + stats.get("archived", 0),
        "session_id": session_id,
    }
