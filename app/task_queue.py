import os
import re
import json
import logging
from datetime import datetime, timedelta
import asyncio
from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2
from fastapi import BackgroundTasks
from google.cloud import firestore
from app.firebase import db
from app.services import llm
from app.services.usage import usage_logger
from app.services.transcripts import resolve_transcript_text, get_transcript_chunks
from app.services.playlist_utils import normalize_playlist_items

# Pattern to detect auto-generated default titles (e.g. "2026/03/12 14:30", "録音 2026-03-12", "セッション 3/12")
# Also matches previously auto-generated titles like "3/12 14:30_タイトル" to allow re-generation.
_DEFAULT_TITLE_RE = re.compile(
    r"^("
    r"\d{4}[/\-]\d{1,2}[/\-]\d{1,2}"   # date-based: 2026/03/12, 2026-03-12
    r"|\d{1,2}/\d{1,2}\s+\d{2}:\d{2}_"  # auto-generated: 3/12 14:30_タイトル
    r"|録音\s*\d"                          # 録音 2026...
    r"|セッション\s*\d"                    # セッション 3/12...
    r"|Recording\s"                        # Recording ...
    r"|Session\s"                          # Session ...
    r"|新しいセッション"                   # 新しいセッション
    r"|New\s*Session"                      # New Session
    r"|YouTube取り込み"                    # YouTube import default
    r"|YouTube\s*Import"                   # YouTube Import (EN)
    r"|インポート"                         # Generic import
    r")",
    re.IGNORECASE,
)


def _is_default_title(title: str) -> bool:
    """Return True if the title looks auto-generated (timestamp-based or generic)."""
    if not title or not title.strip():
        return True
    return bool(_DEFAULT_TITLE_RE.match(title.strip()))


def _build_auto_title(suggested_title: str, session_data: dict) -> str:
    """Build auto title in format: 'M/D HH:MM_suggestedTitle'."""
    created_at = session_data.get("createdAt") or session_data.get("startAt")
    if created_at is None:
        return suggested_title

    # Firestore timestamps → datetime
    if hasattr(created_at, "isoformat"):
        # Already a datetime or Firestore DatetimeWithNanoseconds
        dt = created_at
    else:
        return suggested_title

    # Convert to JST (UTC+9)
    from datetime import timezone as tz
    jst = tz(timedelta(hours=9))
    dt_jst = dt.astimezone(jst) if dt.tzinfo else dt.replace(tzinfo=tz.utc).astimezone(jst)

    m = dt_jst.month
    d = dt_jst.day
    hh = dt_jst.strftime("%H:%M")
    return f"{m}月{d}日 {hh} — {suggested_title}"


def enqueue_cleanup_sessions_task(user_id: str, background_tasks: BackgroundTasks = None):
    """
    [TRIPLE LOCK] Enqueue cleanup task to delete old sessions if limit exceeded.
    """
    payload = {"userId": user_id}
    
    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.info(f"[Cleanup] Enqueuing local task for user: {user_id}")
        # Note: We don't have a local runner for this yet in task_queue. 
        # But we can import it inside the runner or sim. 
        # For now, just log usage since local cleanup isn't critical for dev?
        # actually we should run it.
        # Let's assume we can call the function directly if needed, but it's an API handler.
        # We can skip local execution for cleanup or mock it.
        # "cleanup is safe to auto-run".
        return

    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    url = f"{CLOUD_RUN_URL}/internal/tasks/cleanup_sessions"
    
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        }
    }
    
    try:
        _create_task_nonblocking(parent, task)
        logger.info(f"[Cleanup] Enqueued task for {user_id}")
    except Exception as e:
        logger.error(f"[Cleanup] Failed to enqueue: {e}")


logger = logging.getLogger("app.task_queue")

PROJECT_ID = os.environ.get("GCP_PROJECT", "classnote-x-dev")
LOCATION   = os.environ.get("TASKS_LOCATION", "asia-northeast1")
QUEUE_NAME = os.environ.get("SUMMARIZE_QUEUE", "summarize-queue")
CLOUD_RUN_URL = os.environ.get("CLOUD_RUN_SERVICE_URL", "http://localhost:8000")

# Cloud Tasks Client (Lazy init might be better but global for now)
try:
    tasks_client = tasks_v2.CloudTasksClient()
except Exception:
    # ローカルでクレデンシャルがない場合など
    tasks_client = None
    logger.warning("Cloud Tasks client init failed. BackgroundTasks will be used (Local Mode).")


async def _create_task_async(parent: str, task: dict) -> object:
    """[PERF] Offload sync gRPC create_task() to thread so it doesn't block the event loop."""
    return await asyncio.to_thread(tasks_client.create_task, parent=parent, task=task)


def _create_task_nonblocking(parent: str, task: dict) -> None:
    """
    [PERF] Fire-and-forget Cloud Tasks creation without blocking the event loop.
    If called from an async context, schedules in a thread. Otherwise calls sync.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # We're in an async context — offload to thread pool
        loop.run_in_executor(None, lambda: tasks_client.create_task(parent=parent, task=task))
    else:
        # Pure sync context — call directly
        tasks_client.create_task(parent=parent, task=task)

def enqueue_summarize_task(
    session_id: str,
    job_id: str | None = None,
    background_tasks: BackgroundTasks = None,
    idempotency_key: str | None = None,
    user_id: str | None = None,
    usage_reserved: bool = False,
):
    """
    要約タスクをキューに入れる。
    ローカル環境などで Client がない場合は FastAPI の BackgroundTasks にフォールバックする（デバッグ用）。
    """
    payload = {
        "sessionId": session_id,
        "jobId": job_id,
        "idempotencyKey": idempotency_key,
        "userId": user_id,
        "usageReserved": usage_reserved,
    }
    
    # 1. ローカルデバッグ (No Cloud Tasks Client or Explicit Local Mode)
    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.info(f"Enqueuing local background task for session: {session_id}")
        if background_tasks:
            # FastAPI がコルーチンを await してくれるので、そのまま渡す
            background_tasks.add_task(_run_local_summarize, session_id, job_id=job_id)
        else:
            asyncio.create_task(_run_local_summarize(session_id, job_id=job_id))
        return

    # 2. Cloud Tasks
    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    url = f"{CLOUD_RUN_URL}/internal/tasks/summarize"
    
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
            # OIDC Token 設定 (Cloud Run 間の認証用)
            # "oidc_token": {"service_account_email": ...} 
        },
        "dispatch_deadline": {"seconds": 1800},  # 30 mins timeout for LLM
    }

    try:
        _create_task_nonblocking(parent, task)
        logger.info(f"Created summarize task for {session_id}")
    except Exception as e:
        logger.error(f"Failed to create task: {e}")
        raise e

def enqueue_quiz_task(
    session_id: str,
    count: int = 8,
    job_id: str | None = None,
    idempotency_key: str | None = None,
    user_id: str | None = None,
    usage_reserved: bool = False,
    background_tasks: BackgroundTasks = None,
):
    # 同様に実装
    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.info("Running quiz task locally")
        if background_tasks:
            background_tasks.add_task(_run_local_quiz, session_id, count, job_id)
        else:
            asyncio.create_task(_run_local_quiz(session_id, count, job_id))
        return

    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    url = f"{CLOUD_RUN_URL}/internal/tasks/quiz"
    payload = {
        "sessionId": session_id,
        "count": count,
        "jobId": job_id,
        "idempotencyKey": idempotency_key,
        "userId": user_id,
        "usageReserved": usage_reserved,
    }
    
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        }
    }
    
    _create_task_nonblocking(parent, task)


def enqueue_timeline_task(
    session_id: str,
    job_id: str | None = None,
    force: bool = False,
    idempotency_key: str | None = None,
    user_id: str | None = None,
    background_tasks: BackgroundTasks = None,
):
    """Enqueue a chapter-style timeline generation task."""
    payload = {
        "sessionId": session_id,
        "jobId": job_id,
        "force": bool(force),
        "idempotencyKey": idempotency_key,
        "userId": user_id,
    }

    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.info(f"Running timeline task locally for session: {session_id}")
        if background_tasks:
            background_tasks.add_task(_run_local_timeline, session_id, job_id, bool(force))
        else:
            asyncio.create_task(_run_local_timeline(session_id, job_id, bool(force)))
        return

    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    url = f"{CLOUD_RUN_URL}/internal/tasks/timeline"
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        },
    }
    _create_task_nonblocking(parent, task)
    logger.info(f"Enqueued timeline task for session {session_id}")


async def _run_local_timeline(
    session_id: str,
    job_id: str | None = None,
    force: bool = False,
):
    """Local fallback for timeline generation (dev / USE_LOCAL_TASKS=1)."""
    from app.services.timeline_service import build_session_timeline
    from app.routes.jobs import start_job, complete_job, fail_job

    if job_id:
        if start_job(job_id) is None:
            logger.info(f"[local timeline] job {job_id} already terminal — skip")
            return
    try:
        result = await build_session_timeline(session_id, force=force)
        if job_id:
            if result.get("status") == "succeeded":
                complete_job(job_id, result_url=f"/sessions/{session_id}/artifacts/timeline")
            else:
                fail_job(job_id, error_reason=str(result.get("reason") or "failed"))
        logger.info(f"[local timeline] done session={session_id} result={result}")
    except Exception as e:
        logger.exception(f"[local timeline] failed session={session_id}")
        if job_id:
            fail_job(job_id, error_reason=str(e)[:200])


def enqueue_summarize_quick_task(
    session_id: str,
    job_id: str | None = None,
    idempotency_key: str | None = None,
    user_id: str | None = None,
):
    """
    Quick Summary タスクをキューに入れる（30-60秒で先出し要約）。
    CostGuard消費なし。dispatch_deadline短め。
    """
    payload = {
        "sessionId": session_id,
        "jobId": job_id,
        "idempotencyKey": idempotency_key,
        "userId": user_id,
    }

    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.info(f"Running quick summary task locally for session: {session_id}")
        asyncio.create_task(_run_local_summarize_quick(session_id, job_id=job_id))
        return

    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    url = f"{CLOUD_RUN_URL}/internal/tasks/summarize_quick"

    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        },
        "dispatch_deadline": {"seconds": 120},  # Quick is short
    }

    try:
        _create_task_nonblocking(parent, task)
        logger.info(f"Created quick summary task for {session_id}")
    except Exception as e:
        logger.error(f"Failed to create quick summary task: {e}")


async def _run_local_summarize_quick(session_id: str, job_id: str | None = None):
    """Local fallback for quick summary (debug only)."""
    logger.info(f"[Local] Quick summary for {session_id} (stub)")


def enqueue_quiz_batch_tasks(
    session_id: str,
    total_questions: int = 8,
    batch_size: int = 2,
    job_id: str | None = None,
    idempotency_key: str | None = None,
    user_id: str | None = None,
    usage_reserved: bool = False,
):
    """
    クイズ生成タスクを1つ投入する（シングルショット）。
    重複防止のため全問を1回のLLM呼び出しで生成する。
    """
    payload = {
        "sessionId": session_id,
        "count": total_questions,
        "jobId": job_id,
        "idempotencyKey": idempotency_key,
        "userId": user_id,
        "usageReserved": usage_reserved,
    }

    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.info(f"Running quiz locally for {session_id}")
        asyncio.create_task(_run_local_quiz(session_id, total_questions, job_id))
        return

    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    url = f"{CLOUD_RUN_URL}/internal/tasks/quiz"

    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        },
    }

    try:
        _create_task_nonblocking(parent, task)
        logger.info(f"Created quiz task for {session_id} (count={total_questions})")
    except Exception as e:
        logger.error(f"Failed to create quiz task: {e}")


def enqueue_qa_task(session_id: str, question: str, user_id: str, qa_id: str):
    """
    QA タスクをキューに入れる。
    結果は derived/qa/{qa_id} に保存される。
    """
    payload = {"sessionId": session_id, "question": question, "userId": user_id, "qaId": qa_id}
    
    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.info(f"Running QA task locally for session: {session_id}")
        asyncio.create_task(_run_local_qa(session_id, question, user_id, qa_id))
        return

    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    url = f"{CLOUD_RUN_URL}/internal/tasks/qa"

    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        },
        "dispatch_deadline": {"seconds": 300},  # 5 mins for QA
    }

    _create_task_nonblocking(parent, task)
    logger.info(f"Enqueued QA task for session {session_id}, qaId: {qa_id}")

def enqueue_translate_task(session_id: str, target_language: str, user_id: str):
    """
    翻訳タスクをキューに入れる。
    結果は translations/{session_id} に保存される。
    """
    payload = {"sessionId": session_id, "targetLanguage": target_language, "userId": user_id}
    
    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.info(f"Running translate task locally for session: {session_id}")
        asyncio.create_task(_run_local_translate(session_id, target_language))
        return

    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    url = f"{CLOUD_RUN_URL}/internal/tasks/translate"

    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        },
        "dispatch_deadline": {"seconds": 600},  # 10 mins for translation
    }

    _create_task_nonblocking(parent, task)
    logger.info(f"Enqueued translate task for session {session_id}")


def enqueue_playlist_task(session_id: str, user_id: str | None = None, job_id: str | None = None):
    """
    プレイリスト生成タスクをキューに入れる。
    """
    payload = {
        "sessionId": session_id,
        "jobId": job_id,
        "userId": user_id,
    }

    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.info(f"Running playlist task locally for session: {session_id}")
        asyncio.create_task(_run_local_playlist(session_id, job_id=job_id))
        return

    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    url = f"{CLOUD_RUN_URL}/internal/tasks/playlist"

    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        }
    }

    _create_task_nonblocking(parent, task)
    logger.info(f"Enqueued playlist task for session {session_id}")


def enqueue_summary_v2_task(
    session_id: str,
    user_id: str | None = None,
    job_id: str | None = None,
    meeting_purpose: str | None = None,
    meeting_type: str | None = None,
    participants: list | None = None,
):
    """
    SummaryV2（根拠付き構造化サマリー）生成タスクをキューに入れる。
    """
    payload = {
        "sessionId": session_id,
        "jobId": job_id,
        "userId": user_id,
        "meetingPurpose": meeting_purpose,
        "meetingType": meeting_type,
        "participants": participants or [],
    }

    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.info(f"Running summary_v2 task locally for session: {session_id}")
        asyncio.create_task(_run_local_summary_v2(session_id, job_id=job_id, **payload))
        return job_id

    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    url = f"{CLOUD_RUN_URL}/internal/tasks/summary_v2"

    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        },
        "dispatch_deadline": {"seconds": 600},  # 10 mins
    }

    _create_task_nonblocking(parent, task)
    logger.info(f"Enqueued summary_v2 task for session {session_id}")
    return job_id


async def _run_local_summary_v2(
    session_id: str,
    job_id: str | None = None,
    meeting_purpose: str | None = None,
    meeting_type: str | None = None,
    participants: list | None = None,
    **kwargs,
):
    """Local fallback for SummaryV2 generation."""
    from app.services.summary_v2 import generate_summary_v2
    from datetime import datetime, timezone

    doc_ref = db.collection("sessions").document(session_id)
    doc = doc_ref.get()
    if not doc.exists:
        logger.error(f"[local summary_v2] Session {session_id} not found")
        return

    data = doc.to_dict()
    transcript = resolve_transcript_text(session_id, data)
    if not transcript:
        logger.error(f"[local summary_v2] No transcript for {session_id}")
        return

    derived_ref = doc_ref.collection("derived").document("summary_v2")

    try:
        # Mark as running
        derived_ref.set({
            "status": "running",
            "jobId": job_id,
            "updatedAt": datetime.now(timezone.utc),
        }, merge=True)

        # Generate
        summary = await generate_summary_v2(
            session_id=session_id,
            transcript_text=transcript,
            diarized_segments=data.get("diarizedSegments"),
            user_marks=data.get("userMarks"),
            meeting_purpose=meeting_purpose or data.get("meetingPurpose"),
            meeting_type=meeting_type or data.get("mode"),
            participants=participants or data.get("participants", []),
        )

        # Save result
        derived_ref.set({
            "status": "succeeded",
            "result": summary.dict(),
            "jobId": job_id,
            "updatedAt": datetime.now(timezone.utc),
        }, merge=True)

        doc_ref.update({
            "summaryV2Status": "completed",
            "summaryV2Markdown": summary.renderedMarkdown,
            "updatedAt": datetime.now(timezone.utc),
        })

        logger.info(f"[local summary_v2] Generated for {session_id}")

    except Exception as e:
        logger.exception(f"[local summary_v2] Failed for {session_id}: {e}")
        derived_ref.set({
            "status": "failed",
            "errorReason": str(e)[:500],
            "jobId": job_id,
            "updatedAt": datetime.now(timezone.utc),
        }, merge=True)


# ---------- Local fallback workers ---------- #

def _update_root_job_status(
    job_id: str,
    status: str,
    result_url: str = None,
    error_reason: str = None,
    stage: str = None,
    progress: float = None,
    partial: dict = None,
):
    """Update job status in root jobs collection (for new async job system)."""
    if not job_id:
        return
    from datetime import timezone
    now = datetime.now(timezone.utc)
    update_data = {
        "status": status,
        "updatedAt": now,
    }
    if status in ["succeeded", "failed"]:
        update_data["completedAt"] = now
        update_data["leaseUntil"] = None
    if result_url:
        update_data["resultUrl"] = result_url
    if error_reason:
        update_data["errorReason"] = error_reason
    if stage:
        update_data["stage"] = stage
    if progress is not None:
        update_data["progress"] = progress
    if partial is not None:
        update_data["partial"] = partial
    try:
        db.collection("jobs").document(job_id).update(update_data)
    except Exception as e:
        logger.warning(f"[job_status] Failed to update job {job_id}: {e}")


async def _run_local_summarize(session_id: str, job_id: str | None = None):
    doc_ref = db.collection("sessions").document(session_id)

    # Stage 1: Mark job as running
    _update_root_job_status(job_id, "running", stage="loading_transcript", progress=0.1)

    try:
        doc = doc_ref.get()
        if not doc.exists:
            logger.warning(f"[local summarize] session not found: {session_id}")
            _update_root_job_status(job_id, "failed", stage="failed", error_reason="Session not found")
            return
        data = doc.to_dict()

        # Stage 2: Load transcript
        transcript = resolve_transcript_text(session_id, data)
        if not transcript:
            logger.warning(f"[local summarize] transcript empty: {session_id}")
            doc_ref.update({
                "summaryStatus": "failed",
                "summaryError": "Transcript is empty",
                "summaryUpdatedAt": datetime.utcnow(),
                "playlistStatus": "failed",
                "playlistError": "Transcript is empty",
                "playlistUpdatedAt": datetime.utcnow(),
                "status": "録音済み",
            })
            _update_root_job_status(job_id, "failed", stage="failed", error_reason="Transcript is empty")
            return

        doc_ref.update({
            "summaryStatus": "running",
            "summaryError": None,
            "summaryUpdatedAt": datetime.utcnow()
        })

        # Stage 3: Generate summary (with stage updates)
        _update_root_job_status(job_id, "running", stage="generating_structure", progress=0.3)

        # ★ Translation sessions: use translate-specific summary path (first-class mode).
        # Bilingual ===ORIGINAL===/===TRANSLATION=== transcript is passed through in full
        # so the translate prompt (built in llm.py) can use both sides.
        mode = data.get("mode", "lecture")
        import_type = data.get("importType")
        if mode == "translate" or import_type == "translate":
            mode = "translate"

        # Read user custom prompts
        user_id = data.get("userId")
        summary_instruction, _ = llm.get_user_custom_prompts(user_id)

        # Fetch transcript chunks so summary bullets can be grounded with
        # anchorMs / segmentIds (text-matched against real segments).
        try:
            transcript_segments = get_transcript_chunks(session_id)
        except Exception as seg_err:
            logger.warning(f"[summarize] failed to load transcript chunks for anchors: {seg_err}")
            transcript_segments = None

        result = await llm.generate_summary_and_tags(
            transcript,
            mode=mode,
            custom_instruction=summary_instruction,
            segments=transcript_segments,
        )

        # Stage 4: Formatting
        _update_root_job_status(job_id, "running", stage="formatting_json", progress=0.8)

        summary_md = result.get("summaryMarkdown")
        summary_json = result.get("summaryJson") or {}
        summary_type = result.get("summaryType") or mode
        summary_json_version = result.get("summaryJsonVersion") or 1
        tags = result.get("tags") or []
        suggested_title = result.get("suggestedTitle")

        # Extract partial for final update (TL;DR from summaryJson if available)
        partial_data = None
        if summary_json:
            tldr = summary_json.get("tldr") or summary_json.get("overview_bullets")
            if tldr:
                partial_data = {"tldr": tldr if isinstance(tldr, list) else [tldr]}

        update_payload = {
            "summaryStatus": "completed",
            "summaryMarkdown": summary_md,
            "summaryJson": summary_json,
            "summaryJsonVersion": summary_json_version,
            "summaryType": summary_type,
            "summaryUpdatedAt": datetime.utcnow(),
            "summaryError": None,
            "autoTags": tags[:4],
            "status": "要約済み",
        }
        # Auto-update title if LLM suggested one and current title looks like a default
        if suggested_title:
            update_payload["suggestedTitle"] = suggested_title
            current_title = data.get("title", "")
            if not current_title or _is_default_title(current_title):
                auto_title = _build_auto_title(suggested_title, data)
                update_payload["title"] = auto_title
                logger.info(f"[Summary] Auto-updated title: '{current_title}' → '{auto_title}'")
        doc_ref.update(update_payload)

        # Stage 5: Completed
        if job_id:
            db.collection("sessions").document(session_id).collection("jobs").document(job_id).update({"status": "completed"})
        result_url = f"/sessions/{session_id}/artifacts/summary"
        _update_root_job_status(
            job_id, "succeeded",
            stage="completed",
            progress=1.0,
            result_url=result_url,
            partial=partial_data,
        )

        # Log usage
        await usage_logger.log(
            user_id=data.get("userId", "unknown"),
            session_id=session_id,
            feature="summary",
            event_type="success"
        )

        # [PLAYLIST PRE-GENERATE] Enqueue playlist if not already done (non-blocking)
        try:
            existing_playlist = data.get("playlist")
            playlist_status = data.get("playlistStatus")
            if not existing_playlist and playlist_status not in ("running", "completed"):
                pl_job_id = f"playlist_{session_id[:8]}"
                enqueue_playlist_task(session_id, job_id=pl_job_id, user_id=data.get("userId"))
                doc_ref.update({"playlistStatus": "running"})
                logger.info(f"[Playlist] Pre-enqueued after summary for {session_id}")
        except Exception as pl_err:
            logger.warning(f"[Playlist] Pre-enqueue failed for {session_id} (non-blocking): {pl_err}")

    except Exception as e:
        error_str = str(e)

        # Handle "session deleted during job" gracefully
        if "No document to update" in error_str or "NOT_FOUND" in error_str:
            logger.info(f"[local summarize] Session {session_id} was deleted during job execution, marking as cancelled")
            _update_root_job_status(job_id, "cancelled", stage="cancelled", error_reason="Session deleted during processing")
            return

        logger.exception(f"[local summarize] failed: {e}")

        # Try to update session status, but ignore if session was deleted
        try:
            doc_ref.update({
                "summaryStatus": "failed",
                "summaryError": error_str,
                "summaryUpdatedAt": datetime.utcnow(),
                "status": "録音済み",
            })
        except Exception as update_error:
            if "No document to update" in str(update_error):
                logger.info(f"[local summarize] Session {session_id} was deleted, skipping error status update")
            else:
                logger.warning(f"[local summarize] Failed to update error status: {update_error}")

        _update_root_job_status(job_id, "failed", stage="failed", error_reason=error_str)

        # Log error (use session_id as user_id fallback if data not available)
        try:
            await usage_logger.log(
                user_id=data.get("userId", "unknown") if 'data' in dir() else "unknown",
                session_id=session_id,
                feature="summary",
                event_type="error",
                payload={"error_code": type(e).__name__}
            )
        except Exception:
            pass  # Don't fail on logging error


async def _run_local_quiz(session_id: str, count: int, job_id: str | None = None):
    doc_ref = db.collection("sessions").document(session_id)

    # Stage 1: Mark job as running
    _update_root_job_status(job_id, "running", stage="loading_transcript", progress=0.1)

    doc = doc_ref.get()
    if not doc.exists:
        logger.warning(f"[local quiz] session not found: {session_id}")
        _update_root_job_status(job_id, "failed", stage="failed", error_reason="Session not found")
        return
    data = doc.to_dict()

    # Stage 2: Load transcript
    transcript = resolve_transcript_text(session_id, data)
    if not transcript:
        logger.warning(f"[local quiz] transcript empty: {session_id}")
        doc_ref.update({
            "quizStatus": "failed",
            "quizError": "Transcript is empty",
            "quizUpdatedAt": datetime.utcnow(),
            "status": "録音済み",
        })
        _update_root_job_status(job_id, "failed", stage="failed", error_reason="Transcript is empty")
        return

    # Stage 3: Generate quiz
    try:
        doc_ref.update({
            "quizStatus": "running",
            "quizError": None,
            "quizUpdatedAt": datetime.utcnow()
        })

        _update_root_job_status(job_id, "running", stage="generating_questions", progress=0.3)

        # ★ Translation sessions: extract original text and use lecture mode
        quiz_mode = data.get("mode", "lecture")
        quiz_transcript = transcript
        if (quiz_mode == "translate" or data.get("importType") == "translate") and "===ORIGINAL===" in transcript:
            original_part = transcript.split("\n===TRANSLATION===")[0]
            quiz_transcript = original_part.replace("===ORIGINAL===\n", "").strip()
            quiz_mode = "lecture"
        elif quiz_mode == "translate":
            quiz_mode = "lecture"

        # Read user custom prompts
        user_id = data.get("userId")
        _, quiz_instruction = llm.get_user_custom_prompts(user_id)

        quiz_md = await llm.generate_quiz(quiz_transcript, mode=quiz_mode, count=count, custom_instruction=quiz_instruction)

        # Stage 4: Formatting
        _update_root_job_status(job_id, "running", stage="formatting", progress=0.8)

        doc_ref.update({
            "quizStatus": "completed",
            "quizMarkdown": quiz_md,
            "quizUpdatedAt": datetime.utcnow(),
            "quizError": None,
        })

        # Stage 5: Completed
        if job_id:
            db.collection("sessions").document(session_id).collection("jobs").document(job_id).update({"status": "completed"})
        result_url = f"/sessions/{session_id}/artifacts/quiz"
        _update_root_job_status(job_id, "succeeded", stage="completed", progress=1.0, result_url=result_url)
    except Exception as e:
        error_str = str(e)

        # Handle "session deleted during job" gracefully
        if "No document to update" in error_str or "NOT_FOUND" in error_str:
            logger.info(f"[local quiz] Session {session_id} was deleted during job execution, marking as cancelled")
            _update_root_job_status(job_id, "cancelled", stage="cancelled", error_reason="Session deleted during processing")
            return

        logger.exception(f"[local quiz] failed: {e}")

        # Try to update session status, but ignore if session was deleted
        try:
            doc_ref.update({
                "quizStatus": "failed",
                "quizError": error_str,
                "quizUpdatedAt": datetime.utcnow(),
            })
        except Exception as update_error:
            if "No document to update" in str(update_error):
                logger.info(f"[local quiz] Session {session_id} was deleted, skipping error status update")
            else:
                logger.warning(f"[local quiz] Failed to update error status: {update_error}")

        _update_root_job_status(job_id, "failed", stage="failed", error_reason=error_str)


async def _run_local_playlist(session_id: str, job_id: str | None = None):
    doc_ref = db.collection("sessions").document(session_id)
    doc = doc_ref.get()
    if not doc.exists:
        return
    data = doc.to_dict()
    transcript = resolve_transcript_text(session_id, data)
    if not transcript:
        return
    
    try:
        # Update status
        doc_ref.update({"playlistStatus": "running"})
        _derived_doc_ref(session_id, "playlist").set({
            "status": "running",
            "updatedAt": datetime.utcnow(),
            "jobId": job_id
        }, merge=True)
        
        # Generate
        segments = data.get("diarizedSegments")
        duration = data.get("durationSec")
        playlist_json_str = await llm.generate_playlist_timeline(transcript, segments=segments, duration_sec=duration)

        try:
            raw_items = json.loads(playlist_json_str)
        except:
            raw_items = []

        # [NEW] Normalize: ms/sec detection, duration clamp, validation
        items = normalize_playlist_items(raw_items, segments=segments, duration_sec=duration)

        if not items:
            logger.warning(f"[local playlist] LLM returned empty playlist for {session_id}")
            doc_ref.update({"playlistStatus": "failed", "playlistError": "Empty playlist from LLM"})
            _derived_doc_ref(session_id, "playlist").set({"status": "failed", "errorReason": "empty_result", "updatedAt": datetime.utcnow(), "jobId": job_id}, merge=True)
            return

        # Update result (Legacy playlist field + New Artifact)
        ts = datetime.utcnow()
        doc_ref.update({
            "playlistStatus": "completed",
            "playlist": items,
            "playlistUpdatedAt": ts
        })
        _derived_doc_ref(session_id, "playlist").set({
            "status": "succeeded",
            "result": {"items": items},
            "updatedAt": ts,
            "jobId": job_id # Persist jobId
        }, merge=True)
        
        if job_id:
             db.collection("sessions").document(session_id).collection("jobs").document(job_id).update({"status": "completed"})
             
    except Exception as e:
        logger.exception(f"[local playlist] failed: {e}")
        doc_ref.update({"playlistStatus": "failed"})
        _derived_doc_ref(session_id, "playlist").set({
            "status": "failed", 
            "errorReason": str(e),
            "updatedAt": datetime.utcnow()
        }, merge=True)

        return
    doc_ref.update({"quizStatus": "running", "quizError": None, "status": "テスト生成"})
    try:
        from app.services.llm import clean_quiz_markdown
        # ★ Translation sessions: use lecture mode for quiz generation
        quiz_mode2 = data.get("mode", "lecture")
        quiz_transcript2 = transcript
        if (quiz_mode2 == "translate" or data.get("importType") == "translate") and "===ORIGINAL===" in transcript:
            original_part = transcript.split("\n===TRANSLATION===")[0]
            quiz_transcript2 = original_part.replace("===ORIGINAL===\n", "").strip()
            quiz_mode2 = "lecture"
        elif quiz_mode2 == "translate":
            quiz_mode2 = "lecture"
        quiz_raw = await llm.generate_quiz(quiz_transcript2, mode=quiz_mode2, count=count)
        quiz_md = clean_quiz_markdown(quiz_raw)
        
        doc_ref.update({
            "quizStatus": "completed",
            "quizMarkdown": quiz_md,
            "quizUpdatedAt": datetime.utcnow(),
            "quizError": None,
            "status": "テスト完了",
        })
        if job_id:
            db.collection("sessions").document(session_id).collection("jobs").document(job_id).update({"status": "completed"})
    except Exception as e:
        logger.exception(f"[local quiz] failed: {e}")
        doc_ref.update({"quizStatus": "failed", "quizError": str(e), "status": "録音済み"})


async def _run_local_highlights(session_id: str):
    doc_ref = db.collection("sessions").document(session_id)
    doc = doc_ref.get()
    if not doc.exists:
        logger.warning(f"[local highlights] session not found: {session_id}")
        return
    doc_ref.update({
        "highlightsStatus": "failed",
        "highlightsError": "deprecated",
        "highlightsUpdatedAt": datetime.utcnow()
    })


def enqueue_generate_highlights_task(session_id: str, user_id: str | None = None, job_id: str | None = None):
    """
    ハイライト生成タスクをキューに入れる。
    """
    logger.info("Highlights task is deprecated; skipping enqueue.")


def enqueue_todo_extraction_task(
    session_id: str,
    account_id: str,
    source_key: str,
    summary_text: str,
    transcript_text: str | None = None,
    mode: str = "lecture",
    user_id: str | None = None,
):
    """
    [NEW 2026-02] TODO抽出タスクを非同期でキューに入れる。
    要約完了後に呼び出し、ユーザーの待ち時間を削減する。
    """
    payload = {
        "sessionId": session_id,
        "accountId": account_id,
        "sourceKey": source_key,
        "summaryText": summary_text,
        "transcriptText": transcript_text or "",
        "mode": mode,
        "userId": user_id,
    }

    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.info(f"Running TODO extraction locally for session: {session_id}")
        asyncio.create_task(_run_local_todo_extraction(**payload))
        return

    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    url = f"{CLOUD_RUN_URL}/internal/tasks/todo_extraction"

    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        },
        "dispatch_deadline": {"seconds": 120},  # 2 mins max for TODO extraction
    }

    _create_task_nonblocking(parent, task)
    logger.info(f"Enqueued TODO extraction task for session {session_id}")


async def _run_local_todo_extraction(
    sessionId: str,
    accountId: str,
    sourceKey: str,
    summaryText: str,
    transcriptText: str = "",
    mode: str = "lecture",
    userId: str | None = None,
    **kwargs,
):
    """Local fallback for TODO extraction."""
    from app.services.todo_extractor import update_todos_from_summary

    try:
        todo_stats = await update_todos_from_summary(
            session_id=sessionId,
            account_id=accountId,
            source_key=sourceKey,
            summary_text=summaryText,
            transcript_text=transcriptText,
            mode=mode,
        )
        logger.info(f"[local TODO] Extracted for {sessionId}: created={todo_stats.get('created')}")

        # Update session with todo_status
        doc_ref = db.collection("sessions").document(sessionId)
        doc_ref.update({
            "todoStatus": "completed",
            "todoUpdatedAt": datetime.utcnow(),
            "todoStats": todo_stats,
        })
    except Exception as e:
        logger.exception(f"[local TODO] Failed for {sessionId}: {e}")
        # Mark as failed but don't block
        try:
            doc_ref = db.collection("sessions").document(sessionId)
            doc_ref.update({
                "todoStatus": "failed",
                "todoError": str(e)[:200],
                "todoUpdatedAt": datetime.utcnow(),
            })
        except Exception:
            pass


def enqueue_nuke_user_task(user_id: str):
    """
    [CRITICAL] Enqueue a task to completely wipe a user's account and data.
    """
    payload = {"userId": user_id}

    # Lock Safety: Local execution not supported for safety (always async)
    # But if no queue client, we must log error or use background_tasks if passed
    # For now, if no client, we just log error as this is critical op.
    
    if tasks_client is None:
        if os.environ.get("USE_LOCAL_TASKS") == "1":
             # Local dev mode - just spawn async task? 
             # Or maybe we need to support it for testing.
             logger.info(f"Running NUKE task locally for {user_id}")
             asyncio.create_task(_run_local_nuke(user_id))
             return
        logger.error("Cloud Tasks client missing, cannot enqueue Nuke User task.")
        return

    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    url = f"{CLOUD_RUN_URL}/internal/tasks/nuke_user"

    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        },
        "dispatch_deadline": {"seconds": 1800}, # 30 mins max
    }

    try:
        _create_task_nonblocking(parent, task)
        logger.info(f"Enqueued NUKE task for {user_id}")
    except Exception as e:
        logger.error(f"Failed to enqueue Nuke task for {user_id}: {e}")
        raise e


async def _run_local_nuke(user_id: str):
    """
    Local fallback for complete account deletion.
    Uses the nuke_user_complete service function.
    """
    from app.services.session_cleanup import nuke_user_complete
    logger.info(f"[LocalNuke] Starting complete deletion for {user_id}")
    result = nuke_user_complete(user_id)
    logger.info(f"[LocalNuke] Result for {user_id}: {result}")
    return result


def enqueue_transcribe_task(
    session_id: str,
    force: bool = False,
    engine: str = "google",
    job_id: str | None = None,
    idempotency_key: str | None = None,
    user_id: str | None = None,
):
    """
    文字起こしタスク（Cloud Run Jobs / Functions 連携用）
    job_id を渡すことで、ジョブドキュメントのステータス更新が可能になる。
    """
    payload = {
        "sessionId": session_id,
        "force": force,
        "engine": engine,
        "jobId": job_id,
        "idempotencyKey": idempotency_key,
        "userId": user_id,
    }

    # ローカルモード: asyncio.create_task でバックグラウンド実行
    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.info(f"Running transcribe task locally for session: {session_id}")
        asyncio.create_task(_run_local_transcribe(session_id, force=force, engine=engine, job_id=job_id, user_id=user_id))
        return

    # Cloud Tasks
    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    url = f"{CLOUD_RUN_URL}/internal/tasks/transcribe"

    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        },
        "dispatch_deadline": {"seconds": 1800},  # 30 mins (Max for Cloud Tasks HTTP)
    }

    try:
        _create_task_nonblocking(parent, task)
        logger.info(f"Enqueued transcribe task for session {session_id}, job_id={job_id}")
    except Exception as e:
        logger.error(f"Failed to enqueue transcribe task: {e}")
        raise e

def enqueue_youtube_import_task(session_id: str, url: str, language: str = "ja", user_id: str | None = None, job_id: str | None = None):
    """
    YouTube取込タスクをキューに入れる。
    """
    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        # Local fallback: try running async if possible, but ffmpeg might be missing.
        # We assume local env has deps or we warn.
        logger.info(f"Running youtube import locally for {session_id}")
        asyncio.create_task(_run_local_youtube_import(session_id, url, language))
        return

    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    # Using generic queue for now
    target_url = f"{CLOUD_RUN_URL}/internal/tasks/import_youtube"
    payload = {"sessionId": session_id, "url": url, "language": language, "userId": user_id, "jobId": job_id}

    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": target_url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        },
        "dispatch_deadline": {"seconds": 1800}, # 30 mins (Max for Cloud Tasks HTTP)
    }

    try:
        _create_task_nonblocking(parent, task)
        logger.info(f"Enqueued youtube import task for {session_id}")
    except Exception as e:
        logger.error(f"Failed to enqueue youtube import task: {e}")
        raise e

def enqueue_merge_migration_task(merge_id: str):
    """
    Enqueues the background worker to migrate data for an account merge.
    """
    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.info(f"Running merge migration locally for {merge_id}")
        import asyncio
        from app.routes.tasks import _run_local_merge_migration
        asyncio.create_task(_run_local_merge_migration(merge_id))
        return

    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    url = f"{CLOUD_RUN_URL}/internal/tasks/merge_migration"
    payload = {"mergeId": merge_id}

    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        }
    }

    try:
        _create_task_nonblocking(parent, task)
        logger.info(f"Enqueued merge migration task for {merge_id}")
    except Exception as e:
        logger.error(f"Failed to enqueue merge migration task: {e}")
        raise e


def enqueue_account_migration_task(from_account_id: str, to_account_id: str):
    """
    Enqueues a task to migrate data (sessions, etc.) from one account to another.
    Used after phone verification triggers account merge.
    """
    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.info(f"Running account migration locally: {from_account_id} -> {to_account_id}")
        import asyncio
        asyncio.create_task(_run_local_account_migration(from_account_id, to_account_id))
        return

    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    url = f"{CLOUD_RUN_URL}/internal/tasks/account_migration"
    payload = {"fromAccountId": from_account_id, "toAccountId": to_account_id}

    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        }
    }

    try:
        _create_task_nonblocking(parent, task)
        logger.info(f"Enqueued account migration task: {from_account_id} -> {to_account_id}")
    except Exception as e:
        logger.error(f"Failed to enqueue account migration task: {e}")
        raise e


async def _run_local_account_migration(from_account_id: str, to_account_id: str):
    """
    Local fallback for account migration.
    Moves sessions from one account to another.
    """
    from app.firebase import db
    from datetime import datetime, timezone

    logger.info(f"[LocalAccountMigration] Starting: {from_account_id} -> {to_account_id}")
    now = datetime.now(timezone.utc)
    batch_size = 200
    total_migrated = 0

    while True:
        # Find sessions owned by old account
        sessions_query = (
            db.collection("sessions")
            .where("ownerAccountId", "==", from_account_id)
            .limit(batch_size)
        )
        docs = list(sessions_query.stream())

        if not docs:
            break

        batch = db.batch()
        for doc in docs:
            batch.update(doc.reference, {
                "ownerAccountId": to_account_id,
                "migratedFrom": from_account_id,
                "migratedAt": now,
                "updatedAt": now
            })
        batch.commit()
        total_migrated += len(docs)

        if len(docs) < batch_size:
            break

    logger.info(f"[LocalAccountMigration] Complete: migrated {total_migrated} sessions")


async def _run_local_youtube_import(session_id: str, url: str, language: str):
    # This invokes the worker logic directly (must implement import inside tasks.py or services)
    # Since worker logic is in services, we can call it here OR import tasks router logic.
    # To keep it simple, we just call the service and update DB here.
    from app.services.youtube import process_youtube_import
    # DB update logic duplicates what's in tasks.py...
    # For now, let's keep local fallback minimal or point to tasks.py handler if possible.
    # It's better to implement the logic ONCE in tasks.py and have local fallback call it?
    # No, tasks.py handlers take Request.
    # We will duplicate the DB wrapping logic in _run_local_youtube_import briefly.
    logger.info("Local YouTube Import Started")
    doc_ref = db.collection("sessions").document(session_id)
    try:
        transcript = await asyncio.to_thread(process_youtube_import, session_id, url, language)
        # Update DB
        doc_ref.update({
            "transcriptText": transcript,
            "status": "録音済み",
            "audioPath": f"imports/{session_id}.flac" 
        })
        # [Security] Pass userId if available
        data = doc_ref.get().to_dict() or {}
        uid = data.get("ownerUserId") or data.get("userId")

        # [DISABLED] Summary/Quiz auto-trigger removed — user triggers manually via generate button
    except Exception as e:
        logger.exception("Local YouTube Import Failed")
        doc_ref.update({"status": "failed", "transcriptText": f"Error: {e}"})


# ---------- Local fallback workers for QA and Translate ---------- #

async def _run_local_qa(session_id: str, question: str, user_id: str, qa_id: str):
    """Local fallback for QA task."""
    doc_ref = db.collection("sessions").document(session_id)
    qa_ref = doc_ref.collection("qa_results").document(qa_id)
    
    try:
        doc = doc_ref.get()
        if not doc.exists:
            logger.warning(f"[local qa] session not found: {session_id}")
            return
        
        data = doc.to_dict()
        transcript = resolve_transcript_text(session_id, data) or ""
        
        if not transcript:
            qa_ref.set({"status": "failed", "error": "Transcript empty", "updatedAt": datetime.utcnow()})
            return
        
        qa_ref.set({"status": "running", "question": question, "updatedAt": datetime.utcnow()}, merge=True)
        
        result = await llm.answer_question(transcript, question, data.get("mode", "lecture"))
        answer = result.get("answer", "")
        citations = result.get("citations", [])
        
        qa_ref.set({
            "status": "completed",
            "answer": answer,
            "citations": citations,
            "updatedAt": datetime.utcnow(),
        }, merge=True)
        
    except Exception as e:
        logger.exception(f"[local qa] failed: {e}")
        qa_ref.set({"status": "failed", "error": str(e), "updatedAt": datetime.utcnow()}, merge=True)


async def _run_local_translate(session_id: str, target_language: str):
    """Local fallback for Translate task."""
    doc_ref = db.collection("sessions").document(session_id)
    trans_ref = db.collection("translations").document(session_id)
    
    try:
        doc = doc_ref.get()
        if not doc.exists:
            logger.warning(f"[local translate] session not found: {session_id}")
            return
        
        data = doc.to_dict()
        transcript = resolve_transcript_text(session_id, data) or ""
        
        if not transcript:
            trans_ref.set({"status": "failed", "error": "Transcript empty", "updatedAt": datetime.utcnow()})
            return
        
        trans_ref.set({"status": "running", "language": target_language, "updatedAt": datetime.utcnow()}, merge=True)
        
        translated_text = await llm.translate_text(transcript, target_language)
        
        trans_ref.set({
            "status": "completed",
            "language": target_language,
            "translatedText": translated_text,
            "updatedAt": datetime.utcnow(),
        }, merge=True)
        
    except Exception as e:
        logger.exception(f"[local translate] failed: {e}")
        trans_ref.set({"status": "failed", "error": str(e), "updatedAt": datetime.utcnow()}, merge=True)


async def _run_local_transcribe(session_id: str, force: bool = False, engine: str = "google", job_id: str | None = None):
    """
    Local fallback for Transcribe task.
    Google Speech-to-Text を呼び出し、結果を Firestore に保存する。
    job_id があればジョブドキュメントのステータスも更新する。
    """
    doc_ref = db.collection("sessions").document(session_id)
    job_ref = db.collection("sessions").document(session_id).collection("jobs").document(job_id) if job_id else None

    def _update_job_status(status: str, error: str | None = None):
        if job_ref:
            update = {"status": status, "updatedAt": datetime.utcnow()}
            if error:
                update["error"] = error
            job_ref.set(update, merge=True)

    try:
        doc = doc_ref.get()
        if not doc.exists:
            logger.warning(f"[local transcribe] session not found: {session_id}")
            _update_job_status("failed", "Session not found")
            return

        data = doc.to_dict()
        audio_info = data.get("audio") or {}
        gcs_path = audio_info.get("gcsPath") or data.get("audioPath")

        if not gcs_path:
            logger.warning(f"[local transcribe] no audio path for session: {session_id}")
            _update_job_status("failed", "No audio path found")
            doc_ref.update({"transcriptionStatus": "failed", "transcriptionError": "No audio path found"})
            return

        # Update status: running
        _update_job_status("running")
        doc_ref.update({"transcriptionStatus": "running", "transcriptionEngine": engine})

        if engine == "google":
            from app.services.google_speech import transcribe_audio_google_with_segments
            transcript_text, segments = transcribe_audio_google_with_segments(
                gcs_path, language_code="ja-JP"
            )
        else:
            # fallback: google
            from app.services.google_speech import transcribe_audio_google_with_segments
            transcript_text, segments = transcribe_audio_google_with_segments(
                gcs_path, language_code="ja-JP"
            )

        now = datetime.utcnow()

        # Save artifact
        artifact_ref = doc_ref.collection("artifacts").document("transcript_google")
        artifact_ref.set({
            "text": transcript_text,
            "source": f"cloud_{engine}",
            "modelInfo": {"engine": f"google_speech_v1"},
            "createdAt": now,
            "type": "transcript",
        })

        # Update session document
        transcription_mode = data.get("transcriptionMode", "cloud_google")
        updates = {
            "transcriptionStatus": "completed",
            "transcriptionUpdatedAt": now,
        }
        if transcription_mode == "cloud_google" or not data.get("transcriptText"):
            updates["transcriptText"] = transcript_text
            if segments:
                updates["segments"] = segments

        doc_ref.update(updates)

        # Update job status: completed
        _update_job_status("completed")

        # [DISABLED] Summary/Quiz auto-trigger removed — user triggers manually via generate button

        logger.info(f"[local transcribe] completed for session: {session_id}")

    except Exception as e:
        logger.exception(f"[local transcribe] failed for {session_id}: {e}")
        _update_job_status("failed", str(e))
        doc_ref.update({
            "transcriptionStatus": "failed",
            "transcriptionError": str(e),
        })

def enqueue_merge_migration_task(
    merge_job_id: str,
    source_uid: str,
    target_account_id: str,
):
    """
    Enqueues a background task to migrate sessions from a source UID to a target Account ID.
    Used during Account Merge (Strategy B).
    """
    payload = {
        "mergeJobId": merge_job_id,
        "sourceUid": source_uid,
        "targetAccountId": target_account_id,
    }

    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.info(f"Running merge migration locally for job: {merge_job_id}")
        asyncio.create_task(_run_local_merge_migration(merge_job_id, source_uid, target_account_id))
        return

    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    url = f"{CLOUD_RUN_URL}/internal/tasks/merge_migration"

    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        },
        "dispatch_deadline": {"seconds": 1800},
    }

    try:
        _create_task_nonblocking(parent, task)
        logger.info(f"Enqueued merge migration task for job {merge_job_id}")
    except Exception as e:
        logger.error(f"Failed to enqueue merge migration task: {e}")
        # In a critical merge flow, we might want to retry or alert, but usually Cloud Tasks is reliable.
        raise e

async def _run_local_merge_migration(merge_job_id: str, source_uid: str, target_account_id: str):
    """
    Local fallback for Merge Migration.
    Directly calls the worker logic (simulated or imported).
    """
    # Ideally import from routes/tasks or services, but to avoid circular imports,
    # we might just trigger the endpoint/function if it was refactored.
    # For now, minimal mock or warning that it needs the server running.
    logger.warning("Local merge migration is not fully implemented in task_queue (logic is in routes/tasks.py).")
    # If we wanted to run it, we'd need to move the logic to a service.
    # For now, we assume local dev might not test full merge migration backgrounding strictly.


# ---------- finalize v2: derived feature worker enqueue ---------- #

def enqueue_derived_finalize_task(
    session_id: str,
    feature: str,
    *,
    reservation_id: str,
    user_id: str | None = None,
    account_id: str | None = None,
    operation_id: str | None = None,
    client_request_id: str | None = None,
) -> None:
    """finalize v2 の per-feature derived worker を Cloud Tasks にエンキューする。

    PR D 時点では worker 本体(`/internal/tasks/derived/finalize`)は未実装。
    PR E で routes/tasks.py にハンドラを追加するまで、本関数はキュー投入のみを担い
    ローカル実行では警告ログを出して no-op する。

    Args:
        session_id: 対象 session(canonical id)
        feature: `summary` | `highlights` | `quiz` のいずれか
        reservation_id: 対応する credits_reservations doc id(= clientRequestId)
        user_id: 実行ユーザー uid
        account_id: 請求先 account id
        operation_id: finalize operationId(audit 相関用)
        client_request_id: 元 client request id(= reservation_id と同じ)
    """
    payload = {
        "sessionId": session_id,
        "feature": feature,
        "reservationId": reservation_id,
        "userId": user_id,
        "accountId": account_id,
        "operationId": operation_id,
        "clientRequestId": client_request_id or reservation_id,
    }

    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.warning(
            f"[derived_finalize] local mode no-op for session={session_id} feature={feature}"
            " (worker deferred to PR E)"
        )
        return

    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    url = f"{CLOUD_RUN_URL}/internal/tasks/derived/finalize"

    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        },
        "dispatch_deadline": {"seconds": 1800},  # 30 mins
    }

    _create_task_nonblocking(parent, task)
    logger.info(
        f"[derived_finalize] enqueued session={session_id} feature={feature}"
        f" reservation={reservation_id}"
    )
