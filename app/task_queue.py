import os
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
from app.services.transcripts import resolve_transcript_text

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

def enqueue_summarize_task(
    session_id: str,
    job_id: str | None = None,
    background_tasks: BackgroundTasks = None,
    idempotency_key: str | None = None,
):
    """
    要約タスクをキューに入れる。
    ローカル環境などで Client がない場合は FastAPI の BackgroundTasks にフォールバックする（デバッグ用）。
    """
    payload = {"sessionId": session_id, "jobId": job_id, "idempotencyKey": idempotency_key}
    
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
        response = tasks_client.create_task(parent=parent, task=task)
        logger.info(f"Created task {response.name}")
    except Exception as e:
        logger.error(f"Failed to create task: {e}")
        raise e

def enqueue_transcribe_task(session_id: str, force: bool = False, engine: str = "whisper", job_id: str | None = None):
    """
    文字起こしタスク（Cloud Run Jobs / Functions 連携用）
    """
    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.warning(f"Skipping Transcribe Task (Local mode not fully supported for Whisper): {session_id}")
        # In local mode, we might just mark as failed or log warning
        return

    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    # Assume a separate endpoint for transcription
    url = f"{CLOUD_RUN_URL}/internal/tasks/transcribe"
    payload = {"sessionId": session_id, "force": force, "engine": engine, "jobId": job_id} # Corrected payload line
    
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        },
        "dispatch_deadline": {"seconds": 3600}, # 60 mins for transcription
    }

    try:
        response = tasks_client.create_task(parent=parent, task=task)
        logger.info(f"Created transcribe task {response.name} for session {session_id}")
    except Exception as e:
        logger.error(f"Failed to create transcribe task for session {session_id}: {e}")
        raise e

def enqueue_quiz_task(session_id: str, count: int = 5, job_id: str | None = None, idempotency_key: str | None = None):
    # 同様に実装
    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.info("Running quiz task locally")
        asyncio.create_task(_run_local_quiz(session_id, count, job_id))
        return

    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    url = f"{CLOUD_RUN_URL}/internal/tasks/quiz"
    payload = {"sessionId": session_id, "count": count, "jobId": job_id, "idempotencyKey": idempotency_key}
    
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        }
    }
    
    tasks_client.create_task(parent=parent, task=task)

def enqueue_explain_task(session_id: str, job_id: str | None = None, idempotency_key: str | None = None):
    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.info("Running explain task locally")
        asyncio.create_task(_run_local_explain(session_id, job_id=job_id))
        return

    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    url = f"{CLOUD_RUN_URL}/internal/tasks/explain"
    payload = {"sessionId": session_id, "idempotencyKey": idempotency_key}

    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        },
        "dispatch_deadline": {"seconds": 1800},
    }

    tasks_client.create_task(parent=parent, task=task)

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

    tasks_client.create_task(parent=parent, task=task)
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

    tasks_client.create_task(parent=parent, task=task)
    logger.info(f"Enqueued translate task for session {session_id}")


# ---------- Local fallback workers ---------- #

async def _run_local_summarize(session_id: str, job_id: str | None = None):
    doc_ref = db.collection("sessions").document(session_id)
    try:
        doc = doc_ref.get()
        if not doc.exists:
            logger.warning(f"[local summarize] session not found: {session_id}")
            return
        data = doc.to_dict()
        transcript = resolve_transcript_text(session_id, data)
        segments = data.get("segments") or data.get("diarizedSegments") or []
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
            return
        doc_ref.update({
            "summaryStatus": "running",
            "summaryError": None,
            "summaryUpdatedAt": datetime.utcnow()
        })
        duration = float(data.get("durationSec") or 0.0)
        result = await llm.generate_summary_and_tags(transcript, mode=data.get("mode", "lecture"), segments=segments, duration=duration)
        summary_md = result.get("summaryMarkdown")
        tags = result.get("tags") or []
        doc_ref.update({
            "summaryStatus": "completed",
            "summaryMarkdown": summary_md,
            "summaryUpdatedAt": datetime.utcnow(),
            "summaryError": None,
            "tags": tags[:4],
            "status": "要約済み",
        })
        if job_id:
            db.collection("sessions").document(session_id).collection("jobs").document(job_id).update({"status": "completed"})
        # Log usage
        await usage_logger.log(
            user_id=data.get("userId", "unknown"),
            session_id=session_id,
            feature="summary",
            event_type="success"
        )
    except Exception as e:
        logger.exception(f"[local summarize] failed: {e}")
        doc_ref.update({
            "summaryStatus": "failed",
            "summaryError": str(e),
            "summaryUpdatedAt": datetime.utcnow(),
            "status": "録音済み",
        })
        # Log error
        await usage_logger.log(
            user_id=data.get("userId", "unknown"),
            session_id=session_id,
            feature="summary",
            event_type="error",
            payload={"error_code": type(e).__name__}
        )


async def _run_local_quiz(session_id: str, count: int, job_id: str | None = None):
    doc_ref = db.collection("sessions").document(session_id)
    doc = doc_ref.get()
    if not doc.exists:
        logger.warning(f"[local quiz] session not found: {session_id}")
        return
    data = doc.to_dict()
    transcript = resolve_transcript_text(session_id, data)
    if not transcript:
        logger.warning(f"[local quiz] transcript empty: {session_id}")
        doc_ref.update({
            "quizStatus": "failed",
            "quizError": "Transcript is empty",
            "status": "録音済み"
        })
        return
    doc_ref.update({"quizStatus": "running", "quizError": None, "status": "テスト生成"})
    try:
        from app.services.llm import clean_quiz_markdown
        quiz_raw = await llm.generate_quiz(transcript, mode=data.get("mode", "lecture"), count=count)
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

async def _run_local_explain(session_id: str, job_id: str | None = None):
    doc_ref = db.collection("sessions").document(session_id)
    doc = doc_ref.get()
    if not doc.exists:
        logger.warning(f"[local explain] session not found: {session_id}")
        return
    data = doc.to_dict()
    transcript = resolve_transcript_text(session_id, data)
    if not transcript:
        doc_ref.update({
            "explainStatus": "failed",
            "explainError": "Transcript is empty",
            "status": "録音済み",
        })
        return
    doc_ref.update({"explainStatus": "running", "explainError": None})
    try:
        explanation = await llm.generate_explanation(transcript, mode=data.get("mode", "lecture"))
        doc_ref.update({
            "explainStatus": "completed",
            "explainMarkdown": explanation,
            "explainUpdatedAt": datetime.utcnow(),
            "explainError": None,
        })
        if job_id:
            db.collection("sessions").document(session_id).collection("jobs").document(job_id).update({"status": "completed"})
    except Exception as e:
        logger.exception(f"[local explain] failed: {e}")
        doc_ref.update({
            "explainStatus": "failed",
            "explainError": str(e),
            "explainUpdatedAt": datetime.utcnow(),
        })


async def _run_local_highlights(session_id: str):
    doc_ref = db.collection("sessions").document(session_id)
    doc = doc_ref.get()
    if not doc.exists:
        logger.warning(f"[local highlights] session not found: {session_id}")
        return
    data = doc.to_dict()
    transcript = resolve_transcript_text(session_id, data) or ""
    segments = data.get("segments") or data.get("diarizedSegments") or []
    if not transcript:
        doc_ref.update({
            "highlightsStatus": "failed",
            "highlightsError": "Transcript is empty",
            "highlightsUpdatedAt": datetime.utcnow()
        })
        return
    doc_ref.update({"highlightsStatus": "running", "highlightsError": None})
    try:
        result = await llm.generate_highlights_and_tags(transcript, segments)
        doc_ref.update({
            "highlightsStatus": "completed",
            "highlights": result.get("highlights", []),
            "tags": result.get("tags", []),
            "highlightsUpdatedAt": datetime.utcnow(),
            "highlightsError": None
        })
    except Exception as e:
        logger.exception(f"[local highlights] failed: {e}")
        doc_ref.update({
            "highlightsStatus": "failed",
            "highlightsError": str(e),
            "highlightsUpdatedAt": datetime.utcnow()
        })


async def _run_local_playlist(session_id: str):
    doc_ref = db.collection("sessions").document(session_id)
    doc = doc_ref.get()
    if not doc.exists:
        logger.warning(f"[local playlist] session not found: {session_id}")
        return
    data = doc.to_dict()
    transcript = resolve_transcript_text(session_id, data) or ""
    segments = data.get("diarizedSegments") or data.get("segments") or []
    if not transcript:
        doc_ref.update({
            "playlistStatus": "failed",
            "playlistError": "Transcript is empty",
            "playlistUpdatedAt": datetime.utcnow()
        })
        return
    doc_ref.update({
        "playlistStatus": "running",
        "playlistError": None,
        "playlistUpdatedAt": datetime.utcnow()
    })
    try:
        duration = data.get("durationSec")
        raw = await llm.generate_playlist_timeline(transcript, segments=segments, duration_sec=duration)
        try:
            items_raw = json.loads(raw)
        except Exception:
            items_raw = []
        from app.services.playlist_utils import normalize_playlist_items
        normalized = normalize_playlist_items(items_raw, segments=segments, duration_sec=duration)
        doc_ref.update({
            "playlistStatus": "completed",
            "playlist": normalized,
            "playlistUpdatedAt": datetime.utcnow(),
            "playlistError": None
        })
    except Exception as e:
        logger.exception(f"[local playlist] failed: {e}")
        doc_ref.update({
            "playlistStatus": "failed",
            "playlistError": str(e),
            "playlistUpdatedAt": datetime.utcnow()
        })
def enqueue_generate_highlights_task(session_id: str):
    """
    ハイライト生成タスクをキューに入れる。
    """
    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.info("Running highlights task locally")
        asyncio.create_task(_run_local_highlights(session_id))
        return

    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    url = f"{CLOUD_RUN_URL}/internal/tasks/highlights"
    payload = {"sessionId": session_id}

    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        }
    }

    tasks_client.create_task(parent=parent, task=task)

def enqueue_playlist_task(session_id: str):
    """
    プレイリスト生成タスクをキューに入れる。
    """
    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.info("Running playlist task locally")
        asyncio.create_task(_run_local_playlist(session_id))
        return

    parent = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    url = f"{CLOUD_RUN_URL}/internal/tasks/playlist"
    payload = {"sessionId": session_id}

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
        tasks_client.create_task(parent=parent, task=task)
        logger.info(f"Enqueued playlist task for session {session_id}")
    except Exception as e:
        logger.error(f"Failed to enqueue playlist task: {e}")
        raise e

def enqueue_transcribe_task(
    session_id: str,
    force: bool = False,
    engine: str = "google",
    job_id: str | None = None,
    idempotency_key: str | None = None,
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
    }

    # ローカルモード: asyncio.create_task でバックグラウンド実行
    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.info(f"Running transcribe task locally for session: {session_id}")
        asyncio.create_task(_run_local_transcribe(session_id, force=force, engine=engine, job_id=job_id))
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
        tasks_client.create_task(parent=parent, task=task)
        logger.info(f"Enqueued transcribe task for session {session_id}, job_id={job_id}")
    except Exception as e:
        logger.error(f"Failed to enqueue transcribe task: {e}")
        raise e

def enqueue_youtube_import_task(session_id: str, url: str, language: str = "ja"):
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
    payload = {"sessionId": session_id, "url": url, "language": language}

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
        tasks_client.create_task(parent=parent, task=task)
        logger.info(f"Enqueued youtube import task for {session_id}")
    except Exception as e:
        logger.error(f"Failed to enqueue youtube import task: {e}")
        raise e

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
        # Trigger next steps (Summary/Quiz)
        # Trigger next steps (Summary/Quiz/Playlist)
        enqueue_summarize_task(session_id)
        enqueue_quiz_task(session_id)
        enqueue_playlist_task(session_id)
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

        # Trigger downstream tasks (summary, quiz)
        if updates.get("transcriptText"):
            enqueue_summarize_task(session_id)
            enqueue_quiz_task(session_id)

        logger.info(f"[local transcribe] completed for session: {session_id}")

    except Exception as e:
        logger.exception(f"[local transcribe] failed for {session_id}: {e}")
        _update_job_status("failed", str(e))
        doc_ref.update({
            "transcriptionStatus": "failed",
            "transcriptionError": str(e),
        })
