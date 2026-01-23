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
        tasks_client.create_task(parent=parent, task=task)
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
        response = tasks_client.create_task(parent=parent, task=task)
        logger.info(f"Created task {response.name}")
    except Exception as e:
        logger.error(f"Failed to create task: {e}")
        raise e

def enqueue_quiz_task(
    session_id: str,
    count: int = 5,
    job_id: str | None = None,
    idempotency_key: str | None = None,
    user_id: str | None = None,
    usage_reserved: bool = False,
):
    # 同様に実装
    if tasks_client is None or os.environ.get("USE_LOCAL_TASKS") == "1":
        logger.info("Running quiz task locally")
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

    tasks_client.create_task(parent=parent, task=task)
    logger.info(f"Enqueued playlist task for session {session_id}")

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
        result = await llm.generate_summary_and_tags(
            transcript,
            mode=data.get("mode", "lecture"),
        )
        summary_md = result.get("summaryMarkdown")
        summary_json = result.get("summaryJson") or {}
        summary_type = result.get("summaryType") or data.get("mode", "lecture")
        summary_json_version = result.get("summaryJsonVersion") or 1
        tags = result.get("tags") or []
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
        doc_ref.update(update_payload)
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
            "quizUpdatedAt": datetime.utcnow(),
            "status": "録音済み",
        })
        return

    # Trigger LLM
    try:
        doc_ref.update({
            "quizStatus": "running",
            "quizError": None,
            "quizUpdatedAt": datetime.utcnow()
        })
        quiz_md = await llm.generate_quiz(transcript, mode=data.get("mode", "lecture"), count=count)
        doc_ref.update({
            "quizStatus": "completed",
            "quizMarkdown": quiz_md,
            "quizUpdatedAt": datetime.utcnow(),
            "quizError": None,
        })
        if job_id:
             db.collection("sessions").document(session_id).collection("jobs").document(job_id).update({"status": "completed"})
    except Exception as e:
        logger.exception(f"[local quiz] failed: {e}")
        doc_ref.update({
            "quizStatus": "failed",
            "quizError": str(e),
            "quizUpdatedAt": datetime.utcnow(),
        })

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
            items = json.loads(playlist_json_str)
        except:
            items = []
            
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


async def _run_local_playlist(session_id: str):
    doc_ref = db.collection("sessions").document(session_id)
    doc = doc_ref.get()
    if not doc.exists:
        logger.warning(f"[local playlist] session not found: {session_id}")
        return
    doc_ref.update({
        "playlistStatus": "failed",
        "playlistError": "deprecated",
        "playlistUpdatedAt": datetime.utcnow()
    })
def enqueue_generate_highlights_task(session_id: str, user_id: str | None = None, job_id: str | None = None):
    """
    ハイライト生成タスクをキューに入れる。
    """
    logger.info("Highlights task is deprecated; skipping enqueue.")

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
        tasks_client.create_task(parent=parent, task=task)
        logger.info(f"Enqueued NUKE task for {user_id}")
    except Exception as e:
        logger.error(f"Failed to enqueue Nuke task for {user_id}: {e}")
        raise e

def enqueue_playlist_task(session_id: str, user_id: str | None = None, job_id: str | None = None):
    """
    プレイリスト生成タスクをキューに入れる。
    """
    logger.info("Playlist task is deprecated; skipping enqueue.")

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
        tasks_client.create_task(parent=parent, task=task)
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
        tasks_client.create_task(parent=parent, task=task)
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
        tasks_client.create_task(parent=parent, task=task)
        logger.info(f"Enqueued merge migration task for {merge_id}")
    except Exception as e:
        logger.error(f"Failed to enqueue merge migration task: {e}")
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
        # [Security] Pass userId if available
        data = doc_ref.get().to_dict() or {}
        uid = data.get("ownerUserId") or data.get("userId")
        
        # Trigger next steps (Summary/Quiz/Playlist)
        enqueue_summarize_task(session_id, user_id=uid)
        enqueue_quiz_task(session_id, user_id=uid)
        enqueue_playlist_task(session_id, user_id=uid)
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
            uid = data.get("ownerUserId") or data.get("userId")
            enqueue_summarize_task(session_id, user_id=uid)
            enqueue_quiz_task(session_id, user_id=uid)

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
        tasks_client.create_task(parent=parent, task=task)
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
