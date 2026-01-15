from fastapi import APIRouter, HTTPException, Request
from google.cloud import firestore
# from app.firebase import db
from app.firebase import AUDIO_BUCKET_NAME
from app.services.llm import generate_quiz, generate_summary_and_tags, generate_explanation, clean_quiz_markdown
from app.services.transcripts import resolve_transcript_text
from app.services.usage import usage_logger
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
    """
    Cloud Tasks から呼び出される Worker エンドポイント。
    実際に LLM を呼び出して Firestore を更新する。
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    try:
        session_id = payload.get("sessionId")
        job_id = payload.get("jobId")
        idempotency_key = payload.get("idempotencyKey")
        if not session_id:
            logger.error("sessionId is missing")
            return {"status": "error", "message": "sessionId required"}

        logger.info(f"Processing summarize task for session: {session_id}")

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
        doc = doc_ref.get()
        
        # 存在しない、または削除済み
        if not doc.exists:
            logger.warning(f"Session {session_id} not found.")
            return {"status": "skipped", "reason": "not_found"}

        data = doc.to_dict()
        transcript = resolve_transcript_text(session_id, data) or ""
        mode = data.get("mode", "lecture")
        segments = data.get("segments") or data.get("diarizedSegments") or []
        derived_ref = doc_ref.collection("derived").document("summary")

        if idempotency_key:
            derived_snap = derived_ref.get()
            if derived_snap.exists:
                current_key = (derived_snap.to_dict() or {}).get("idempotencyKey")
                # If same key, return cached success. If different key, PROCEED (Overwrite).
                if current_key and current_key == idempotency_key:
                    if job_id:
                         db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "completed", "result": "cached"}, merge=True)
                    return {"status": "skipped", "reason": "idempotent_hit"}

        if not transcript:
            # Transcriptがないのにタスクが来た -> 失敗
            logger.error(f"Transcript empty for session {session_id}")
            doc_ref.update({
                "summaryStatus": "failed",
                "summaryError": "Transcript is empty",
                "summaryUpdatedAt": datetime.now(timezone.utc),
                # Playlist status is NOT handled here anymore
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
            return {"status": "failed", "reason": "empty_transcript"}
    except BaseException as e:
        # Catch EVERYTHING including SystemExit/KeyboardInterrupt
        import traceback
        return {"status": "failed", "error": f"FATAL: {str(e)}", "trace": traceback.format_exc()}

    try:
        # 実行開始を記録
        doc_ref.update({
            "summaryStatus": "running",
            "summaryUpdatedAt": datetime.now(timezone.utc),
            # "playlistStatus": "running", # Separated
        })
        derived_ref.set({
            "status": "running",
            "errorReason": None,
            "updatedAt": datetime.now(timezone.utc),
            "idempotencyKey": idempotency_key,
        }, merge=True)

        result = await generate_summary_and_tags(transcript, mode=mode, segments=segments)
        summary_markdown = result.get("summaryMarkdown")
        tags = (result.get("tags") or [])[:4]

        # Extract topic summary (first line of markdown or specific field)
        topic_summary = None
        if summary_markdown:
             lines = summary_markdown.split('\n')
             for line in lines:
                 if line.strip() and not line.startswith('#'):
                     topic_summary = line.strip()[:100] # Limit length
                     break
        
        # Firestore 更新
        doc_ref.update({
            "summaryStatus": "completed",
            "summaryMarkdown": summary_markdown,
            "topicSummary": topic_summary, # [NEW]
            "summaryUpdatedAt": datetime.now(timezone.utc),
            "summaryError": None,
            # Playlist updates removed
            "autoTags": tags, # [NEW] Use autoTags instead of tags
            # "tags": tags, # DO NOT overwrite user tags!
            "status": "要約済み",
        })
        derived_ref.set({
            "status": "succeeded",
            "result": {
                "markdown": summary_markdown,
                "tags": tags,
                "topicSummary": topic_summary,
            },
            "modelInfo": {"provider": "vertexai"},
            "updatedAt": datetime.now(timezone.utc),
            "errorReason": None,
            "idempotencyKey": idempotency_key,
        }, merge=True)
        logger.info(f"Successfully summarized session {session_id} with tags")
        if job_id:
            db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "completed"}, merge=True)
        return {"status": "completed"}

    except Exception as e:
        logger.exception(f"Summarization failed for session {session_id}")
        
        error_str = str(e)
        is_transient = "429" in error_str or "503" in error_str or "ResourceExhausted" in error_str or "ServiceUnavailable" in error_str
        
        if is_transient:
             logger.warning(f"Transient error detected for session {session_id}, raising 503 for retry. Error: {e}")
             raise HTTPException(status_code=503, detail="Transient error, retrying...")

        doc_ref.update({
            "summaryStatus": "failed",
            "summaryError": str(e),
            "summaryUpdatedAt": datetime.now(timezone.utc),
            # playlist status removed
            "status": "録音済み",
        })
        # If job_id provided, fail it
        if job_id:
            db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "failed", "errorReason": str(e)}, merge=True)
        derived_ref.set({
            "status": "failed",
            "errorReason": str(e),
            "updatedAt": datetime.now(timezone.utc),
            "idempotencyKey": idempotency_key,
        }, merge=True)
        return {"status": "failed", "error": str(e)}



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

    if not session_id or not url:
        return {"status": "error", "message": "Missing sessionId or url"}

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

    try:
        from app.services.youtube import process_youtube_import
        
        # Mark as running (optional, user might see "queued" -> "running"?)
        # Currently status is "queued" (set by imports.py).
        # We can update it if we want custom UI state.
        
        # Execute Process (Blocking/Sync)
        # process_youtube_import is blocking, but that's fine for Cloud Run Worker (single request)
        # or we run in threadpool if we want concurrency (FastAPI handles def as threadpool)
        transcript = process_youtube_import(session_id, url, language=language)
        
        # Success
        doc_ref.update({
            "transcriptText": transcript,
            "status": "録音済み",
            "audioPath": f"imports/{session_id}.flac",
            "updatedAt": datetime.now(timezone.utc)
        })
        
        # Trigger Next Steps
        # Trigger Next Steps
        from app.task_queue import enqueue_summarize_task, enqueue_quiz_task, enqueue_playlist_task
        enqueue_summarize_task(session_id)
        enqueue_quiz_task(session_id)
        enqueue_playlist_task(session_id)
        
        logger.info(f"YouTube Import Success for {session_id}")
        return {"status": "completed"}

    except Exception as e:
        logger.exception(f"YouTube Import Failed for {session_id}")
        doc_ref.update({
            "status": "failed", # Or specific error status
            "transcriptText": None,
            "errorMessage": str(e), # Using generic field or create new?
            # Using summaryError for now or just generic logs? 
            # User suggested "errorCode, errorMessage".
            # For compatibility, let's just mark status="failed".
            "updatedAt": datetime.now(timezone.utc)
        })
        return {"status": "failed", "error": str(e)}

@router.post("/internal/tasks/quiz")
async def handle_quiz_task(request: Request):
    """
    Cloud Tasks から呼び出される Quiz Worker エンドポイント。
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    try:
        session_id = payload.get("sessionId")
        job_id = payload.get("jobId")
        idempotency_key = payload.get("idempotencyKey")
        count = payload.get("count", 5)

        if not session_id:
            return {"status": "error", "message": "session_id required"}

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
            return {"status": "skipped"}
            
        data = doc.to_dict()
        transcript = resolve_transcript_text(session_id, data) or ""
        mode = data.get("mode", "lecture")
        derived_ref = doc_ref.collection("derived").document("quiz")

        if idempotency_key:
            derived_snap = derived_ref.get()
            if derived_snap.exists:
                current_key = (derived_snap.to_dict() or {}).get("idempotencyKey")
                # If same key, return cached success. If different key, PROCEED (Overwrite).
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
            return {"status": "failed"}
    except BaseException as e:
        # Catch EVERYTHING
        import traceback
        return {"status": "failed", "error": f"FATAL: {str(e)}", "trace": traceback.format_exc()}

    try:
        # generate_quiz, clean_quiz_markdown imported at top level now
        quiz_raw = await generate_quiz(transcript, mode=mode, count=count)
        quiz_md = clean_quiz_markdown(quiz_raw)
        
        if job_id:
            db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "running"}, merge=True)
            
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
        return {"status": "completed"}
    except Exception as e:
        logger.exception(f"Quiz generation failed for session {session_id}")
        
        error_str = str(e)
        is_transient = "429" in error_str or "503" in error_str or "ResourceExhausted" in error_str or "ServiceUnavailable" in error_str
        
        if is_transient:
             logger.warning(f"Transient error detected for quiz {session_id}, raising 503 for retry.")
             raise HTTPException(status_code=503, detail="Transient error, retrying...")

        doc_ref.update({"quizStatus": "failed", "quizError": str(e), "status": "録音済み"})
        if job_id:
             db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "failed", "errorReason": str(e)}, merge=True)
        derived_ref.set({
            "status": "failed",
            "errorReason": str(e),
            "updatedAt": datetime.now(timezone.utc),
            "idempotencyKey": idempotency_key,
        }, merge=True)
        return {"status": "failed", "error": str(e)}

@router.post("/internal/tasks/explain")
async def handle_explain_task(request: Request):
    """
    Cloud Tasks から呼び出される Explanation Worker エンドポイント。
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    session_id = payload.get("sessionId")
    job_id = payload.get("jobId")
    idempotency_key = payload.get("idempotencyKey")
    if not session_id:
        logger.error("sessionId is missing")
        return {"status": "error", "message": "sessionId required"}

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
        return {"status": "skipped"}

    data = doc.to_dict()
    transcript = resolve_transcript_text(session_id, data) or ""
    mode = data.get("mode", "lecture")
    derived_ref = doc_ref.collection("derived").document("explain")

    if idempotency_key:
        derived_snap = derived_ref.get()
        if derived_snap.exists:
            current_key = (derived_snap.to_dict() or {}).get("idempotencyKey")
            # If same key, return cached success. If different key, PROCEED (Overwrite).
            if current_key and current_key == idempotency_key:
                if job_id:
                     db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "completed", "result": "cached"}, merge=True)
                return {"status": "skipped", "reason": "idempotent_hit"}

    if not transcript:
        doc_ref.update({"explainStatus": "failed", "explainError": "Transcript empty"})
        derived_ref.set({
            "status": "failed",
            "errorReason": "Transcript empty",
            "updatedAt": datetime.now(timezone.utc),
            "idempotencyKey": idempotency_key,
        }, merge=True)
        return {"status": "failed"}

    try:
        doc_ref.update({"explainStatus": "running", "explainError": None})
        derived_ref.set({
            "status": "running",
            "errorReason": None,
            "updatedAt": datetime.now(timezone.utc),
            "idempotencyKey": idempotency_key,
        }, merge=True)
        if job_id:
            db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "running"}, merge=True)

        explanation = await generate_explanation(transcript, mode=mode)
        doc_ref.update({
            "explainStatus": "completed",
            "explainMarkdown": explanation,
            "explainUpdatedAt": datetime.now(timezone.utc),
            "explainError": None,
        })
        derived_ref.set({
            "status": "succeeded",
            "result": {"markdown": explanation},
            "modelInfo": {"provider": "vertexai"},
            "updatedAt": datetime.now(timezone.utc),
            "errorReason": None,
            "idempotencyKey": idempotency_key,
        }, merge=True)
        return {"status": "completed"}
        if job_id:
            db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "completed"}, merge=True)
    except BaseException as e:
        logger.exception(f"Explain generation failed for session {session_id}")
        error_str = str(e)
        is_transient = "429" in error_str or "503" in error_str or "ResourceExhausted" in error_str or "ServiceUnavailable" in error_str
        if is_transient:
            logger.warning(f"Transient error detected for explain {session_id}, raising 503 for retry.")
            raise HTTPException(status_code=503, detail="Transient error, retrying...")

        doc_ref.update({"explainStatus": "failed", "explainError": str(e)})
        if job_id:
             db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "failed", "errorReason": str(e)}, merge=True)
        derived_ref.set({
            "status": "failed",
            "errorReason": str(e),
            "updatedAt": datetime.now(timezone.utc),
            "idempotencyKey": idempotency_key,
        }, merge=True)
        return {"status": "failed", "error": str(e)}

@router.post("/internal/tasks/highlights")
async def handle_generate_highlights(request: Request):
    """
    Cloud Tasks から呼び出される Worker エンドポイント。
    ハイライトとタグを生成する。
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    session_id = payload.get("sessionId")
    if not session_id:
        logger.error("sessionId is missing")
        return {"status": "error", "message": "sessionId required"}

    logger.info(f"Processing highlights task for session: {session_id}")
    
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
        logger.warning(f"Session {session_id} not found.")
        return {"status": "skipped", "reason": "not_found"}
        
    data = doc.to_dict()
    transcript = resolve_transcript_text(session_id, data) or ""
    segments = data.get("segments", [])
    
    if not transcript:
        logger.error(f"Transcript empty for session {session_id}, cannot generate highlights")
        doc_ref.update({
            "highlightsStatus": "failed",
            "highlightsError": "Transcript is empty",
            "highlightsUpdatedAt": datetime.now(timezone.utc)
        })
        return {"status": "failed", "reason": "empty_transcript"}

    try:
        from app.services.llm import generate_highlights_and_tags
        result = await generate_highlights_and_tags(transcript, segments)
            
        doc_ref.update({
            "highlightsStatus": "completed",
            "highlights": result.get("highlights", []),
            "tags": result.get("tags", []),
            "highlightsUpdatedAt": datetime.now(timezone.utc),
            "highlightsError": None
        })
        logger.info(f"Successfully generated highlights for session {session_id}")
        return {"status": "completed"}
    except Exception as e:
        logger.exception(f"Highlights generation failed for session {session_id}")
        doc_ref.update({
            "highlightsStatus": "failed",
            "highlightsError": str(e),
            "highlightsUpdatedAt": datetime.now(timezone.utc)
        })
        return {"status": "failed", "error": str(e)}


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
    transcript = resolve_transcript_text(session_id, data) or ""
    segments = data.get("diarizedSegments") or data.get("segments") or []

    if not transcript:
        doc_ref.update({
            "playlistStatus": "failed",
            "playlistError": "Transcript is empty",
            "playlistUpdatedAt": datetime.now(timezone.utc)
        })
        return {"status": "failed", "reason": "empty_transcript"}

    try:
        duration = data.get("durationSec")
        from app.services.llm import generate_playlist_timeline
        raw = await generate_playlist_timeline(transcript, segments=segments, duration_sec=duration)
        if isinstance(raw, str):
            try:
                items_raw = json.loads(raw)
            except Exception:
                items_raw = []
        else:
            items_raw = raw or []

        from app.services.playlist_utils import normalize_playlist_items
        normalized = normalize_playlist_items(items_raw, segments=segments, duration_sec=duration)

        doc_ref.update({
            "playlistStatus": "completed",
            "playlist": normalized,
            "playlistUpdatedAt": datetime.now(timezone.utc),
            "playlistError": None
        })
        return {"status": "completed", "items": len(normalized)}
    except Exception as e:
        logger.exception(f"Playlist generation failed for session {session_id}")
        doc_ref.update({
            "playlistStatus": "failed",
            "playlistError": str(e),
            "playlistUpdatedAt": datetime.now(timezone.utc)
        })
        return {"status": "failed", "error": str(e)}


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
    
    try:
        doc = doc_ref.get()
        if not doc.exists:
            qa_ref.set({"status": "failed", "error": "Session not found", "updatedAt": datetime.now(timezone.utc)})
            return {"status": "failed", "error": "Session not found"}
        
        data = doc.to_dict()
        transcript = resolve_transcript_text(session_id, data) or ""
        
        if not transcript:
            qa_ref.set({"status": "failed", "error": "Transcript empty", "updatedAt": datetime.now(timezone.utc)})
            return {"status": "failed", "error": "Transcript empty"}
        
        qa_ref.set({"status": "running", "question": question, "updatedAt": datetime.now(timezone.utc)}, merge=True)
        
        result = await llm.answer_question(transcript, question, data.get("mode", "lecture"))
        answer = result.get("answer", "")
        citations = result.get("citations", [])
        
        qa_ref.set({
            "status": "completed",
            "answer": answer,
            "citations": citations,
            "updatedAt": datetime.now(timezone.utc),
        }, merge=True)
        
        return {"status": "completed", "qaId": qa_id}
        
    except Exception as e:
        logger.exception(f"QA task failed for session {session_id}")
        qa_ref.set({"status": "failed", "error": str(e), "updatedAt": datetime.now(timezone.utc)}, merge=True)
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
    
    try:
        doc = doc_ref.get()
        if not doc.exists:
            trans_ref.set({"status": "failed", "error": "Session not found", "updatedAt": datetime.now(timezone.utc)})
            return {"status": "failed", "error": "Session not found"}
        
        data = doc.to_dict()
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
        
        translated_text = await llm.translate_text(transcript, target_language)
        
        trans_ref.set({
            "status": "completed",
            "language": target_language,
            "translatedText": translated_text,
            "sessionId": session_id,
            "createdAt": datetime.now(timezone.utc),
            "updatedAt": datetime.now(timezone.utc),
        }, merge=True)
        
        return {"status": "completed", "sessionId": session_id, "language": target_language}
        
    except Exception as e:
        logger.exception(f"Translate task failed for session {session_id}")
        trans_ref.set({"status": "failed", "error": str(e), "updatedAt": datetime.now(timezone.utc)}, merge=True)
        return {"status": "failed", "error": str(e)}


@router.post("/internal/tasks/transcribe")
async def handle_transcribe_task(request: Request):
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
    
    if not session_id:
        return {"status": "error", "message": "sessionId required"}

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
        
        # Check if already processed (Idempotency) - though typically transcribe is expensive so we might rely on status check too
        if idempotency_key and job_ref:
            # Check existing job status? 
            # Simplified: Proceed for now mostly.
            pass

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
             doc_ref.update({
                 "transcriptionStatus": "failed",
                 "transcriptionError": "Duration exceeds 2 hour limit"
             })
             if job_ref: job_ref.set({"status": "failed", "error": "Duration exceeds 2 hour limit"}, merge=True)
             return

        doc_ref.update({
             "transcriptionStatus": "running",
             "transcriptionEngine": engine, 
             "transcriptionUpdatedAt": datetime.now(timezone.utc)
        })

        # Execute Transcription
        from app.services.google_speech import transcribe_audio_google_with_segments

        transcript_text, segments = transcribe_audio_google_with_segments(
            gcs_path, language_code="ja-JP"
        )

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
             "transcriptionError": None
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
                    enqueue_summarize_task(session_id)
                    enqueue_quiz_task(session_id, count=3) # Default 3 questions for free?
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
             enqueue_summarize_task(session_id)
             enqueue_quiz_task(session_id)

        return {"status": "completed"}

    except Exception as e:
        logger.exception(f"Transcribe task failed for {session_id}")
        error_msg = str(e)
        doc_ref.update({
            "transcriptionStatus": "failed",
            "transcriptionError": error_msg,
            "transcriptionUpdatedAt": datetime.now(timezone.utc)
        })
        if job_ref: job_ref.set({"status": "failed", "errorReason": error_msg}, merge=True)
        return {"status": "failed", "error": error_msg}
        return {"status": "failed", "error": str(e)}

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
                 enqueue_summarize_task(session_id)
                 enqueue_quiz_task(session_id)

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
