from fastapi import APIRouter, HTTPException, Request
from google.cloud import firestore
# from app.firebase import db
from app.firebase import AUDIO_BUCKET_NAME
from app.services.llm import (
    GEMINI_MODEL_NAME,
    generate_quiz,
    generate_summary_and_tags,
    clean_quiz_markdown,
    answer_question,
    translate_text,
)
from app.services.transcripts import resolve_transcript_text
from app.services.usage import usage_logger
from app.services.ops_logger import log_job_transition, log_llm_event, log_stt_event, ErrorCode
from app.services.cost_guard import cost_guard
from app.services.session_event_bus import publish_session_event
from app.services.account_deletion import (
    LOCKS_COLLECTION,
    REQUESTS_COLLECTION,
    deletion_lock_id,
)
import logging
import json
from datetime import datetime, timezone

router = APIRouter()
logger = logging.getLogger("app.tasks")

@router.get("/internal/tasks/ping")
async def ping_task():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc)}

@router.post("/internal/tasks/summarize")
async def handle_summarize_task(request: Request):
    try:
        payload = await request.json()
        uid = payload.get("userId")
        # Idempotency check logic is inside core, but we need to run core.
    except:
        uid = None

    try:
        return await _handle_summarize_task_core(request)
    finally:
        if uid:
            try:
                await usage_logger.decrement_inflight(uid, "summary")
            except Exception as e:
                logger.error(f"Failed to decrement inflight summary: {e}")

async def _handle_summarize_task_core(request: Request):
    """
    Cloud Tasks Worker for Summarization.
    Decrement inflight count on completion.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    session_id = payload.get("sessionId")
    job_id = payload.get("jobId")
    idempotency_key = payload.get("idempotencyKey")
    user_id = payload.get("userId") # [Security]
    usage_reserved = bool(payload.get("usageReserved"))

    if not session_id:
        logger.error("sessionId is missing")
        return {"status": "error", "message": "sessionId required"}
    
    # Attempt to use payload user_id, will refine from DB if needed
    final_user_id = user_id 

    try:

        # [FIX] Initialize DB locally
        from google.cloud import firestore
        import os
        try:
            project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
            db = firestore.Client(project=project_id)
        except Exception as e:
            logger.error(f"Failed to init local DB: {e}")
            return {"status": "failed", "error": f"DB Init Failed: {e}"}

        doc_ref = db.collection("sessions").document(session_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            logger.warning(f"Session {session_id} not found.")
            return {"status": "skipped", "reason": "not_found"}

        data = doc.to_dict()
        # [Security] Resolve User ID if missing
        if not final_user_id:
            final_user_id = data.get("ownerUserId") or data.get("userId") 
            old_uid = data.get("ownerUid")
            if not final_user_id: final_user_id = old_uid

        transcript = resolve_transcript_text(session_id, data) or ""
        mode = data.get("mode", "lecture")
        derived_ref = doc_ref.collection("derived").document("summary")

        if idempotency_key:
            derived_snap = derived_ref.get()
            if derived_snap.exists:
                current_key = (derived_snap.to_dict() or {}).get("idempotencyKey")
                if current_key and current_key == idempotency_key:
                    if job_id:
                         db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "completed", "result": "cached"}, merge=True)
                    return {"status": "skipped", "reason": "idempotent_hit"}

        if not transcript:
            logger.error(f"Transcript empty for session {session_id}")
            doc_ref.update({
                "summaryStatus": "failed",
                "summaryError": "Transcript is empty",
                "summaryUpdatedAt": datetime.now(timezone.utc),
                "status": "録音済み",
            })
            if job_id:
                db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "failed", "errorReason": "Transcript is empty"}, merge=True)
            derived_ref.set({
                "status": "failed",
                "errorReason": "Transcript is empty",
                "updatedAt": datetime.now(timezone.utc),
                "idempotencyKey": idempotency_key,
            }, merge=True)
            await publish_session_event(session_id, "assets.updated", {"fields": ["summary"]})
            return {"status": "failed", "reason": "empty_transcript"}
        
        # Start Processing
        doc_ref.update({
            "summaryStatus": "running",
            "summaryUpdatedAt": datetime.now(timezone.utc),
        })
        derived_ref.set({
            "status": "running",
            "errorReason": None,
            "updatedAt": datetime.now(timezone.utc),
            "idempotencyKey": idempotency_key,
        }, merge=True)

        # [TRIPLE LOCK] Monthly Cost Guard
        if not usage_reserved:
            allowed, meta = await cost_guard.guard_can_consume(final_user_id, "summary_generated", 1)
            if not allowed:

                 logger.warning(f"[CostGuard] BLOCKED summary {session_id} for user {final_user_id}. Monthly limit exceeded.")
                 err_msg = "Monthly Summary limit exceeded (Free: 3, Premium: 1000 AI pool)"
                 doc_ref.update({"summaryStatus": "failed", "summaryError": err_msg, "status": "録音済み"})
                 if job_id:
                     db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "failed", "error": err_msg}, merge=True)
                 derived_ref.set({"status": "failed", "errorReason": err_msg, "updatedAt": datetime.now(timezone.utc)}, merge=True)
                 await publish_session_event(session_id, "assets.updated", {"fields": ["summary"]})
                 return {"status": "failed", "error": err_msg}

        # ops_logger: job started
        log_job_transition(session_id, "summarize", "started", uid=final_user_id, job_id=job_id)

        result = await generate_summary_and_tags(transcript, mode=mode)

        # ops_logger: LLM call completed
        log_llm_event(session_id, "summary", "completed", uid=final_user_id, model=GEMINI_MODEL_NAME)
        summary_markdown = result.get("summaryMarkdown")
        summary_json = result.get("summaryJson") or {}
        summary_type = result.get("summaryType") or mode
        summary_json_version = result.get("summaryJsonVersion") or 1
        tags = (result.get("tags") or [])[:4]

        topic_summary = None
        if summary_markdown:
             lines = summary_markdown.split('\n')
             for line in lines:
                 if line.strip() and not line.startswith('#'):
                     topic_summary = line.strip()[:100]
                     break
        
        update_payload = {
            "summaryStatus": "completed",
            "summaryMarkdown": summary_markdown,
            "summaryJson": summary_json,
            "summaryJsonVersion": summary_json_version,
            "summaryType": summary_type,
            "topicSummary": topic_summary,
            "summaryUpdatedAt": datetime.now(timezone.utc),
            "summaryError": None,
            "autoTags": tags,
            "status": "要約済み",
        }
        doc_ref.update(update_payload)
        derived_ref.set({
            "status": "succeeded",
            "result": {
                "json": summary_json,
                "markdown": summary_markdown,
                "tags": tags,
                "topicSummary": topic_summary,
            },
            "meta": {
                "schemaVersion": summary_json_version,
                "type": summary_type,
            },
            "modelInfo": {"provider": "vertexai"},
            "updatedAt": datetime.now(timezone.utc),
            "errorReason": None,
            "idempotencyKey": idempotency_key,
        }, merge=True)
        
        logger.info(f"Successfully summarized session {session_id}")
        if job_id:
            db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "completed"}, merge=True)

        # ops_logger: job completed
        log_job_transition(session_id, "summarize", "completed", uid=final_user_id, job_id=job_id)
        await publish_session_event(session_id, "assets.updated", {"fields": ["summary"]})
        return {"status": "completed"}

    except Exception as e:
        logger.exception(f"Summarization failed for session {session_id}")

        # ops_logger: LLM/job failed
        log_llm_event(session_id, "summary", "failed", uid=final_user_id, error_code=ErrorCode.VERTEX_SCHEMA_PARSE_ERROR, error_message=str(e))
        error_str = str(e)
        is_transient = "429" in error_str or "503" in error_str or "ResourceExhausted" in error_str or "ServiceUnavailable" in error_str
        
        if is_transient:
             logger.warning(f"Transient error detected for session {session_id}, raising 503 for retry.")
             raise HTTPException(status_code=503, detail="Transient error, retrying...")

        # Update DB on failure
        # We need to be careful if DB init failed, db might be undefined? 
        # But we are inside try where db init happened or returned.
        try:
             doc_ref.update({
                 "summaryStatus": "failed",
                 "summaryError": str(e),
                 "summaryUpdatedAt": datetime.now(timezone.utc),
                 "status": "録音済み",
             })
             if job_id:
                 db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "failed", "errorReason": str(e)}, merge=True)
             derived_ref.set({
                 "status": "failed",
                 "errorReason": str(e),
                 "updatedAt": datetime.now(timezone.utc),
                 "idempotencyKey": idempotency_key,
             }, merge=True)
             await publish_session_event(session_id, "assets.updated", {"fields": ["summary"]})
        except Exception as db_err:
            logger.warning(f"[summarize] Failed to update error status in DB: {db_err}")

        # ops_logger: job failed
        log_job_transition(session_id, "summarize", "failed", uid=final_user_id, job_id=job_id, error_code=ErrorCode.JOB_WORKER_500, error_message=str(e))

        return {"status": "failed", "error": str(e)}

    # finally: removed



@router.post("/internal/tasks/import_youtube")
async def handle_import_youtube_task(request: Request):
    """
    YouTube取込を行うWorker。
    Cloud Tasksから呼ばれる。
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    session_id = payload.get("sessionId")
    url = payload.get("url")
    language = payload.get("language", "ja")
    user_id = payload.get("userId")

    if not session_id or not url:
        return {"status": "error", "message": "Missing sessionId or url"}

    final_user_id = user_id

    logger.info(f"Processing YouTube Import Task for {session_id} (lang: {language})")

    # [FIX] Initialize DB locally with STANDALONE Client
    from google.cloud import firestore
    import os
    try:
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
        db = firestore.Client(project=project_id)
    except Exception as e:
        logger.error(f"Failed to init local DB: {e}")
        return {"status": "failed", "error": f"DB Init Failed: {e}"}

    doc_ref = db.collection("sessions").document(session_id)
    
    # Check if session exists
    doc = doc_ref.get()
    if not doc.exists:
        return {"status": "skipped", "reason": "not_found"}

    data = doc.to_dict()
    # [Security] Resolve User ID if missing
    if not final_user_id:
        final_user_id = data.get("ownerUserId") or data.get("userId") or data.get("ownerUid")

    try:
        from app.services.youtube import process_youtube_import
        
        # ops_logger: job started
        log_job_transition(session_id, "import_youtube", "started", uid=final_user_id)
        
        # Execute Process (Blocking/Sync)
        # process_youtube_import is blocking, but that's fine for Cloud Run Worker (single request)
        # or we run in threadpool if we want concurrency (FastAPI handles def as threadpool)
        transcript = process_youtube_import(session_id, url, language=language)
        
        # Success - Note: No audioPath since we use transcript API instead of audio download
        doc_ref.update({
            "transcriptText": transcript,
            "status": "録音済み",
            "transcriptSource": "youtube_caption",
            "updatedAt": datetime.now(timezone.utc)
        })
        
        # Trigger Next Steps
        from app.task_queue import enqueue_summarize_task, enqueue_quiz_task
        # [Security] Pass userId to keep inflight tracking correct if they implement handling
        uid = final_user_id
        enqueue_summarize_task(session_id, user_id=uid)
        enqueue_quiz_task(session_id, user_id=uid)
        
        logger.info(f"YouTube Import Success for {session_id}")

        # ops_logger: job completed
        log_job_transition(session_id, "import_youtube", "completed", uid=final_user_id)
        return {"status": "completed"}

    except Exception as e:
        logger.exception(f"YouTube Import Failed for {session_id}")

        # ops_logger: job failed
        log_job_transition(session_id, "import_youtube", "failed", uid=final_user_id, error_code=ErrorCode.JOB_WORKER_500, error_message=str(e))

        doc_ref.update({
            "status": "failed", # Or specific error status
            "transcriptText": None,
            "errorMessage": str(e), # Using generic field or create new?
            "updatedAt": datetime.now(timezone.utc)
        })
        return {"status": "failed", "error": str(e)}

@router.post("/internal/tasks/quiz")
async def handle_quiz_task(request: Request):
    try:
        payload = await request.json()
        uid = payload.get("userId")
    except:
        uid = None

    try:
        return await _handle_quiz_task_core(request)
    finally:
        if uid:
            try:
                await usage_logger.decrement_inflight(uid, "quiz")
            except Exception as e:
                logger.error(f"Failed to decrement inflight quiz: {e}")

async def _handle_quiz_task_core(request: Request):
    """
    Cloud Tasks Quiz Worker.
    Decrement inflight count on completion.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    session_id = payload.get("sessionId")
    job_id = payload.get("jobId")
    idempotency_key = payload.get("idempotencyKey")
    count = payload.get("count", 5)
    user_id = payload.get("userId") # [Security]
    usage_reserved = bool(payload.get("usageReserved"))

    if not session_id:
        return {"status": "error", "message": "session_id required"}
    
    final_user_id = user_id

    try:
        # [FIX] Initialize DB locally
        from google.cloud import firestore
        import os
        try:
            project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
            db = firestore.Client(project=project_id)
        except Exception as e:
            return {"status": "failed", "error": f"DB Init Failed: {e}"}
        
        doc_ref = db.collection("sessions").document(session_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            return {"status": "skipped"}
            
        data = doc.to_dict()
        if not final_user_id:
            final_user_id = data.get("ownerUserId") or data.get("userId") or data.get("ownerUid")

        transcript = resolve_transcript_text(session_id, data) or ""
        mode = data.get("mode", "lecture")
        derived_ref = doc_ref.collection("derived").document("quiz")

        if idempotency_key:
            derived_snap = derived_ref.get()
            if derived_snap.exists:
                current_key = (derived_snap.to_dict() or {}).get("idempotencyKey")
                if current_key and current_key == idempotency_key:
                    if job_id:
                         db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "completed", "result": "cached"}, merge=True)
                    return {"status": "skipped", "reason": "idempotent_hit"}
        
        if not transcript:
            doc_ref.update({"quizStatus": "failed", "quizError": "Transcript empty", "status": "録音済み"})
            if job_id:
                db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "failed", "errorReason": "Transcript empty"}, merge=True)
            derived_ref.set({
                "status": "failed",
                "errorReason": "Transcript empty",
                "updatedAt": datetime.now(timezone.utc),
                "idempotencyKey": idempotency_key,
            }, merge=True)
            await publish_session_event(session_id, "assets.updated", {"fields": ["quiz"]})
            return {"status": "failed"}

        # generate_quiz, clean_quiz_markdown imported at top level now
        if job_id:
            db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "running"}, merge=True)

        doc_ref.update({
            "quizStatus": "running",
            "quizError": None,
            "status": "テスト生成",
        })

        # [TRIPLE LOCK] Monthly Cost Guard
        if not usage_reserved:
            allowed, meta = await cost_guard.guard_can_consume(final_user_id, "quiz_generated", 1)
            if not allowed:

                 logger.warning(f"[CostGuard] BLOCKED quiz {session_id} for user {final_user_id}. Monthly limit exceeded.")
                 err_msg = "Monthly Quiz limit exceeded (Free: 3, Premium: 1000 AI pool)"
                 doc_ref.update({"quizStatus": "failed", "quizError": err_msg})
                 if job_id:
                     db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "failed", "error": err_msg}, merge=True)
                 derived_ref.set({"status": "failed", "errorReason": err_msg, "updatedAt": datetime.now(timezone.utc)}, merge=True)
                 await publish_session_event(session_id, "assets.updated", {"fields": ["quiz"]})
                 return {"status": "failed", "error": err_msg}

        # ops_logger: job started
        log_job_transition(session_id, "quiz", "started", uid=final_user_id, job_id=job_id)

        quiz_raw = await generate_quiz(transcript, mode=mode, count=count)

        # ops_logger: LLM call completed
        log_llm_event(session_id, "quiz", "completed", uid=final_user_id, model="gemini-1.5-flash")
        quiz_md = clean_quiz_markdown(quiz_raw)
        
        doc_ref.update({
            "quizStatus": "completed",
            "quizMarkdown": quiz_md,
            "quizUpdatedAt": datetime.now(timezone.utc),
            "quizError": None,
            "status": "テスト完了",
        })
        derived_ref.set({
            "status": "succeeded",
            "result": {"markdown": quiz_md, "count": count},
            "modelInfo": {"provider": "vertexai"},
            "updatedAt": datetime.now(timezone.utc),
            "errorReason": None,
            "idempotencyKey": idempotency_key,
        }, merge=True)
        if job_id:
            db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "completed"}, merge=True)

        # ops_logger: job completed
        log_job_transition(session_id, "quiz", "completed", uid=final_user_id, job_id=job_id)
        await publish_session_event(session_id, "assets.updated", {"fields": ["quiz"]})
        return {"status": "completed"}

    except Exception as e:
        logger.exception(f"Quiz generation failed for session {session_id}")

        # ops_logger: LLM/job failed
        log_llm_event(session_id, "quiz", "failed", uid=final_user_id, error_code=ErrorCode.VERTEX_SCHEMA_PARSE_ERROR, error_message=str(e))
        error_str = str(e)
        is_transient = "429" in error_str or "503" in error_str or "ResourceExhausted" in error_str or "ServiceUnavailable" in error_str
        
        if is_transient:
             logger.warning(f"Transient error detected for quiz {session_id}, raising 503 for retry.")
             raise HTTPException(status_code=503, detail="Transient error, retrying...")

        # Safe DB update
        try:
            doc_ref.update({"quizStatus": "failed", "quizError": str(e), "status": "録音済み"})
            if job_id:
                 db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "failed", "errorReason": str(e)}, merge=True)
            derived_ref.set({
                "status": "failed",
                "errorReason": str(e),
                "updatedAt": datetime.now(timezone.utc),
                "idempotencyKey": idempotency_key,
            }, merge=True)
            await publish_session_event(session_id, "assets.updated", {"fields": ["quiz"]})
        except Exception as db_err:
            logger.warning(f"[quiz] Failed to update error status in DB: {db_err}")

        # ops_logger: job failed
        log_job_transition(session_id, "quiz", "failed", uid=final_user_id, job_id=job_id, error_code=ErrorCode.JOB_WORKER_500, error_message=str(e))

        return {"status": "failed", "error": str(e)}

    # finally: removed

@router.post("/internal/tasks/highlights")
async def handle_generate_highlights(request: Request):
    """
    Cloud Tasks から呼び出される Worker エンドポイント。
    ハイライト生成は廃止。
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    session_id = payload.get("sessionId")

    if not session_id:
        logger.error("sessionId is missing")
        return {"status": "error", "message": "sessionId required"}

    from google.cloud import firestore
    import os
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
    db = firestore.Client(project=project_id)

    doc_ref = db.collection("sessions").document(session_id)
    doc_ref.update({
        "highlightsStatus": "failed",
        "highlightsError": "deprecated",
        "highlightsUpdatedAt": datetime.now(timezone.utc)
    })
    return {"status": "failed", "reason": "deprecated"}


@router.post("/internal/tasks/playlist")
async def handle_playlist_task(request: Request):
    """
    端末同期後にプレイリスト（再生リスト）を生成するワーカー。
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    session_id = payload.get("sessionId")
    if not session_id:
        return {"status": "error", "message": "sessionId required"}

    from google.cloud import firestore
    import os
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
    db = firestore.Client(project=project_id)

    doc_ref = db.collection("sessions").document(session_id)
    doc_ref.update({
        "playlistStatus": "failed",
        "playlistError": "deprecated",
        "playlistUpdatedAt": datetime.now(timezone.utc),
    })
    return {"status": "failed", "reason": "deprecated"}


@router.post("/internal/tasks/audio-cleanup")
async def handle_audio_cleanup_task():
    """
    Cloud Scheduler から呼び出される Audio Cleanup Worker エンドポイント。
    """
    from app.jobs.cleanup_audio import cleanup_expired_audio
    try:
        count = cleanup_expired_audio()
        return {"status": "completed", "deletedCount": count}
    except Exception as e:
        logger.exception("Audio cleanup failed")
        return {"status": "failed", "error": str(e)}


@router.post("/internal/tasks/daily-usage-aggregation")
async def handle_daily_usage_aggregation(request: Request):
    """
    Cloud Scheduler から呼び出される Daily Aggregation Worker エンドポイント。
    Payload: {"date": "YYYY-MM-DD"} (optional)
    """
    from app.jobs.aggregate_daily_usage import aggregate_daily_usage
    try:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
            
        target_date = payload.get("date")
        result = aggregate_daily_usage(target_date)
        return result
    except Exception as e:
        logger.exception("Daily usage aggregation failed")
        return {"status": "failed", "error": str(e)}


@router.post("/internal/tasks/account-deletion-sweep")
async def handle_account_deletion_sweep():
    """
    Cloud Scheduler から呼び出される Account Deletion Sweep.
    deleteAfterAt を過ぎた削除リクエストをNUKEに回す。
    """
    from app.task_queue import enqueue_nuke_user_task
    import os

    try:
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
        db = firestore.Client(project=project_id)
    except Exception as e:
        logger.error(f"[AccountDeletion] DB Init Failed: {e}")
        return {"status": "failed", "error": f"DB Init Failed: {e}"}

    now = datetime.now(timezone.utc)
    reqs_ref = db.collection(REQUESTS_COLLECTION)

    try:
        docs = list(reqs_ref.where("deleteAfterAt", "<=", now).stream())
    except Exception as e:
        logger.error(f"[AccountDeletion] Query failed: {e}")
        return {"status": "failed", "error": f"Query Failed: {e}"}

    enqueued = 0
    failed = 0

    for doc in docs:
        data = doc.to_dict() or {}
        if data.get("status") != "requested":
            continue
        uid = data.get("uid") or doc.id
        if not uid:
            continue
        try:
            enqueue_nuke_user_task(uid)
            doc.reference.update(
                {
                    "status": "enqueued",
                    "nukeEnqueuedAt": now,
                    "updatedAt": now,
                }
            )
            enqueued += 1
        except Exception as e:
            logger.error(f"[AccountDeletion] Failed to enqueue {uid}: {e}")
            try:
                doc.reference.update(
                    {
                        "status": "failed",
                        "lastError": str(e),
                        "updatedAt": now,
                    }
                )
            except Exception:
                pass
            failed += 1

    return {"status": "completed", "enqueued": enqueued, "failed": failed}


@router.post("/internal/tasks/qa")
async def handle_qa_task(request: Request):
    """
    Cloud Tasks から呼び出される QA Worker エンドポイント。
    Payload: {"sessionId": str, "question": str, "userId": str, "qaId": str}
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    session_id = payload.get("sessionId")
    question = payload.get("question")
    user_id = payload.get("userId")
    qa_id = payload.get("qaId")
    
    if not all([session_id, question, qa_id]):
        raise HTTPException(status_code=400, detail="Missing required fields")
    
    # [FIX] Initialize DB locally with STANDALONE Client
    from google.cloud import firestore
    import os
    try:
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
        db = firestore.Client(project=project_id)
    except Exception as e:
        return {"status": "failed", "error": f"DB Init Failed: {e}"}

    doc_ref = db.collection("sessions").document(session_id)
    qa_ref = doc_ref.collection("qa_results").document(qa_id)
    
    final_user_id = user_id

    try:
        doc = doc_ref.get()
        if not doc.exists:
            qa_ref.set({"status": "failed", "error": "Session not found", "updatedAt": datetime.now(timezone.utc)})
            return {"status": "failed", "error": "Session not found"}
        
        data = doc.to_dict()
        # [Security] Resolve User ID if missing
        if not final_user_id:
            final_user_id = data.get("ownerUserId") or data.get("userId") or data.get("ownerUid")

        transcript = resolve_transcript_text(session_id, data) or ""
        
        if not transcript:
            qa_ref.set({"status": "failed", "error": "Transcript empty", "updatedAt": datetime.now(timezone.utc)})
            return {"status": "failed", "error": "Transcript empty"}
        
        qa_ref.set({"status": "running", "question": question, "updatedAt": datetime.now(timezone.utc)}, merge=True)
        
        # [TRIPLE LOCK] Monthly Cost Guard
        allowed = await cost_guard.guard_can_consume(final_user_id, "llm_calls", 1)
        if not allowed:
             logger.warning(f"[CostGuard] BLOCKED qa {session_id} for user {final_user_id}. Monthly limit exceeded.")
             err_msg = "Monthly LLM limit exceeded (1000 calls)"
             qa_ref.set({"status": "failed", "error": err_msg, "updatedAt": datetime.now(timezone.utc)}, merge=True)
             return {"status": "failed", "error": err_msg}

        # ops_logger: job started
        log_job_transition(session_id, "qa", "started", uid=final_user_id, job_id=qa_id)

        result = await answer_question(transcript, question, data.get("mode", "lecture"))

        # ops_logger: LLM call completed
        log_llm_event(session_id, "qa", "completed", uid=final_user_id, model="gemini-1.5-flash")

        answer = result.get("answer", "")
        citations = result.get("citations", [])
        
        qa_ref.set({
            "status": "completed",
            "answer": answer,
            "citations": citations,
            "updatedAt": datetime.now(timezone.utc),
        }, merge=True)
        
        # ops_logger: job completed
        log_job_transition(session_id, "qa", "completed", uid=final_user_id, job_id=qa_id)

        return {"status": "completed", "qaId": qa_id}
        
    except Exception as e:
        logger.exception(f"QA task failed for session {session_id}")

        # ops_logger: LLM/job failed
        log_llm_event(session_id, "qa", "failed", uid=final_user_id, error_code=ErrorCode.VERTEX_SCHEMA_PARSE_ERROR, error_message=str(e))

        qa_ref.set({"status": "failed", "error": str(e), "updatedAt": datetime.now(timezone.utc)}, merge=True)

        # ops_logger: job failed
        log_job_transition(session_id, "qa", "failed", uid=final_user_id, job_id=qa_id, error_code=ErrorCode.JOB_WORKER_500, error_message=str(e))

        return {"status": "failed", "error": str(e)}


@router.post("/internal/tasks/translate")
async def handle_translate_task(request: Request):
    """
    Cloud Tasks から呼び出される Translate Worker エンドポイント。
    Payload: {"sessionId": str, "targetLanguage": str, "userId": str}
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    session_id = payload.get("sessionId")
    target_language = payload.get("targetLanguage")
    user_id = payload.get("userId")
    
    if not all([session_id, target_language]):
        raise HTTPException(status_code=400, detail="Missing required fields")
    
    # [FIX] Initialize DB locally with STANDALONE Client
    from google.cloud import firestore
    import os
    try:
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
        db = firestore.Client(project=project_id)
    except Exception as e:
        return {"status": "failed", "error": f"DB Init Failed: {e}"}

    doc_ref = db.collection("sessions").document(session_id)
    trans_ref = db.collection("translations").document(session_id)
    
    final_user_id = user_id

    try:
        doc = doc_ref.get()
        if not doc.exists:
            trans_ref.set({"status": "failed", "error": "Session not found", "updatedAt": datetime.now(timezone.utc)})
            return {"status": "failed", "error": "Session not found"}
        
        data = doc.to_dict()
        # [Security] Resolve User ID if missing
        if not final_user_id:
            final_user_id = data.get("ownerUserId") or data.get("userId") or data.get("ownerUid")

        transcript = resolve_transcript_text(session_id, data) or ""
        
        if not transcript:
            trans_ref.set({"status": "failed", "error": "Transcript empty", "updatedAt": datetime.now(timezone.utc)})
            return {"status": "failed", "error": "Transcript empty"}
        
        trans_ref.set({
            "status": "running", 
            "language": target_language, 
            "sessionId": session_id,
            "updatedAt": datetime.now(timezone.utc)
        }, merge=True)
        
        # ops_logger: job started
        log_job_transition(session_id, "translate", "started", uid=final_user_id)

        translated_text = await translate_text(transcript, target_language)

        # ops_logger: LLM call completed
        log_llm_event(session_id, "translate", "completed", uid=final_user_id, model="gemini-1.5-flash")
        
        trans_ref.set({
            "status": "completed",
            "language": target_language,
            "translatedText": translated_text,
            "sessionId": session_id,
            "createdAt": datetime.now(timezone.utc),
            "updatedAt": datetime.now(timezone.utc),
        }, merge=True)
        
        # ops_logger: job completed
        log_job_transition(session_id, "translate", "completed", uid=final_user_id)

        return {"status": "completed", "sessionId": session_id, "language": target_language}
        
    except Exception as e:
        logger.exception(f"Translate task failed for session {session_id}")

        # ops_logger: LLM/job failed
        log_llm_event(session_id, "translate", "failed", uid=final_user_id, error_code=ErrorCode.VERTEX_SCHEMA_PARSE_ERROR, error_message=str(e))

        trans_ref.set({"status": "failed", "error": str(e), "updatedAt": datetime.now(timezone.utc)}, merge=True)

        # ops_logger: job failed
        log_job_transition(session_id, "translate", "failed", uid=final_user_id, error_code=ErrorCode.JOB_WORKER_500, error_message=str(e))

        return {"status": "failed", "error": str(e)}


@router.post("/internal/tasks/transcribe")
async def handle_transcribe_task(request: Request):
    try:
        payload = await request.json()
        uid = payload.get("userId")
    except:
        uid = None

    try:
        return await _handle_transcribe_task_core(request)
    finally:
        if uid:
            try:
                await usage_logger.decrement_inflight(uid, "transcribe")
            except Exception as e:
                logger.error(f"Failed to decrement inflight transcribe: {e}")

async def _handle_transcribe_task_core(request: Request):
    """
    Cloud Tasks から呼び出される Transcribe Worker エンドポイント。
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    session_id = payload.get("sessionId")
    job_id = payload.get("jobId")
    idempotency_key = payload.get("idempotencyKey")
    force = payload.get("force", False)
    engine = payload.get("engine", "google")
    user_id = payload.get("userId") # [Security]
    
    if not session_id:
        return {"status": "error", "message": "sessionId required"}
    
    final_user_id = user_id

    logger.info(f"Processing Transcribe Task for {session_id} (engine: {engine}, force: {force})")

    # [FIX] Initialize DB locally with STANDALONE Client
    from google.cloud import firestore
    import os
    try:
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
        db = firestore.Client(project=project_id)
    except Exception as e:
        return {"status": "failed", "error": f"DB Init Failed: {e}"}

    doc_ref = db.collection("sessions").document(session_id)
    job_ref = doc_ref.collection("jobs").document(job_id) if job_id else None

    try:
        doc = doc_ref.get()
        if not doc.exists:
            logger.warning(f"Session {session_id} not found.")
            return {"status": "skipped", "reason": "not_found"}

        data = doc.to_dict()
        
        # [Security] Resolve User ID if missing
        if not final_user_id:
            final_user_id = data.get("ownerUserId") or data.get("userId") or data.get("ownerUid")

        # Check if already processed (Idempotency) - though typically transcribe is expensive so we might rely on status check too
        if idempotency_key and job_ref:
            # Check existing job status? 
            # Simplified: Proceed for now mostly.
            pass

        # [POLICY] STRICT Transcription Mode Enforcement
        # - Cloud Mode (cloud_google): ONLY use Google STT V2 Streaming. NEVER batch transcribe.
        # - On-Device Mode: ONLY use SFSpeechRecognizer/sherpa-onnx. No cloud.
        transcription_mode = data.get("transcriptionMode") or ""
        transcript_source = data.get("transcriptSource") or ""
        existing_transcript = data.get("transcriptText") or ""
        
        # Cloud mode: Streaming is the ONLY source. Skip batch entirely.
        if transcription_mode == "cloud_google" or transcript_source == "cloud_streaming_v2":
            logger.info(f"[Transcribe] SKIPPED for session {session_id}: Cloud mode uses streaming only (policy). Has {len(existing_transcript)} chars.")
            if job_ref:
                job_ref.set({
                    "status": "completed",
                    "result": "skipped_cloud_mode_policy",
                    "reason": "Cloud mode uses only live streaming transcription",
                    "completedAt": datetime.now(timezone.utc)
                }, merge=True)
            return {"status": "skipped", "reason": "cloud_mode_streaming_only", "chars": len(existing_transcript)}
        
        # On-Device mode: This job should not be queued in the first place.
        # But if it is, reject it since on-device doesn't use cloud batch.
        if transcription_mode in ["on_device", "local", "offline"]:
            logger.info(f"[Transcribe] SKIPPED for session {session_id}: On-Device mode does not use cloud batch (policy).")
            if job_ref:
                job_ref.set({
                    "status": "completed",
                    "result": "skipped_on_device_mode_policy",
                    "reason": "On-Device mode uses only local transcription",
                    "completedAt": datetime.now(timezone.utc)
                }, merge=True)
            return {"status": "skipped", "reason": "on_device_mode_local_only"}

        audio_info = data.get("audio") or {}
        gcs_path = audio_info.get("gcsPath") or data.get("audioPath") # audioPath usually is 'sessions/...', need full GS?
        
        # NOTE: data.get("audioPath") is often a relative path "sessions/{uid}/{sid}.m4a"
        # We need "gs://bucket/..."
        # If gcsPath is explicitly set in audio map, use it.
        # Else construct it from BUCKET + audioPath if possible, OR fail if we rely on gcs_path being absolute.
        
        if not gcs_path:
             # Fallback if audioPath exists
             rel_path = data.get("audioPath")
             bucket = AUDIO_BUCKET_NAME
             if rel_path and not rel_path.startswith("gs://"):
                 gcs_path = f"gs://{bucket}/{rel_path}"
             elif rel_path:
                 gcs_path = rel_path
                 
        if not gcs_path:
             err = "No audio path found"
             doc_ref.update({"transcriptionStatus": "failed", "transcriptionError": err})
             if job_ref: job_ref.set({"status": "failed", "errorReason": err}, merge=True)
             return {"status": "failed", "error": err}

        # Update running status
        if job_ref: job_ref.set({"status": "running"}, merge=True)
        
        # [Security] Duration Guard (120m)
        duration = float(data.get("durationSec") or 0.0)
        if duration > 7200:
             logger.error(f"[Security] Skipping batch transcription for session {session_id}: Duration {duration}s exceeds 2h limit.")
             err_msg = "Duration exceeds 2 hour limit"
             doc_ref.update({
                 "transcriptionStatus": "failed",
                 "transcriptionError": err_msg
             })
             if job_ref: job_ref.set({"status": "failed", "error": err_msg}, merge=True)
             return

        # [TRIPLE LOCK] Monthly Cost Guard
        if duration <= 0:
             # Safety: Block unknown duration to prevent billing leak
             logger.error(f"[CostGuard] Blocking session {session_id} due to unknown duration ({duration}s).")
             err_msg = "Duration metadata missing (Cost Guard)"
             doc_ref.update({"transcriptionStatus": "failed", "transcriptionError": err_msg})
             if job_ref: job_ref.set({"status": "failed", "error": err_msg}, merge=True)
             return

        allowed = await cost_guard.guard_can_consume(final_user_id, "cloud_stt_sec", duration)
        if not allowed:
             logger.warning(f"[CostGuard] BLOCKED session {session_id} for user {final_user_id}. Monthly limit exceeded.")
             err_msg = "Monthly transcription limit exceeded (100h)"
             doc_ref.update({"transcriptionStatus": "failed", "transcriptionError": err_msg})
             if job_ref: job_ref.set({"status": "failed", "error": err_msg}, merge=True)
             # Should we return or raise? Return prevents retryloop.
             return

        doc_ref.update({
             "transcriptionStatus": "running",
             "transcriptionEngine": engine,
             "transcriptionUpdatedAt": datetime.now(timezone.utc)
        })

        # ops_logger: job started
        log_job_transition(session_id, "transcribe", "started", uid=final_user_id, job_id=job_id)

        # Execute Transcription
        from app.services.google_speech import transcribe_audio_google_with_segments

        # ops_logger: STT started
        log_stt_event(session_id, "started", uid=final_user_id)

        transcript_text, segments = transcribe_audio_google_with_segments(
            gcs_path, language_code="ja-JP"
        )

        # ops_logger: STT completed
        log_stt_event(session_id, "completed", uid=final_user_id, duration_sec=duration)

        now = datetime.now(timezone.utc)

        # Save Artifact
        artifact_ref = doc_ref.collection("artifacts").document("transcript_google")
        artifact_ref.set({
            "text": transcript_text,
            "source": f"cloud_{engine}",
            "modelInfo": {"engine": f"google_speech_v2"},
            "createdAt": now,
            "type": "transcript",
        })

        # Update Session
        # Only overwrite main transcript if mode is cloud or if main is empty
        transcription_mode = data.get("transcriptionMode", "cloud_google")
        updates = {
             "transcriptionStatus": "completed",
             "transcriptionUpdatedAt": now,
             "transcriptionError": None,
             "batchRetranscribeState": "completed", # [NEW]
             "batchRetranscribeUsed": True,         # [NEW] Lock after success
        }
        
        should_update_main = (transcription_mode == "cloud_google") or (not data.get("transcriptText")) or force
        
        if should_update_main:
             updates["transcriptText"] = transcript_text
             updates["hasTranscript"] = True
             updates["transcriptSource"] = f"cloud_{engine}"
             if segments:
                 updates["segments"] = segments

        # LOG USAGE for Billing
        try:
            # Determine duration
            usage_sec = float(data.get("durationSec") or 0.0)
            if usage_sec == 0.0 and segments:
                # Try to get from last segment
                last = segments[-1]
                usage_sec = float(last.get("end", 0.0) or last.get("endSec", 0.0))
            
            if usage_sec > 0:
                # Check for ownerUid or userId
                uid = data.get("ownerUserId") or data.get("userId") or "unknown_task_user"
                if not final_user_id: final_user_id = uid # Resolve if missing
                
                # app.services.usage is already imported
                await usage_logger.log(
                    user_id=uid,
                    session_id=session_id,
                    feature="transcribe",
                    event_type="success",
                    payload={
                        "recording_sec": usage_sec,
                        "type": "cloud",
                        "mode": data.get("mode"),
                        "engine": engine
                    }
                )
        except Exception as e:
            logger.error(f"Failed to log usage for session {session_id}: {e}")
        
        doc_ref.update(updates)
        if job_ref: job_ref.set({"status": "completed"}, merge=True)
        
        logger.info(f"Transcription Success for {session_id}")

        # ops_logger: job completed
        log_job_transition(session_id, "transcribe", "completed", uid=final_user_id, job_id=job_id)

        # [NEW] Free Plan: Auto-trigger Summary and Quiz
        # "Free 1 time = Cloud STT + Summary + Quiz"
        # Since they consumed their 1 credit to start this Cloud STT, we maximize their value.
        uid = data.get("ownerUserId") or data.get("userId")
        if uid:
            try:
                # Check plan
                user_doc = db.collection("users").document(uid).get()
                if user_doc.exists and user_doc.to_dict().get("plan", "free") == "free":
                    from app.task_queue import enqueue_summarize_task, enqueue_quiz_task
                    
                    logger.info(f"[FreePlan] Auto-triggering Summary/Quiz for {session_id}")
                    enqueue_summarize_task(session_id, user_id=uid)
                    enqueue_quiz_task(session_id, count=3, user_id=uid) # Default 3 questions for free?
            except Exception as e:
                logger.error(f"[FreePlan] Auto-trigger failed: {e}")

        # [NEW] Log Usage for Cloud STT
        try:
             # Calculate duration if possible (from metadata or audio file size?)
             # Here we rely on data.get("durationSec") or audio_info
             # If not available, we might log 0 or approximate?
             duration_sec = data.get("durationSec")
             if not duration_sec and audio_info.get("durationSec"):
                 duration_sec = audio_info.get("durationSec")
             
             if duration_sec:
                 from app.services.usage import usage_logger
                 # We need user_id. 'ownerUserId' (new) or 'userId' (old)
                 uid = data.get("ownerUserId") or data.get("userId") or data.get("ownerUid")
                 if uid:
                     # Since this is a background task, await might need loop?
                     # No, this is an async def, so await is fine.
                     await usage_logger.log(
                        user_id=uid,
                        session_id=session_id,
                        feature="transcribe",
                        event_type="success",
                        payload={
                            "recording_sec": float(duration_sec),
                            "type": "cloud"
                        }
                     )
        except Exception as e:
            logger.warning(f"Failed to log usage for transcribe task {session_id}: {e}")

        # Trigger downstream
        if should_update_main and transcript_text:
             from app.task_queue import enqueue_summarize_task, enqueue_quiz_task
             enqueue_summarize_task(session_id, user_id=final_user_id)
             enqueue_quiz_task(session_id, user_id=final_user_id)

        return {"status": "completed"}

    except Exception as e:
        logger.exception(f"Transcribe task failed for {session_id}")
        error_msg = str(e)

        # ops_logger: STT failed
        log_stt_event(session_id, "failed", uid=final_user_id, error_code=ErrorCode.STT_OPERATION_FAILED, error_message=error_msg)

        doc_ref.update({
            "transcriptionStatus": "failed",
            "transcriptionError": error_msg,
            "transcriptionUpdatedAt": datetime.now(timezone.utc),
            "batchRetranscribeState": "failed", # [NEW]
        })
        if job_ref: job_ref.set({"status": "failed", "errorReason": error_msg}, merge=True)

        # ops_logger: job failed
        log_job_transition(session_id, "transcribe", "failed", uid=final_user_id, job_id=job_id, error_code=ErrorCode.JOB_WORKER_500, error_message=error_msg)

        return {"status": "failed", "error": error_msg}

    # finally: removed

# @router.post("/internal/tasks/transcribe")
async def handle_transcribe_task_deprecated(request: Request):
    """
    Cloud Tasks from enqueue_transcribe_task.
    Executes Google STT or other engines.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    session_id = payload.get("sessionId")
    engine = payload.get("engine", "google")
    force = payload.get("force", False)
    job_id = payload.get("jobId")

    if not session_id:
        raise HTTPException(status_code=400, detail="Missing sessionId")
    
    # [FIX] Initialize DB locally with STANDALONE Client
    from google.cloud import firestore
    import os
    try:
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
        db = firestore.Client(project=project_id)
    except Exception as e:
        return {"status": "failed", "error": f"DB Init Failed: {e}"}

    doc_ref = db.collection("sessions").document(session_id)
    doc = doc_ref.get()
    
    if not doc.exists:
        return {"status": "skipped", "reason": "not_found"}
        
    data = doc.to_dict()
    transcription_mode = data.get("transcriptionMode", "cloud_google")
    
    if job_id:
        db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "running"}, merge=True)
    
    # Only proceed if engine matches or we want to force it
    # If engine='google', we just run it.
    
    if engine == "google":
        try:
            from app.services.google_speech import transcribe_audio_google_with_segments
            
            audio_info = data.get("audio") or {}
            gcs_path = audio_info.get("gcsPath") or data.get("audioPath")
            
            if not gcs_path:
                 if job_id:
                      db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "failed", "errorReason": "No audio path found"}, merge=True)
                 return {"status": "failed", "error": "No audio path found"}
            
            # Update status
            doc_ref.update({
                "transcriptionStatus": "running", 
                "transcriptionEngine": "google"
            })
            
            # Execute STT
            transcript_text, segments = transcribe_audio_google_with_segments(
                gcs_path, language_code="ja-JP"
            )
            
            now = datetime.now(timezone.utc)
            
            # Save Artifact
            artifact_ref = doc_ref.collection("artifacts").document("transcript_google")
            artifact_ref.set({
                "text": transcript_text,
                "source": "cloud_google",
                "modelInfo": {"engine": "google_speech_v1"},
                "createdAt": now,
                "type": "transcript",
                "status": "ready" # Explicitly mark ready
            })
            
            updates = {
                "transcriptionStatus": "completed",
                "transcriptionUpdatedAt": now,
            }
            
            # If Main Mode is Google, update main transcript
            if transcription_mode == "cloud_google" or not data.get("transcriptText"):
                updates["transcriptText"] = transcript_text
                if segments:
                    updates["segments"] = segments
                
            doc_ref.update(updates)
            
            if job_id:
                db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({
                    "status": "completed",
                    "result": {"transcript": transcript_text},
                    "transcriptText": transcript_text
                }, merge=True)
            
            # Trigger Auto-Summary if needed
            # Only if we just updated the main transcript
            if updates.get("transcriptText"):
                 from app.task_queue import enqueue_summarize_task, enqueue_quiz_task
                 # Use data.get("ownerUserId") or data.get("userId") for uid
                 uid = data.get("ownerUserId") or data.get("userId")
                 enqueue_summarize_task(session_id, user_id=uid)
                 enqueue_quiz_task(session_id, user_id=uid)

            logger.info(f"Transcribe task completed for {session_id}, job_id={job_id}")
            return {"status": "completed", "engine": "google"}
            
        except Exception as e:
            logger.exception(f"Transcribe task failed for {session_id}")
            doc_ref.update({
                "transcriptionStatus": "failed",
                "transcriptionError": str(e)
            })
            if job_id:
                db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "failed", "errorReason": str(e)}, merge=True)
            return {"status": "failed", "error": str(e)}

    return {"status": "skipped", "reason": f"unknown_engine: {engine}"}

@router.post("/internal/tasks/cleanup_sessions")
async def handle_cleanup_sessions_task(request: Request):
    """
    [TRIPLE LOCK] Cleanup Worker.
    Deletes old sessions if user exceeds SERVER_SESSION_LIMIT (300).
    Input: { userId: "..." }
    """
    try:
        payload = await request.json()
        user_id = payload.get("userId")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if not user_id:
        return {"status": "error", "message": "userId required"}

    SERVER_SESSION_LIMIT = 300
    SAFE_WINDOW_DAYS = 7  # Don't delete sessions created in last 7 days

    logger.info(f"[Cleanup] Starting session cleanup for {user_id}")
    
    # [FIX] Initialize DB locally
    from google.cloud import firestore
    import os
    try:
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
        db = firestore.Client(project=project_id)
    except Exception as e:
        logger.error(f"Failed to init local DB: {e}")
        return {"status": "failed", "error": f"DB Init Failed: {e}"}

    # 1. Count Active Sessions
    sessions_ref = db.collection("sessions")
    docs = sessions_ref.where("ownerUid", "==", user_id).stream()
    
    active_sessions = []
    for d in docs:
        d_dict = d.to_dict()
        if d_dict.get("deletedAt") is None:
            active_sessions.append((d.id, d_dict))
            
    current_count = len(active_sessions)
    if current_count <= SERVER_SESSION_LIMIT:
        logger.info(f"[Cleanup] User {user_id} has {current_count} sessions. Within limit ({SERVER_SESSION_LIMIT}).")
        
        # Update user count just in case
        try:
             db.collection("users").document(user_id).update({
                 "serverSessionCount": current_count,
                 "serverSessionLimit": SERVER_SESSION_LIMIT
             })
        except Exception as db_err:
            logger.warning(f"[cleanup] Failed to update user session count: {db_err}")
        return {"status": "skipped", "count": current_count}

    # 2. Exceeds limit -> Identify cleanup candidates
    excess_count = current_count - SERVER_SESSION_LIMIT
    logger.info(f"[Cleanup] User {user_id} has {current_count} sessions. Excess: {excess_count} (Deleting {excess_count})")
    
    # Sort: Oldest first (Delete target)
    # Priority: lastOpenedAt (if available) -> updatedAt -> createdAt
    def sort_key(item):
        sid, data = item
        ts = data.get("lastOpenedAt") or data.get("updatedAt") or data.get("createdAt")
        if not ts: return datetime.min
        if isinstance(ts, str):
             try: return datetime.fromisoformat(ts.replace('Z', '+00:00'))
             except: return datetime.min
        return ts

    sorted_sessions = sorted(active_sessions, key=sort_key)
    
    now = datetime.now(timezone.utc)
    deleted_count = 0
    
    batch = db.batch()
    
    for sid, data in sorted_sessions:
        if deleted_count >= excess_count:
            break
            
        # Protection Rules (Safe Window)
        created_at = data.get("createdAt")
        if created_at:
             if isinstance(created_at, str):
                 try: created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                 except: pass
             if isinstance(created_at, datetime):
                 age = now - created_at
                 if age.days < SAFE_WINDOW_DAYS:
                     # Too new, skip deletion even if limit exceeded?
                     # Ideally we should find another candidate.
                     # But if ALL are new, we might be forced to delete or skip.
                     # "Triple Lock" priority is COST > User Convenience.
                     # Start with skipping, but if truly overflowed, we should delete.
                     # For Phase 3, let's respect window but log warning.
                     continue

        ref = db.collection("sessions").document(sid)
        batch.update(ref, {
             "deletedAt": now,
             "status": "deleted_by_limit_cleanup",
             "audioPath": None, # Wipe reference
             "transcriptText": None, # Wipe text
             "summaryMarkdown": None,
             "quizMarkdown": None,
             "playlist": None
        })
        deleted_count += 1
        
    if deleted_count > 0:
        batch.commit()
        logger.info(f"[Cleanup] Deleted {deleted_count} sessions for {user_id}")
        
    # Update final count
    new_count = current_count - deleted_count
    try:
        db.collection("users").document(user_id).update({
            "serverSessionCount": new_count
        })
    except Exception as db_err:
        logger.warning(f"[cleanup] Failed to update user session count after cleanup: {db_err}")

    return {"status": "completed", "deleted": deleted_count}


@router.post("/internal/tasks/nuke_user")
async def handle_nuke_user_task(request: Request):
    """
    [DANGER] Completely wipe a user account and all associated data.
    1. List all sessions
    2. For each session:
       - Delete GCS Objects (Audio/Images)
       - Delete Subcollections (jobs, derived, etc)
       - Delete Session Doc
    3. Delete User Data (sessionMeta, Claims, User Doc, Auth)
    """
    try:
        payload = await request.json()
        user_id = payload.get("userId")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if not user_id:
        return {"status": "error", "message": "userId required"}

    logger.warning(f"[NUKE] Starting Account Deletion for {user_id}")
    
    # Init DB & Storage
    from google.cloud import firestore, storage
    import os
    from app.firebase import AUDIO_BUCKET_NAME, MEDIA_BUCKET_NAME
    
    try:
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
        db = firestore.Client(project=project_id)
        storage_client = storage.Client(project=project_id)
    except Exception as e:
        logger.error(f"[NUKE] DB/Storage Init Failed: {e}")
        return {"status": "failed", "error": f"Init Failed: {e}"}

    # --- Helper: Recursive Delete Collection ---
    def delete_collection(coll_ref, batch_size=50):
        docs = list(coll_ref.limit(batch_size).stream())
        deleted = 0

        while len(docs) > 0:
            batch = db.batch()
            for doc in docs:
                batch.delete(doc.reference)
            batch.commit()
            deleted += len(docs)
            logger.info(f"[NUKE] Deleted {len(docs)} docs from {coll_ref.path}")
            docs = list(coll_ref.limit(batch_size).stream())  # Fetch next batch

    # --- Step 1: List All Sessions ---
    sessions_ref = db.collection("sessions")
    # Query by both ownerUid and userId (legacy) to be safe
    # But mostly ownerUid is the new standard
    # To be extremely thorough, we'll do two passes or one large query if possible
    # We will iterate and handle each session.
    
    # Strategy: Get IDs first
    session_ids = set()
    for field in ["ownerUid", "userId", "ownerUserId"]:
        docs = sessions_ref.where(field, "==", user_id).stream()
        for d in docs:
            session_ids.add(d.id)
            
    logger.info(f"[NUKE] Found {len(session_ids)} sessions to delete.")

    audio_bucket = storage_client.bucket(AUDIO_BUCKET_NAME)
    media_bucket = storage_client.bucket(MEDIA_BUCKET_NAME)

    for sid in session_ids:
        try:
            doc_ref = sessions_ref.document(sid)
            
            # A. GCS Cleanup (Prefix-based)
            # Audio Bucket: sessions/{sid}/
            blobs_audio = list(audio_bucket.list_blobs(prefix=f"sessions/{sid}/"))
            for b in blobs_audio: b.delete()
            
            # Media Bucket: sessions/{sid}/ (Images)
            blobs_media = list(media_bucket.list_blobs(prefix=f"sessions/{sid}/"))
            for b in blobs_media: b.delete()
            
            logger.info(f"[NUKE] Wiped GCS for session {sid}")

            # B. Subcollections
            sub_colls = ["jobs", "derived", "calendar_sync", "transcript_chunks", "vectors", "artifacts"]
            for sub in sub_colls:
                delete_collection(doc_ref.collection(sub))
                
            # C. Delete Session Doc
            doc_ref.delete()
            
        except Exception as e:
             logger.error(f"[NUKE] Failed to delete session {sid}: {e}")
             # Continue to next session even if one fails
    
    logger.info("[NUKE] All sessions wiped.")

    # --- Step 2: Delete User Subcollections ---
    user_ref = db.collection("users").document(user_id)
    delete_collection(user_ref.collection("sessionMeta"))
    delete_collection(user_ref.collection("devices")) # If exists

    # --- Step 3: Delete Username Claim ---
    # Need to read user doc first to know username
    user_doc = user_ref.get()
    if user_doc.exists:
        u_data = user_doc.to_dict()
        username = u_data.get("username") or u_data.get("usernameLower")
        if username:
            try:
                db.collection("username_claims").document(username).delete()
                logger.info(f"[NUKE] Released username {username}")
            except Exception as e:
                logger.error(f"[NUKE] Failed username release: {e}")

    # --- Step 4: Delete User Doc ---
    user_ref.delete()
    logger.info(f"[NUKE] User doc {user_id} deleted.")

    # --- Step 5: Firebase Auth ---
    try:
        from firebase_admin import auth
        auth.delete_user(user_id)
        logger.info(f"[NUKE] Firebase Auth user {user_id} deleted.")
    except Exception as e:
        # Ignore if already deleted
        logger.warning(f"[NUKE] Auth deletion warning (might be already deleted): {e}")

    # --- Step 6: Finalize deletion request/lock ---
    try:
        req_ref = db.collection(REQUESTS_COLLECTION).document(user_id)
        req_doc = req_ref.get()
        if req_doc.exists:
            req_data = req_doc.to_dict() or {}
            email_lower = req_data.get("emailLower")
            provider_id = req_data.get("providerId") or req_data.get("provider")
            if email_lower and provider_id:
                lock_id = deletion_lock_id(email_lower, provider_id)
                db.collection(LOCKS_COLLECTION).document(lock_id).delete()
            now = datetime.now(timezone.utc)
            req_ref.update(
                {
                    "status": "deleted",
                    "deletedAt": now,
                    "updatedAt": now,
                }
            )
    except Exception as e:
        logger.warning(f"[NUKE] Failed to finalize deletion request for {user_id}: {e}")

    logger.info(f"[NUKE] SUCCESS. Account {user_id} is gone.")
    return {"status": "completed", "user_id": user_id}
