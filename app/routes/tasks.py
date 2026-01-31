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
    generate_playlist_timeline,
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
from app.services.app_config import is_feature_enabled
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
    # [FeatureGate] Check if summarization is enabled
    if not is_feature_enabled("summarization"):
        logger.warning("[FeatureGate] Summarization feature is disabled, skipping task")
        return {"status": "skipped", "reason": "feature_disabled"}

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

    # [FIX] Initialize cost_guard variables at top level for exception handler scope
    cost_guard_id = None
    cost_guard_mode = "user"
    has_consumed = False

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

        # [FIX] Resolve account ID for cost guard
        owner_account_id = data.get("ownerAccountId")
        cost_guard_id = owner_account_id or final_user_id
        cost_guard_mode = "account" if owner_account_id else "user"
        has_consumed = False  # Track if we've consumed quota for refund on failure

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
            "startedAt": datetime.now(timezone.utc), # [NEW] Track start time for robust retry
            "idempotencyKey": idempotency_key,
            "jobId": job_id,
        }, merge=True)

        # [FIX] CostGuard - Count usage when task actually executes (not just at API layer)
        if not usage_reserved and cost_guard_id:
            allowed, _meta = await cost_guard.guard_can_consume(cost_guard_id, "summary_generated", 1, mode=cost_guard_mode)
            if not allowed:
                logger.warning(f"[CostGuard] BLOCKED summary {session_id} for {cost_guard_id}. Monthly limit exceeded.")
                err_msg = "Monthly summary limit exceeded"
                doc_ref.update({"summaryStatus": "locked", "summaryError": err_msg})
                derived_ref.set({"status": "locked", "errorReason": err_msg, "updatedAt": datetime.now(timezone.utc)}, merge=True)
                return {"status": "blocked", "error": err_msg}
            has_consumed = True
            logger.info(f"[CostGuard] Reserved summary_generated for {cost_guard_id} ({cost_guard_mode})")

        # ops_logger: job started
        logger.info(f"Starting summary task for {session_id} job={job_id}")
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
            "jobId": job_id,
        }, merge=True)
        
        logger.info(f"Successfully summarized session {session_id}")
        if job_id:
            db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "completed"}, merge=True)

        # ops_logger: job completed
        log_job_transition(session_id, "summarize", "completed", uid=final_user_id, job_id=job_id)
        # Log usage success
        await usage_logger.log(user_id=final_user_id, feature="summary", event_type="success", session_id=session_id)
        
        await publish_session_event(session_id, "assets.updated", {"fields": ["summary"]})
        return {"status": "completed"}

    except Exception as e:
        # [FIX] Refund quota on failure
        if has_consumed and cost_guard_id:
            await cost_guard.refund_consumption(cost_guard_id, "summary_generated", 1, mode=cost_guard_mode)
            logger.info(f"[CostGuard] Refunded summary_generated for {cost_guard_id} due to failure")

        logger.exception(f"Summarization failed for session {session_id}")

        # ops_logger: LLM/job failed
        log_llm_event(session_id, "summary", "failed", uid=final_user_id, error_code=ErrorCode.VERTEX_SCHEMA_PARSE_ERROR, error_message=str(e))
        # Log usage error
        if final_user_id:
            await usage_logger.log(user_id=final_user_id, feature="summary", event_type="error", session_id=session_id)
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
        # [FIX] Use session-based idempotency key to prevent duplicate consumption
        uid = final_user_id
        enqueue_summarize_task(session_id, user_id=uid, idempotency_key=f"auto_summary:{session_id}")
        enqueue_quiz_task(session_id, user_id=uid, idempotency_key=f"auto_quiz:{session_id}")
        
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
    # [FeatureGate] Check if quiz feature is enabled
    if not is_feature_enabled("quiz"):
        logger.warning("[FeatureGate] Quiz feature is disabled, skipping task")
        return {"status": "skipped", "reason": "feature_disabled"}

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

    # [FIX] Initialize cost_guard variables at top level for exception handler scope
    cost_guard_id = None
    cost_guard_mode = "user"
    has_consumed = False

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

        # [FIX] Resolve account ID for cost guard
        owner_account_id = data.get("ownerAccountId")
        cost_guard_id = owner_account_id or final_user_id
        cost_guard_mode = "account" if owner_account_id else "user"
        has_consumed = False  # Track if we've consumed quota for refund on failure

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

        # [FIX] CostGuard - Count usage when task actually executes (not just at API layer)
        if not usage_reserved and cost_guard_id:
            allowed, _meta = await cost_guard.guard_can_consume(cost_guard_id, "quiz_generated", 1, mode=cost_guard_mode)
            if not allowed:
                logger.warning(f"[CostGuard] BLOCKED quiz {session_id} for {cost_guard_id}. Monthly limit exceeded.")
                err_msg = "Monthly quiz limit exceeded"
                doc_ref.update({"quizStatus": "locked", "quizError": err_msg})
                derived_ref.set({"status": "locked", "errorReason": err_msg, "updatedAt": datetime.now(timezone.utc)}, merge=True)
                return {"status": "blocked", "error": err_msg}
            has_consumed = True
            logger.info(f"[CostGuard] Reserved quiz_generated for {cost_guard_id} ({cost_guard_mode})")

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
        # Log usage success
        await usage_logger.log(user_id=final_user_id, feature="quiz", event_type="success", session_id=session_id)
        
        await publish_session_event(session_id, "assets.updated", {"fields": ["quiz"]})
        return {"status": "completed"}

    except Exception as e:
        # [FIX] Refund quota on failure
        if has_consumed and cost_guard_id:
            await cost_guard.refund_consumption(cost_guard_id, "quiz_generated", 1, mode=cost_guard_mode)
            logger.info(f"[CostGuard] Refunded quiz_generated for {cost_guard_id} due to failure")

        logger.exception(f"Quiz generation failed for session {session_id}")

        # ops_logger: LLM/job failed
        log_llm_event(session_id, "quiz", "failed", uid=final_user_id, error_code=ErrorCode.VERTEX_SCHEMA_PARSE_ERROR, error_message=str(e))
        # Log usage error
        if final_user_id:
            await usage_logger.log(user_id=final_user_id, feature="quiz", event_type="error", session_id=session_id)
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

@router.post("/internal/tasks/playlist", include_in_schema=False)
async def handle_playlist_task(request: Request):
    """
    Cloud Tasks endpoint for Playlist Generation
    """
    return await _handle_playlist_task_core(request)

async def _handle_playlist_task_core(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    session_id = payload.get("sessionId")
    job_id = payload.get("jobId")
    user_id = payload.get("userId")
    
    if not session_id:
        return {"status": "error", "message": "sessionId required"}
    
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
            return {"status": "skipped", "reason": "not_found"}
        
        data = doc.to_dict()
        if not final_user_id:
             final_user_id = data.get("ownerUserId") or data.get("userId")

        transcript = resolve_transcript_text(session_id, data)
        if not transcript:
             logger.error("Empty transcript for playlist")
             return {"status": "failed", "reason": "empty_transcript"}

        # Running
        doc_ref.update({"playlistStatus": "running"})
        derived_ref = doc_ref.collection("derived").document("playlist")
        derived_ref.set({
            "status": "running", 
            "updatedAt": datetime.now(timezone.utc),
            "jobId": job_id
        }, merge=True)
        
        log_job_transition(session_id, "playlist", "started", uid=final_user_id, job_id=job_id)

        # Generate
        segments = data.get("diarizedSegments")
        duration = data.get("durationSec")
        playlist_json_str = await generate_playlist_timeline(transcript, segments=segments, duration_sec=duration)
        
        try:
            items = json.loads(playlist_json_str)
        except:
            items = []

        # Success
        ts = datetime.now(timezone.utc)
        doc_ref.update({
            "playlistStatus": "completed",
            "playlist": items,
            "playlistUpdatedAt": ts
        })
        derived_ref.set({
            "status": "succeeded",
            "result": {"items": items},
            "updatedAt": ts,
            "jobId": job_id
        }, merge=True)

        if job_id:
             db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "completed"}, merge=True)
        
        log_job_transition(session_id, "playlist", "completed", uid=final_user_id, job_id=job_id)
        await publish_session_event(session_id, "assets.updated", {"fields": ["playlist"]})
        return {"status": "completed"}

    except Exception as e:
        logger.exception(f"Playlist failed for {session_id}")
        ts = datetime.now(timezone.utc)

        # [FIX] Update session document status to "failed"
        try:
            doc_ref.update({
                "playlistStatus": "failed",
                "playlistError": str(e)[:500],  # Truncate long errors
                "playlistUpdatedAt": ts
            })
        except Exception as update_err:
            logger.error(f"Failed to update session playlistStatus: {update_err}")

        if job_id:
             db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "failed", "errorReason": str(e)}, merge=True)

        derived_ref.set({
            "status": "failed",
            "errorReason": str(e)[:500],
            "updatedAt": ts,
            "jobId": job_id
        }, merge=True)

        return {"status": "failed", "error": str(e)}


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


# [REMOVED] Duplicate deprecated playlist handler - the real implementation is at line 574
# FastAPI registers routes in order, and having two handlers for the same path
# causes the second one to override the first.

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


@router.post("/internal/tasks/merge_migration")
async def handle_merge_migration_task(request: Request):
    """
    Cloud Tasks worker for Account Merge Migration.
    Payload: {"mergeId": str}
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    merge_id = payload.get("mergeId")
    if not merge_id:
        return {"status": "error", "message": "mergeId required"}

    # Use the account_merge logic
    from app.routes.account_merge import execute_migration_batch
    
    try:
        # DB init if needed (Standalone client)
        from google.cloud import firestore
        import os
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
        # Note: execute_migration_batch uses app.firebase.db which is global. 
        # In Cloud Run (FastAPI), app.firebase.db is initialized at startup, so usually safe.
        # But if this is a separate instance or script... 
        # Since this is "internal/tasks/...", it hits the same FastAPI app.
        
        status = execute_migration_batch(merge_id)
        return {"status": "completed", "result": status}
    except Exception as e:
        logger.exception(f"Merge migration failed for {merge_id}")
        return {"status": "failed", "error": str(e)}


async def _run_local_merge_migration(merge_id: str):
    """Local fallback runner"""
    from app.routes.account_merge import execute_migration_batch
    try:
        status = execute_migration_batch(merge_id)
        logger.info(f"Local merge migration finished: {status}")
    except Exception as e:
        logger.error(f"Local merge migration failed: {e}")


@router.post("/internal/tasks/account_migration")
async def handle_account_migration_task(request: Request):
    """
    Cloud Tasks worker for Account Migration (triggered by phone verification merge).
    Payload: {"fromAccountId": str, "toAccountId": str}

    Migrates sessions and other data from one account to another.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    from_account_id = payload.get("fromAccountId")
    to_account_id = payload.get("toAccountId")

    if not from_account_id or not to_account_id:
        return {"status": "error", "message": "fromAccountId and toAccountId required"}

    logger.info(f"[AccountMigration] Starting: {from_account_id} -> {to_account_id}")

    try:
        from datetime import datetime, timezone
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
            logger.info(f"[AccountMigration] Migrated batch of {len(docs)} sessions")

            if len(docs) < batch_size:
                break

        logger.info(f"[AccountMigration] Complete: migrated {total_migrated} sessions from {from_account_id} to {to_account_id}")
        return {"status": "completed", "migratedCount": total_migrated}

    except Exception as e:
        logger.exception(f"[AccountMigration] Failed: {from_account_id} -> {to_account_id}")
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

## First nuke_user handler removed - using consolidated handler below (handle_nuke_user_task_v2)

def _delete_collection(coll_ref, batch_size=50):
    docs = list(coll_ref.limit(batch_size).stream())
    deleted = 0
    while docs:
        for doc in docs:
            doc.reference.delete()
            deleted += 1
        docs = list(coll_ref.limit(batch_size).stream())





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

    has_consumed = False
    try:
        doc = doc_ref.get()
        if not doc.exists:
            qa_ref.set({"status": "failed", "error": "Session not found", "updatedAt": datetime.now(timezone.utc)})
            return {"status": "failed", "error": "Session not found"}
        
        data = doc.to_dict()
        # [Security] Resolve User ID if missing
        if not final_user_id:
            final_user_id = data.get("ownerUserId") or data.get("userId") or data.get("ownerUid")

        # [FIX] Resolve account ID for cost guard
        owner_account_id = data.get("ownerAccountId")
        cost_guard_id = owner_account_id or final_user_id
        cost_guard_mode = "account" if owner_account_id else "user"

        transcript = resolve_transcript_text(session_id, data) or ""

        if not transcript:
            qa_ref.set({"status": "failed", "error": "Transcript empty", "updatedAt": datetime.now(timezone.utc)})
            return {"status": "failed", "error": "Transcript empty"}

        qa_ref.set({"status": "running", "question": question, "updatedAt": datetime.now(timezone.utc)}, merge=True)

        # [TRIPLE LOCK] Monthly Cost Guard
        allowed = await cost_guard.guard_can_consume(cost_guard_id, "llm_calls", 1, mode=cost_guard_mode)
        if not allowed:
             logger.warning(f"[CostGuard] BLOCKED qa {session_id} for user {final_user_id}. Monthly limit exceeded.")
             err_msg = "Monthly LLM limit exceeded (1000 calls)"
             qa_ref.set({"status": "failed", "error": err_msg, "updatedAt": datetime.now(timezone.utc)}, merge=True)
             return {"status": "failed", "error": err_msg}

        has_consumed = True

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
        if has_consumed:
            await cost_guard.refund_consumption(cost_guard_id, "llm_calls", 1, mode=cost_guard_mode)
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
    # [FeatureGate] Check if cloudStt feature is enabled
    if not is_feature_enabled("cloudStt"):
        logger.warning("[FeatureGate] Cloud STT feature is disabled, skipping task")
        return {"status": "skipped", "reason": "feature_disabled"}

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
        
        # [FIX] Policy removed: now cloud_google sessions are allowed to perform batch transcription
        # Cloud mode: Streaming is preferred, but batch is allowed for re-processing or uploaded audio
        pass
        
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

        has_consumed = False
        consumption_sec = duration

        # [FIX] Use ownerAccountId for accurate limit check, fall back to uid with mode="user"
        owner_account_id = data.get("ownerAccountId")
        cost_guard_id = owner_account_id or final_user_id
        cost_guard_mode = "account" if owner_account_id else "user"
        allowed = await cost_guard.guard_can_consume(cost_guard_id, "cloud_stt_sec", duration, mode=cost_guard_mode)
        if not allowed:
             logger.warning(f"[CostGuard] BLOCKED session {session_id} for user {final_user_id}. Monthly limit exceeded.")
             err_msg = "Monthly transcription limit exceeded (100h)"
             doc_ref.update({"transcriptionStatus": "failed", "transcriptionError": err_msg})
             if job_ref: job_ref.set({"status": "failed", "error": err_msg}, merge=True)
             # Should we return or raise? Return prevents retryloop.
             return

        has_consumed = True

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

                    # [FIX] Use session-based idempotency key to prevent duplicate consumption
                    logger.info(f"[FreePlan] Auto-triggering Summary/Quiz for {session_id}")
                    enqueue_summarize_task(session_id, user_id=uid, idempotency_key=f"auto_summary:{session_id}")
                    enqueue_quiz_task(session_id, count=3, user_id=uid, idempotency_key=f"auto_quiz:{session_id}")
            except Exception as e:
                logger.error(f"[FreePlan] Auto-trigger failed: {e}")

        # [NEW] Log Usage for Cloud STT
        # NOTE: usage_logger is imported at file level (line 15)
        try:
             duration_sec = data.get("durationSec")
             if not duration_sec and audio_info.get("durationSec"):
                 duration_sec = audio_info.get("durationSec")

             if duration_sec:
                 uid = data.get("ownerUserId") or data.get("userId") or data.get("ownerUid")
                 if uid:
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
        # [FIX] Use session-based idempotency key to prevent duplicate consumption
        if should_update_main and transcript_text:
             from app.task_queue import enqueue_summarize_task, enqueue_quiz_task
             enqueue_summarize_task(session_id, user_id=final_user_id, idempotency_key=f"auto_summary:{session_id}")
             enqueue_quiz_task(session_id, user_id=final_user_id, idempotency_key=f"auto_quiz:{session_id}")

        return {"status": "completed"}

    except Exception as e:
        if has_consumed:
            await cost_guard.refund_consumption(cost_guard_id, "cloud_stt_sec", consumption_sec, mode=cost_guard_mode)
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
            # [FIX] Use session-based idempotency key to prevent duplicate consumption
            if updates.get("transcriptText"):
                 from app.task_queue import enqueue_summarize_task, enqueue_quiz_task
                 uid = data.get("ownerUserId") or data.get("userId")
                 enqueue_summarize_task(session_id, user_id=uid, idempotency_key=f"auto_summary:{session_id}")
                 enqueue_quiz_task(session_id, user_id=uid, idempotency_key=f"auto_quiz:{session_id}")

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
    [CRITICAL] Complete account deletion worker.

    Deletes ALL user data:
    - All owned sessions (with cascade: audio, images, subcollections)
    - Removes user from shared sessions
    - User document and subcollections (sessionMeta, subscriptions, etc.)
    - Account (if sole owner) or removes from account members
    - uid_links, phone_numbers
    - Apple subscription data (transactions, tokens, entitlements)
    - Share links, username claims
    - Firebase Auth user
    """
    try:
        payload = await request.json()
        user_id = payload.get("userId")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if not user_id:
        return {"status": "error", "message": "userId required"}

    logger.warning(f"[NUKE] Starting complete account deletion for {user_id}")

    # Use the comprehensive nuke service
    from app.services.session_cleanup import nuke_user_complete

    result = nuke_user_complete(user_id)

    # Finalize deletion request/lock if exists
    if result.get("status") == "completed":
        try:
            from google.cloud import firestore as fs
            import os
            project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
            db = fs.Client(project=project_id)

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
                req_ref.update({
                    "status": "deleted",
                    "deletedAt": now,
                    "updatedAt": now,
                })
        except Exception as e:
            logger.warning(f"[NUKE] Failed to finalize deletion request for {user_id}: {e}")

    logger.info(f"[NUKE] Result for {user_id}: {result}")
    return result
@router.post("/internal/tasks/merge_migration")
async def handle_merge_migration_task(request: Request):
    """
    Background Worker: Migrates sessions/data from Source UID to Target Account ID.
    Triggered after a successful merge commit.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    merge_job_id = payload.get("mergeJobId")
    source_uid = payload.get("sourceUid")
    target_account_id = payload.get("targetAccountId")
    
    if not all([merge_job_id, source_uid, target_account_id]):
         return {"status": "error", "message": "Missing required fields"}

    logger.info(f"[Merge] Starting migration job {merge_job_id} (source={source_uid} -> target={target_account_id})")

    # [FIX] Standalone DB Init
    from google.cloud import firestore
    import os
    try:
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
        db = firestore.Client(project=project_id)
    except Exception as e:
        return {"status": "failed", "error": f"DB Init Failed: {e}"}

    job_ref = db.collection("mergeJobs").document(merge_job_id)

    # 1. Migrate Sessions
    # Find all sessions where ownerUserId == source_uid AND ownerAccountId != target_account_id
    # Note: Firestore '!=' query is tricky, simpler to query matches and update.
    # Or query where ownerAccountId == null OR ownerAccountId == old_acc_id?
    # Simplest: where ownerUserId == source_uid.
    
    sessions_ref = db.collection("sessions")
    query = sessions_ref.where("ownerUserId", "==", source_uid).limit(500)
    
    total_migrated = 0
    
    # Simple loop for batching (Cloud Tasks execution time is usually long enough for small/med accounts)
    # For very large accounts, re-enqueue might be needed (cursor).
    
    while True:
        docs = list(query.stream())
        if not docs:
            break
            
        batch = db.batch()
        count = 0
        for doc in docs:
            # Skip if already migrated
            d = doc.to_dict()
            if d.get("ownerAccountId") == target_account_id:
                continue
                
            batch.update(doc.reference, {
                "ownerAccountId": target_account_id,
                "mergedAt": datetime.now(timezone.utc),
                "mergeJobId": merge_job_id
            })
            count += 1
        
        if count > 0:
            batch.commit()
            total_migrated += count
            logger.info(f"[Merge] Migrated batch of {count} sessions.")
        
        if len(docs) < 500:
             break
        # If we hit limit, we loop again. Since we update ownerAccountId, we need query that excludes proper ones?
        # Actually `ownerUserId` doesn't change, so query still returns them.
        # We need to filter manually or change query. 
        # Better: Query where ownerUserId == source_uid AND ownerAccountId == null (or missing)
        # But we need a composite index for that. 
        # Let's rely on `ownerAccountId` check in loop or simple python filtering + safety break 
        # (Assuming typical user has < 5000 sessions).
        # Safe approach: Pagination with last_doc? 
        # For MVP, just breaking after 10 loops (5000 sessions) is safe enough.
        if total_migrated > 5000:
            logger.warning("[Merge] Hit safety limit of 5000 sessions in one run.")
            break

    # 2. Update Job Status
    job_ref.update({
        "status": "completed",
        "migratedSessionCount": total_migrated,
        "completedAt": datetime.now(timezone.utc)
    })
    
    logger.info(f"[Merge] Migration completed. Total sessions: {total_migrated}")
    return {"status": "completed", "migrated": total_migrated}
