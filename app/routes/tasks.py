from fastapi import APIRouter, HTTPException, Request, Depends
from app.dependencies import verify_cloud_tasks_request
from google.cloud import firestore
# from app.firebase import db
from app.firebase import AUDIO_BUCKET_NAME, db, storage_client
from app.services.llm import (
    GEMINI_MODEL_NAME,
    generate_quiz,
    generate_quiz_batch,
    generate_quiz_json,
    quiz_json_to_markdown,
    generate_quick_summary,
    generate_summary_and_tags,
    clean_quiz_markdown,
    answer_question,
    translate_text,
    generate_playlist_timeline,
)
from app.services.transcripts import resolve_transcript_text, resolve_transcript_text_async, count_transcript_chunks, get_transcript_chunks
from app.services.import_state import mark_import_completed
from app.services.audit import emit as audit_emit
from app.services.playlist_utils import normalize_playlist_items
from app.services.usage import usage_logger
from app.services.ops_logger import log_job_transition, log_llm_event, log_stt_event, ErrorCode
from app.services.cost_guard import cost_guard
from app.services.ai_credits import ai_credits, estimate_cost
from app.services.session_event_bus import publish_session_event
from app.services.account_deletion import (
    LOCKS_COLLECTION,
    REQUESTS_COLLECTION,
    deletion_lock_id,
)
from app.services.app_config import is_feature_enabled
import logging
import json
import asyncio
from datetime import datetime, timezone, timedelta

router = APIRouter()
logger = logging.getLogger("app.tasks")

@router.get("/internal/tasks/ping")
async def ping_task():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc)}


# ═══════════════════════════════════════════════════════════════
# YouTube Health Check (daily scheduled)
# ═══════════════════════════════════════════════════════════════

@router.post("/internal/tasks/youtube_health_check", include_in_schema=False)
async def youtube_health_check(request: Request):
    """
    YouTube字幕取得のヘルスチェック。
    Cloud Scheduler から毎朝呼ばれ、結果を Firestore に保存する。
    テスト動画の字幕を取得し、成功/失敗を記録する。
    """
    import asyncio
    from google.cloud import firestore as fs
    import os

    # テスト用動画（短い公開動画）
    TEST_VIDEO_ID = "jNQXAC9IVRw"  # "Me at the zoo" (first YouTube video)
    TEST_VIDEO_FALLBACK = "dQw4w9WgXcQ"

    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
    db = fs.Client(project=project_id)
    now = datetime.now(timezone.utc)

    results = {
        "checkedAt": now,
        "proxy": {"status": "unknown"},
        "direct": {"status": "unknown"},
    }

    # 1. プロキシ経由テスト
    try:
        from app.services.youtube import fetch_youtube_transcript
        def _fetch_proxy():
            return fetch_youtube_transcript(TEST_VIDEO_ID, languages=["en"], format="text")
        r = await asyncio.to_thread(_fetch_proxy)
        segments = len(r.get("items", []))
        text_len = len(r.get("text", ""))
        results["proxy"] = {
            "status": "ok",
            "segments": segments,
            "textLength": text_len,
            "videoId": TEST_VIDEO_ID,
            "latencyMs": None,
        }
    except Exception as e:
        # フォールバック動画でも試す
        try:
            def _fetch_proxy_fb():
                return fetch_youtube_transcript(TEST_VIDEO_FALLBACK, languages=["en"], format="text")
            r = await asyncio.to_thread(_fetch_proxy_fb)
            results["proxy"] = {
                "status": "ok",
                "segments": len(r.get("items", [])),
                "textLength": len(r.get("text", "")),
                "videoId": TEST_VIDEO_FALLBACK,
            }
        except Exception as e2:
            results["proxy"] = {"status": "error", "error": str(e2)[:300], "videoId": TEST_VIDEO_ID}

    # 2. 直接接続テスト
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        def _fetch_direct():
            ytt = YouTubeTranscriptApi()
            fetched = ytt.fetch(TEST_VIDEO_ID, languages=["en"])
            return len(fetched.to_raw_data())
        count = await asyncio.to_thread(_fetch_direct)
        results["direct"] = {"status": "ok", "segments": count, "videoId": TEST_VIDEO_ID}
    except Exception as e:
        results["direct"] = {"status": "error", "error": str(e)[:300]}

    # 3. 結果を Firestore に保存
    # config/youtube_health に最新結果、history サブコレクションに履歴
    health_ref = db.collection("config").document("youtube_health")
    health_ref.set({
        "lastCheck": results,
        "updatedAt": now,
    }, merge=True)

    # 履歴を追加（30日分保持）
    history_ref = health_ref.collection("history").document(now.strftime("%Y-%m-%d"))
    history_ref.set(results)

    logger.info(f"[YouTubeHealth] proxy={results['proxy']['status']}, direct={results['direct']['status']}")
    return results


# ═══════════════════════════════════════════════════════════════
# Quick Summary Worker (30-60s preview, no CostGuard consumption)
# ═══════════════════════════════════════════════════════════════

@router.post("/internal/tasks/summarize_quick", dependencies=[Depends(verify_cloud_tasks_request)])
async def handle_summarize_quick_task(request: Request):
    """Quick Summary: 先出し要約（highlights 3件 + topicSummary）。CostGuard消費なし。"""
    if not is_feature_enabled("summarization"):
        return {"status": "skipped", "reason": "feature_disabled"}

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    session_id = payload.get("sessionId")
    idempotency_key = payload.get("idempotencyKey")
    user_id = payload.get("userId")

    if not session_id:
        return {"status": "error", "message": "sessionId required"}

    try:
        from google.cloud import firestore
        import os
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
        db = firestore.Client(project=project_id)

        doc_ref = db.collection("sessions").document(session_id)
        doc = doc_ref.get()
        if not doc.exists:
            return {"status": "skipped", "reason": "not_found"}

        data = doc.to_dict()
        derived_ref = doc_ref.collection("derived").document("summary_quick")

        # べき等チェック
        if idempotency_key:
            derived_snap = derived_ref.get()
            if derived_snap.exists:
                current_key = (derived_snap.to_dict() or {}).get("idempotencyKey")
                if current_key and current_key == idempotency_key:
                    return {"status": "skipped", "reason": "idempotent_hit"}

        transcript = await resolve_transcript_text_async(session_id, data) or ""
        mode = data.get("mode", "lecture")

        # ★ Translation sessions: route to translate-specific summary path.
        # Prior behavior (translate→lecture) was a fallback; now translate is a
        # first-class mode. The full ===ORIGINAL===/===TRANSLATION=== transcript
        # is passed through so the LLM can see both sides.
        if mode == "translate" or data.get("importType") == "translate":
            mode = "translate"

        if not transcript:
            derived_ref.set({
                "status": "failed", "errorReason": "Transcript empty",
                "updatedAt": datetime.now(timezone.utc), "idempotencyKey": idempotency_key,
            }, merge=True)
            return {"status": "failed", "reason": "empty_transcript"}

        # LLM呼び出し（Quick: 短いプロンプト、少ないトークン）
        result = await generate_quick_summary(transcript, mode=mode)

        derived_ref.set({
            "status": "succeeded",
            "result": {"markdown": result["markdown"], "topicSummary": result.get("topicSummary", "")},
            "updatedAt": datetime.now(timezone.utc),
            "idempotencyKey": idempotency_key,
        }, merge=True)

        # topicSummary をセッション本体にも保存（一覧表示用）
        if result.get("topicSummary"):
            doc_ref.update({"topicSummary": result["topicSummary"]})

        await publish_session_event(session_id, "assets.updated", {"fields": ["summary_quick"]})
        logger.info(f"[QuickSummary] Completed for {session_id}")
        return {"status": "completed"}

    except Exception as e:
        logger.exception(f"[QuickSummary] Failed for {session_id}")
        try:
            derived_ref.set({
                "status": "failed", "errorReason": str(e),
                "updatedAt": datetime.now(timezone.utc), "idempotencyKey": idempotency_key,
            }, merge=True)
        except Exception:
            pass
        error_str = str(e)
        if "429" in error_str or "503" in error_str or "ResourceExhausted" in error_str:
            raise HTTPException(status_code=503, detail="Transient error, retrying...")
        return {"status": "failed", "error": error_str}


# ═══════════════════════════════════════════════════════════════
# Full Summary Worker
# ═══════════════════════════════════════════════════════════════

@router.post("/internal/tasks/summarize", dependencies=[Depends(verify_cloud_tasks_request)])
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
    # Track AI credits actually deducted so we can refund on failure
    credits_consumed = 0

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

        transcript = await resolve_transcript_text_async(session_id, data) or ""
        mode = data.get("mode", "lecture")

        # ★ Translation sessions: use translate-specific summary path (first-class mode).
        # The ===ORIGINAL===/===TRANSLATION=== bilingual transcript is passed through
        # in full so the translate prompt can draw from both sides.
        import_type = data.get("importType")
        if mode == "translate" or import_type == "translate":
            mode = "translate"

        derived_ref = doc_ref.collection("derived").document("summary")

        if idempotency_key:
            derived_snap = derived_ref.get()
            if derived_snap.exists:
                d_data = derived_snap.to_dict() or {}
                current_key = d_data.get("idempotencyKey")
                current_status = d_data.get("status")
                # Only skip if key matches AND previous run actually succeeded
                # If status is still "running", the previous task may have crashed
                if current_key and current_key == idempotency_key and current_status in ("succeeded", "completed"):
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
            allowed, _meta = await cost_guard.guard_can_consume(cost_guard_id, "summary_generated", 1, mode=cost_guard_mode, user_id=final_user_id)
            if not allowed:
                logger.warning(f"[CostGuard] BLOCKED summary {session_id} for {cost_guard_id}. Monthly limit exceeded.")
                err_msg = "Monthly summary limit exceeded"
                doc_ref.update({"summaryStatus": "locked", "summaryError": err_msg})
                derived_ref.set({"status": "locked", "errorReason": err_msg, "updatedAt": datetime.now(timezone.utc)}, merge=True)
                return {"status": "blocked", "error": err_msg}
            has_consumed = True
            logger.info(f"[CostGuard] Reserved summary_generated for {cost_guard_id} ({cost_guard_mode})")
        elif usage_reserved and cost_guard_id:
            # Quota was already reserved by caller (e.g. finalize); track for refund on failure
            has_consumed = True

        # AI Credits: consume 1 credit for summary
        # Skip when the caller already reserved credits via credits_reservations.reserve()
        # (finalize v2 path sets usage_reserved=True). Otherwise double-charge occurs,
        # because reserve() already deducts credits via ai_credits.consume().
        if cost_guard_id and not usage_reserved:
            _credit_cost = estimate_cost("summary_generated")
            _ok, _info = ai_credits.consume(cost_guard_id, _credit_cost, "summary_generated")
            if _ok:
                credits_consumed = _credit_cost
            else:
                # cost_guard already allowed; ai_credits is a parallel counter (daily soft cap etc.)
                # Proceed with the task but do NOT mark credits_consumed so no refund is issued.
                logger.warning(
                    f"[AICredits] consume skipped for summary {cost_guard_id}: {(_info or {}).get('reason')}"
                )

        # ops_logger: job started
        logger.info(f"Starting summary task for {session_id} job={job_id}")
        log_job_transition(session_id, "summarize", "started", uid=final_user_id, job_id=job_id)

        # ── Progress callback: update derived/summary_progress in real-time ──
        progress_ref = doc_ref.collection("derived").document("summary_progress")

        # Write initial progress immediately so polling picks it up right away
        progress_ref.set({
            "status": "running",
            "phase": "preparing",
            "percent": 0,
            "message": "要約を準備中...",
            "updatedAt": datetime.now(timezone.utc),
        }, merge=True)
        await publish_session_event(session_id, "assets.updated", {"fields": ["summary_progress"]})

        async def _progress_cb(done: int, total: int, phase: str):
            try:
                pct = int(done / max(total, 1) * 100) if phase != "done" else 100
                phase_msg = {"map": "テキストを分析中", "reduce": "要約を生成中", "done": "完了"}.get(phase, phase)
                progress_ref.set({
                    "status": "done" if phase == "done" else "running",
                    "phase": phase,
                    "percent": pct,
                    "message": f"{phase_msg} {done}/{total}" if phase != "done" else "完了",
                    "updatedAt": datetime.now(timezone.utc),
                }, merge=True)
                await publish_session_event(session_id, "assets.updated", {"fields": ["summary_progress"]})
            except Exception as prog_err:
                logger.debug(f"[Progress] update failed (non-blocking): {prog_err}")

        from app.services.llm import get_user_custom_prompts
        summary_instruction, _ = get_user_custom_prompts(final_user_id)

        # Fetch transcript chunks so the summary pipeline can ground each
        # bullet with anchorMs / segmentIds instead of relying on LLM-emitted
        # timestamps (which are unreliable).
        try:
            transcript_segments = await asyncio.to_thread(get_transcript_chunks, session_id)
        except Exception as seg_err:
            logger.warning(f"[summarize] failed to load transcript chunks for anchors: {seg_err}")
            transcript_segments = None

        result = await generate_summary_and_tags(
            transcript,
            mode=mode,
            progress_callback=_progress_cb,
            custom_instruction=summary_instruction,
            segments=transcript_segments,
        )

        # ops_logger: LLM call completed
        log_llm_event(session_id, "summary", "completed", uid=final_user_id, model=GEMINI_MODEL_NAME)
        summary_markdown = result.get("summaryMarkdown")
        summary_json = result.get("summaryJson") or {}
        summary_type = result.get("summaryType") or mode
        summary_json_version = result.get("summaryJsonVersion") or 1
        tags = (result.get("tags") or [])[:4]
        facts = result.get("facts") or []
        suggested_title = result.get("suggestedTitle")

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
        # Auto-update title if LLM suggested one and current title is default/generic
        if suggested_title:
            update_payload["suggestedTitle"] = suggested_title
            current_title = data.get("title", "")
            from app.task_queue import _is_default_title, _build_auto_title
            if not current_title or _is_default_title(current_title):
                auto_title = _build_auto_title(suggested_title, data)
                update_payload["title"] = auto_title
                logger.info(f"[Summary] Auto-updated title: '{current_title}' → '{auto_title}'")
        doc_ref.update(update_payload)
        # Phase 7.10: record transcriptVersion alongside the summary so a
        # transcript re-finalize invalidates stale citations. Clients can
        # detect drift by comparing session.transcriptVersion against
        # derived/summary.meta.transcriptVersion.
        transcript_version = int(data.get("transcriptVersion") or 1)
        derived_ref.set({
            "status": "succeeded",
            "result": {
                "json": summary_json,
                "markdown": summary_markdown,
                "tags": tags,
                "topicSummary": topic_summary,
                **({"suggestedTitle": suggested_title} if suggested_title else {}),
            },
            "meta": {
                "schemaVersion": summary_json_version,
                "type": summary_type,
                "transcriptVersion": transcript_version,
            },
            "modelInfo": {"provider": "vertexai"},
            "updatedAt": datetime.now(timezone.utc),
            "errorReason": None,
            "idempotencyKey": idempotency_key,
            "jobId": job_id,
        }, merge=True)

        # ── Save extracted facts for downstream quiz generation ──
        if facts:
            import hashlib
            transcript_hash = hashlib.sha256(transcript.encode()).hexdigest()[:16]
            facts_ref = doc_ref.collection("derived").document("facts")
            facts_ref.set({
                "facts": facts[:60],  # Cap at 60 facts
                "sourceHash": transcript_hash,
                "updatedAt": datetime.now(timezone.utc),
            }, merge=True)
            logger.info(f"[Facts] Saved {len(facts)} facts for {session_id}")

        logger.info(f"Successfully summarized session {session_id}")

        # [TODO EXTRACTION] Enqueue async TODO extraction (non-blocking)
        if summary_markdown and owner_account_id:
            try:
                from app.task_queue import enqueue_todo_extraction_task
                import hashlib

                summary_hash = hashlib.sha256(summary_markdown.encode()).hexdigest()[:12]
                source_key = f"session:{session_id}:artifact:summary:{summary_hash}"
                doc_ref.update({"todoStatus": "pending"})

                enqueue_todo_extraction_task(
                    session_id=session_id,
                    account_id=owner_account_id,
                    source_key=source_key,
                    summary_text=summary_markdown,
                    transcript_text=transcript,
                    mode=mode,
                    user_id=final_user_id,
                )
                logger.info(f"[TODO] Enqueued async extraction for {session_id} (mode={mode})")
            except Exception as todo_err:
                logger.warning(f"[TODO] Enqueue failed for {session_id} (non-blocking): {todo_err}")

        # [PLAYLIST PRE-GENERATE] Enqueue playlist generation if not already done (non-blocking)
        try:
            existing_playlist = data.get("playlist")
            playlist_status = data.get("playlistStatus")
            if not existing_playlist and playlist_status not in ("running", "completed"):
                from app.task_queue import enqueue_playlist_task
                pl_job_id = f"playlist_{session_id[:8]}"
                enqueue_playlist_task(session_id, job_id=pl_job_id, user_id=final_user_id)
                doc_ref.update({"playlistStatus": "running"})
                logger.info(f"[Playlist] Pre-enqueued after summary for {session_id}")
        except Exception as pl_err:
            logger.warning(f"[Playlist] Pre-enqueue failed for {session_id} (non-blocking): {pl_err}")

        if job_id:
            db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "completed"}, merge=True)

        # ops_logger: job completed
        log_job_transition(session_id, "summarize", "completed", uid=final_user_id, job_id=job_id)
        # Log usage success
        await usage_logger.log(user_id=final_user_id, feature="summary", event_type="success", session_id=session_id)
        # Record success in monthly doc for accurate remaining count
        if cost_guard_id:
            await cost_guard.record_success(cost_guard_id, "summary_generated", mode=cost_guard_mode)

        # [FIX] summary_progress watchdog: unconditionally close the progress doc on success.
        # Without this, derived/summary_progress stays at phase=preparing even when derived/summary
        # has already succeeded (observed on master session fe21722a-... on 2026-04-18).
        # Writes "completed" because _map_derived_status has no entry for "done" (→ falls back to "pending").
        try:
            progress_ref.set({
                "status": "completed",
                "phase": "completed",
                "percent": 100,
                "message": "要約が完成しました",
                "updatedAt": datetime.now(timezone.utc),
            }, merge=True)
            await publish_session_event(session_id, "assets.updated", {"fields": ["summary_progress"]})
        except Exception as prog_err:
            logger.warning(f"[summary_progress] final update failed (non-blocking): {prog_err}")

        await publish_session_event(session_id, "assets.updated", {"fields": ["summary"]})
        return {"status": "completed"}

    except Exception as e:
        # [FIX] Refund quota on failure
        if has_consumed and cost_guard_id:
            await cost_guard.refund_consumption(cost_guard_id, "summary_generated", 1, mode=cost_guard_mode)
            logger.info(f"[CostGuard] Refunded summary_generated for {cost_guard_id} due to failure")
        # Refund AI credits (monthly/daily counter) on failure
        if credits_consumed and cost_guard_id:
            try:
                ai_credits.refund(cost_guard_id, credits_consumed, "summary_generated")
            except Exception as refund_err:
                logger.warning(f"[AICredits] Refund failed for summary {cost_guard_id}: {refund_err}")

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
             # [FIX] summary_progress watchdog: close progress doc on failure too.
             progress_ref.set({
                 "status": "failed",
                 "phase": "failed",
                 "percent": 0,
                 "message": "要約の生成に失敗しました",
                 "errorReason": str(e)[:300],
                 "updatedAt": datetime.now(timezone.utc),
             }, merge=True)
             await publish_session_event(session_id, "assets.updated", {"fields": ["summary", "summary_progress"]})
        except Exception as db_err:
            logger.warning(f"[summarize] Failed to update error status in DB: {db_err}")

        # ops_logger: job failed
        log_job_transition(session_id, "summarize", "failed", uid=final_user_id, job_id=job_id, error_code=ErrorCode.JOB_WORKER_500, error_message=str(e))

        return {"status": "failed", "error": str(e)}

    # finally: removed



@router.post("/internal/tasks/import_youtube", dependencies=[Depends(verify_cloud_tasks_request)])
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

        # [DISABLED] Auto-summary removed — user triggers manually via generate button

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

@router.post("/internal/tasks/quiz", dependencies=[Depends(verify_cloud_tasks_request)])
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
    Cloud Tasks Quiz Worker (single-shot, JSON output).
    重複防止のため全問を1回のLLM呼び出しで生成する。
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
    count = payload.get("count", 8)
    user_id = payload.get("userId")
    usage_reserved = bool(payload.get("usageReserved"))

    if not session_id:
        return {"status": "error", "message": "session_id required"}

    final_user_id = user_id

    # Initialize cost_guard variables at top level for exception handler scope
    cost_guard_id = None
    cost_guard_mode = "user"
    has_consumed = False
    credits_consumed = 0

    try:
        # Initialize DB locally
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

        # Resolve account ID for cost guard
        owner_account_id = data.get("ownerAccountId")
        cost_guard_id = owner_account_id or final_user_id
        cost_guard_mode = "account" if owner_account_id else "user"

        mode = data.get("mode", "lecture")
        # ★ Translation sessions: normalize mode for quiz generation
        if mode == "translate":
            mode = "lecture"

        derived_ref = doc_ref.collection("derived").document("quiz")

        # ── Idempotency check ──
        if idempotency_key:
            derived_snap = derived_ref.get()
            if derived_snap.exists:
                d_data = derived_snap.to_dict() or {}
                current_key = d_data.get("idempotencyKey")
                current_status = d_data.get("status")
                # Only skip if key matches AND previous run actually succeeded
                if current_key and current_key == idempotency_key and current_status in ("succeeded", "completed"):
                    if job_id:
                        db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "completed", "result": "cached"}, merge=True)
                    return {"status": "skipped", "reason": "idempotent_hit"}

        # ── Resolve input: Facts or transcript ──
        facts_ref = doc_ref.collection("derived").document("facts")
        facts_snap = facts_ref.get()
        facts = (facts_snap.to_dict() or {}).get("facts", []) if facts_snap.exists else []

        if not facts:
            transcript = await resolve_transcript_text_async(session_id, data) or ""
            if not transcript:
                doc_ref.update({"quizStatus": "failed", "quizError": "Transcript empty", "status": "録音済み"})
                if job_id:
                    db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "failed", "errorReason": "Transcript empty"}, merge=True)
                derived_ref.set({
                    "status": "failed", "errorReason": "Transcript empty",
                    "updatedAt": datetime.now(timezone.utc), "idempotencyKey": idempotency_key,
                }, merge=True)
                await publish_session_event(session_id, "assets.updated", {"fields": ["quiz"]})
                return {"status": "failed"}
        else:
            transcript = ""
            logger.info(f"[Quiz] Using {len(facts)} facts instead of transcript for {session_id}")

        if job_id:
            db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "running"}, merge=True)

        doc_ref.update({
            "quizStatus": "running",
            "quizError": None,
            "status": "テスト生成",
        })

        # CostGuard
        if not usage_reserved and cost_guard_id:
            allowed, _meta = await cost_guard.guard_can_consume(cost_guard_id, "quiz_generated", 1, mode=cost_guard_mode, user_id=final_user_id)
            if not allowed:
                logger.warning(f"[CostGuard] BLOCKED quiz {session_id} for {cost_guard_id}. Monthly limit exceeded.")
                err_msg = "Monthly quiz limit exceeded"
                doc_ref.update({"quizStatus": "locked", "quizError": err_msg})
                derived_ref.set({"status": "locked", "errorReason": err_msg, "updatedAt": datetime.now(timezone.utc)}, merge=True)
                return {"status": "blocked", "error": err_msg}
            has_consumed = True
            logger.info(f"[CostGuard] Reserved quiz_generated for {cost_guard_id} ({cost_guard_mode})")
        elif usage_reserved and cost_guard_id:
            has_consumed = True

        # AI Credits: consume 2 credits for quiz
        # Skip when already reserved via credits_reservations.reserve() (finalize v2).
        if cost_guard_id and not usage_reserved:
            _credit_cost = estimate_cost("quiz_generated")
            _ok, _info = ai_credits.consume(cost_guard_id, _credit_cost, "quiz_generated")
            if _ok:
                credits_consumed = _credit_cost
            else:
                logger.warning(
                    f"[AICredits] consume skipped for quiz {cost_guard_id}: {(_info or {}).get('reason')}"
                )

        log_job_transition(session_id, "quiz", "started", uid=final_user_id, job_id=job_id)

        # ── Generate quiz (single-shot JSON) ──
        from app.services.llm import get_user_custom_prompts
        _, quiz_instruction = get_user_custom_prompts(final_user_id)
        quiz_data = await generate_quiz_json(
            source_text=transcript,
            facts=facts if facts else None,
            mode=mode,
            count=count,
            custom_instruction=quiz_instruction,
        )

        if not quiz_data or not quiz_data.get("questions"):
            # JSON生成失敗時: レガシーMarkdown方式にフォールバック
            logger.warning(f"[Quiz] JSON generation failed for {session_id}, falling back to legacy")
            if facts:
                quiz_md = await generate_quiz_batch(
                    facts=facts, mode=mode, count=count, batch_index=0, total_batches=1,
                )
            else:
                quiz_raw = await generate_quiz(transcript, mode=mode, count=count)
                quiz_md = clean_quiz_markdown(quiz_raw)
            quiz_json_str = None
        else:
            quiz_md = quiz_json_to_markdown(quiz_data)
            quiz_json_str = json.dumps(quiz_data, ensure_ascii=False)

        log_llm_event(session_id, "quiz", "completed", uid=final_user_id, model=GEMINI_MODEL_NAME)

        # ── Save results ──
        update_fields = {
            "quizStatus": "completed",
            "quizMarkdown": quiz_md,
            "quizUpdatedAt": datetime.now(timezone.utc),
            "quizError": None,
            "status": "テスト完了",
        }
        if quiz_json_str:
            update_fields["quizJson"] = quiz_json_str
        doc_ref.update(update_fields)

        derived_result = {"markdown": quiz_md, "count": len(quiz_data.get("questions", [])) if quiz_data else count}
        if quiz_json_str:
            derived_result["json"] = quiz_data  # ★ FIX: Store as dict, not JSON string (iOS expects [String: JSONValue])
        derived_ref.set({
            "status": "succeeded",
            "result": derived_result,
            "modelInfo": {"provider": "vertexai"},
            "updatedAt": datetime.now(timezone.utc),
            "errorReason": None,
            "idempotencyKey": idempotency_key,
        }, merge=True)

        if job_id:
            db.collection("sessions").document(session_id).collection("jobs").document(job_id).set({"status": "completed"}, merge=True)

        log_job_transition(session_id, "quiz", "completed", uid=final_user_id, job_id=job_id)
        await usage_logger.log(user_id=final_user_id, feature="quiz", event_type="success", session_id=session_id)
        if cost_guard_id:
            await cost_guard.record_success(cost_guard_id, "quiz_generated", mode=cost_guard_mode)

        await publish_session_event(session_id, "assets.updated", {"fields": ["quiz"]})
        return {"status": "completed"}

    except Exception as e:
        # Refund quota on failure
        if has_consumed and cost_guard_id:
            await cost_guard.refund_consumption(cost_guard_id, "quiz_generated", 1, mode=cost_guard_mode)
            logger.info(f"[CostGuard] Refunded quiz_generated for {cost_guard_id} due to failure")
        if credits_consumed and cost_guard_id:
            try:
                ai_credits.refund(cost_guard_id, credits_consumed, "quiz_generated")
            except Exception as refund_err:
                logger.warning(f"[AICredits] Refund failed for quiz {cost_guard_id}: {refund_err}")

        logger.exception(f"Quiz generation failed for session {session_id}")

        log_llm_event(session_id, "quiz", "failed", uid=final_user_id, error_code=ErrorCode.VERTEX_SCHEMA_PARSE_ERROR, error_message=str(e))
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

        log_job_transition(session_id, "quiz", "failed", uid=final_user_id, job_id=job_id, error_code=ErrorCode.JOB_WORKER_500, error_message=str(e))

        return {"status": "failed", "error": str(e)}

@router.post("/internal/tasks/playlist", include_in_schema=False, dependencies=[Depends(verify_cloud_tasks_request)])
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

        transcript = await resolve_transcript_text_async(session_id, data)
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
        logger.info(f"[Playlist] Generating for {session_id}, transcript={len(transcript)}chars, segments={'yes' if segments else 'no'}, duration={duration}")
        playlist_json_str = await generate_playlist_timeline(transcript, segments=segments, duration_sec=duration)
        logger.info(f"[Playlist] LLM raw response for {session_id}: len={len(playlist_json_str)}, preview={playlist_json_str[:300]}")

        try:
            raw_items = json.loads(playlist_json_str)
        except Exception as parse_err:
            logger.warning(f"[Playlist] JSON parse error for {session_id}: {parse_err}, raw={playlist_json_str[:200]}")
            raw_items = []

        logger.info(f"[Playlist] raw_items={len(raw_items)} for {session_id}")

        # [NEW] Normalize: ms/sec detection, duration clamp, validation
        items = normalize_playlist_items(raw_items, segments=segments, duration_sec=duration)
        logger.info(f"[Playlist] After normalize: items={len(items)} for {session_id}")

        if not items:
            logger.warning(f"[Playlist] Empty after normalize for {session_id}, raw_items={len(raw_items)}, marking as failed")
            ts = datetime.now(timezone.utc)
            doc_ref.update({"playlistStatus": "failed", "playlistError": "Empty playlist from LLM", "playlistUpdatedAt": ts})
            derived_ref.set({"status": "failed", "errorReason": "empty_result", "updatedAt": ts, "jobId": job_id}, merge=True)
            return {"status": "failed", "reason": "empty_playlist"}

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


@router.post("/internal/tasks/highlights", dependencies=[Depends(verify_cloud_tasks_request)])
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


@router.post("/internal/tasks/summary_v2", dependencies=[Depends(verify_cloud_tasks_request)])
async def handle_summary_v2_task(request: Request):
    """SummaryV2 Cloud Tasks worker (PR1).

    Pipeline (spec §7.2):
      1. derived doc → running (via firestore_summary_v2.write_summary_v2_running)
      2. transcript + chunks load
      3. transcriptHash compute
      4. mode / participants / purpose resolve
      5. build_summary_v2_prompt + LLM call (delegates to generate_summary_v2)
      6. parse / validate (SummaryV2 locks)
      7. anchor resolution (v0.1 structural)
      8. quality gate (v0.1 pass-through)
      9. user edit merge (hidden / userEdited)
     10. markdown render
     11. Firestore write (derived + session mirror)
     12. event + usage log

    Errors map to errorReason per spec §13.2.
    """
    import time as _time
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    session_id = payload.get("sessionId")
    job_id = payload.get("jobId")
    user_id = payload.get("userId")
    meeting_purpose = payload.get("meetingPurpose")
    meeting_type = payload.get("meetingType")
    participants = payload.get("participants", [])
    idempotency_key = payload.get("idempotencyKey")

    if not session_id:
        return {"status": "error", "message": "sessionId required"}

    from google.cloud import firestore
    import os as _os
    from app.services.firestore_summary_v2 import (
        write_summary_v2_running,
        write_summary_v2_success,
        write_summary_v2_failed,
        sync_session_summary_v2_fields,
        get_summary_v2_doc,
    )
    from app.services.summary_v2 import (
        SUMMARY_V2_PROMPT_VERSION,
        compute_transcript_hash,
        generate_summary_v2,
        merge_user_edited_items,
        render_summary_v2_markdown,
    )
    from app.services.anchor_resolver import (
        apply_quality_gate_v2,
        build_chunks_by_id,
        resolve_all_anchors_v2,
    )
    from app.util_models import SummaryV2, SummaryV2Item

    project_id = _os.environ.get("GOOGLE_CLOUD_PROJECT") or _os.environ.get("GCP_PROJECT")
    db = firestore.Client(project=project_id)
    doc_ref = db.collection("sessions").document(session_id)

    started_ms = int(_time.time() * 1000)

    # Step 2: load session + transcript + chunks.
    try:
        doc = doc_ref.get()
        if not doc.exists:
            return {"status": "skipped", "reason": "not_found"}
        data = doc.to_dict() or {}

        transcript = await resolve_transcript_text_async(session_id, data)
        if not transcript:
            logger.error("[summary_v2] empty transcript for %s", session_id)
            write_summary_v2_failed(
                session_id, error_reason="empty_transcript", job_id=job_id,
            )
            return {"status": "failed", "reason": "empty_transcript"}

        try:
            chunks = get_transcript_chunks(session_id)
        except Exception:
            chunks = []

        transcript_hash = compute_transcript_hash(chunks, fallback_text=transcript)
        mode_for_meta = (meeting_type or data.get("mode") or "other")
        resolved_idem = idempotency_key or (
            f"sumv2:{session_id}:{transcript_hash[:16]}:{SUMMARY_V2_PROMPT_VERSION}"
        )

        logger.info(
            "event=summary_v2_task_started sessionId=%s jobId=%s idempotencyKey=%s promptVersion=%s",
            session_id, job_id, resolved_idem, SUMMARY_V2_PROMPT_VERSION,
        )

        # Step 1: Mark running (idempotent write).
        write_summary_v2_running(
            session_id,
            idempotency_key=resolved_idem,
            job_id=job_id,
            prompt_version=SUMMARY_V2_PROMPT_VERSION,
            transcript_hash=transcript_hash,
            mode=mode_for_meta,
        )

        # Step 5–6: delegate to the existing generate_summary_v2() orchestrator
        # which already calls the LLM and returns a SummaryV2. PR1 keeps the
        # internal pipeline as-is; the new prompt builder / validator are
        # wired via the v0.2 follow-up.
        summary: SummaryV2 = await generate_summary_v2(
            session_id=session_id,
            transcript_text=transcript,
            diarized_segments=data.get("diarizedSegments"),
            user_marks=data.get("userMarks"),
            meeting_purpose=meeting_purpose or data.get("meetingPurpose"),
            meeting_type=meeting_type or data.get("mode"),
            participants=participants or data.get("participants", []),
        )

        # Step 7: anchor resolution (v0.1 structural).
        chunks_by_id = build_chunks_by_id(chunks)
        resolved_items: list[SummaryV2Item] = resolve_all_anchors_v2(summary.items, chunks_by_id)

        # Step 8: quality gate (v0.1 pass-through + aggregation).
        kept_items, quality, _filtered = apply_quality_gate_v2(resolved_items)

        # Step 9: user edit merge — preserve userEdited / hidden items.
        prior_doc = get_summary_v2_doc(session_id)
        old_result = (prior_doc or {}).get("result") if prior_doc else None
        old_items: list[SummaryV2Item] = []
        if isinstance(old_result, dict):
            try:
                old_items = [SummaryV2Item(**it) for it in (old_result.get("items") or [])]
            except Exception as parse_err:
                logger.warning("[summary_v2] failed to parse old items for merge: %s", parse_err)
                old_items = []
        merged_items = merge_user_edited_items(old_items, kept_items)

        # Apply merged items + recomputed quality back onto the summary.
        summary = summary.model_copy(update={
            "items": merged_items,
            "quality": quality,
            "version": 2,
            "schemaVersion": "2.0",
        })

        # Step 10: markdown render (regenerate with final item list).
        summary = summary.model_copy(update={
            "renderedMarkdown": render_summary_v2_markdown(summary),
        })

        # Step 11: Firestore write (derived + session mirror).
        latency_ms = int(_time.time() * 1000) - started_ms
        model_info = {
            "provider": "vertex",
            "model": "gemini-2.5-flash",
            "promptVersion": SUMMARY_V2_PROMPT_VERSION,
            "tokensIn": None,   # PR1: not captured from generate_summary_v2 yet
            "tokensOut": None,
            "latencyMs": latency_ms,
        }
        meta = {
            "transcriptVersion": 1,
            "transcriptHash": transcript_hash,
            "mode": mode_for_meta,
            "idempotencyKey": resolved_idem,
        }
        summary_result = summary.model_dump()
        write_summary_v2_success(
            session_id,
            summary_result=summary_result,
            model_info=model_info,
            meta=meta,
        )
        sync_session_summary_v2_fields(session_id, summary_result=summary_result)

        # Step 12: usage log (structured).
        logger.info(
            "event=summary_v2_firestore_written sessionId=%s jobId=%s latencyMs=%d "
            "filteredCount=%d avgConfidence=%.3f itemCount=%d",
            session_id, job_id, latency_ms,
            int(quality.filteredCount or 0),
            float(quality.avgConfidence or 0.0),
            len(merged_items),
        )
        try:
            # Also publish asset-updated event when available.
            await publish_session_event(
                session_id, "assets.updated", {"fields": ["summary_v2", "title"]}
            )
        except Exception:
            pass

        return {"status": "completed"}

    except Exception as e:
        logger.exception("[summary_v2] worker failed for %s: %s", session_id, e)
        try:
            write_summary_v2_failed(
                session_id,
                error_reason=str(e)[:500] or "unknown_error",
                job_id=job_id,
            )
        except Exception as db_err:
            logger.error("[summary_v2] failed to write failed-state: %s", db_err)

        logger.info(
            "event=summary_v2_task_failed sessionId=%s jobId=%s errorReason=%s",
            session_id, job_id, str(e)[:200],
        )
        return {"status": "failed", "error": str(e)[:500]}


# [REMOVED] Duplicate deprecated playlist handler - the real implementation is at line 574
# FastAPI registers routes in order, and having two handlers for the same path
# causes the second one to override the first.

# =============================================================================
# PR2 — Entity Review internal worker
# =============================================================================

@router.post(
    "/internal/tasks/entity-review-run",
    dependencies=[Depends(verify_cloud_tasks_request)],
)
async def handle_entity_review_run_task(request: Request):
    """Run entity-review candidate extraction from canonical transcript.

    Idempotent: if the latest review is already pending we short-circuit so
    Cloud Tasks retries don't produce duplicate review docs.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    session_id = payload.get("sessionId")
    user_id = payload.get("userId")
    if not session_id:
        return {"status": "error", "message": "sessionId required"}

    from app.services import entity_review_services as svc
    from app.services import entity_review_store as store

    # Idempotency: skip if latest review already pending.
    latest = store.get_latest_review(session_id)
    if latest and latest.get("status") == "pending":
        logger.info(
            "[entity_review_run] skipping: session=%s already has pending review %s",
            session_id, latest.get("reviewId"),
        )
        return {"status": "skipped", "reason": "already_pending"}

    canonical = store.get_canonical_transcript(session_id)
    if not canonical:
        logger.info("[entity_review_run] skipping: no canonical for %s", session_id)
        return {"status": "skipped", "reason": "canonical_missing"}

    known_terms: list[str] = []
    if user_id:
        for t in store.list_terms_for_user(user_id):
            c = t.get("canonical")
            if c:
                known_terms.append(c)
            for a in t.get("aliases") or []:
                if a and a not in known_terms:
                    known_terms.append(a)

    candidates = svc.build_candidates(
        text=canonical.get("text") or "",
        known_terms=known_terms,
    )
    review = store.create_review(
        session_id,
        source_transcript_version=int(canonical.get("version", 1)),
        candidate_count=len(candidates),
        language=canonical.get("language", "ja"),
    )
    if candidates:
        store.save_candidates(session_id, review["reviewId"], candidates)
    store.update_entity_review_status(
        session_id,
        "pending" if candidates else "none",
        review["reviewId"] if candidates else None,
    )
    logger.info(
        "event=entity_review_run sessionId=%s reviewId=%s candidates=%d",
        session_id, review["reviewId"], len(candidates),
    )
    return {"status": "completed", "reviewId": review["reviewId"], "candidates": len(candidates)}


# =============================================================================
# PR E — Derived Finalize Worker
# =============================================================================
# /internal/tasks/derived/finalize は finalize v2 (PR D) の per-feature 派生ジョブ
# ディスパッチャ。`enqueue_derived_finalize_task` (app/task_queue.py) から投入される。
#
# 役割:
#   1. payload {sessionId, feature, reservationId, ...} を読む
#   2. feature ごとに既存 legacy enqueue 関数 (summarize / quiz_batch / highlights) を呼ぶ
#   3. dispatch 成功時に credits_reservations.commit(feature)
#   4. dispatch 失敗時に credits_reservations.release(feature, reason)
#
# ⚠️  PR E は thin dispatcher (Option A)。
#     commit は legacy worker への enqueue 成功時点で行うため、legacy worker が
#     最終的に失敗しても credits は refund されない楽観的会計になる。
#     PR F で各 legacy worker が成功 / 失敗時に commit / release を直接呼ぶよう
#     refactor することで完全な credits 整合性を獲得する想定。

@router.post("/internal/tasks/derived/finalize", dependencies=[Depends(verify_cloud_tasks_request)])
async def handle_derived_finalize_task(request: Request):
    """PR E: per-feature derived worker dispatcher.

    Idempotency:
        Cloud Tasks の retry に対しては、各 legacy enqueue 関数が自前の
        idempotency_key (= reservation_id ベース) で重複を抑止する。
        commit/release も冪等に書けるよう credits_reservations 側で保証されている。
    """
    from app.services import credits_reservations
    from app.services.audit import emit as audit_emit
    from app.task_queue import (
        enqueue_summarize_task,
        enqueue_quiz_batch_tasks,
        enqueue_generate_highlights_task,
    )

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid json: {exc}")

    session_id = body.get("sessionId")
    feature = body.get("feature")
    reservation_id = body.get("reservationId")
    user_id = body.get("userId")
    operation_id = body.get("operationId")
    client_request_id = body.get("clientRequestId") or reservation_id

    if not session_id or not feature or not reservation_id:
        raise HTTPException(
            status_code=400,
            detail="sessionId, feature, reservationId are required",
        )

    audit_emit(
        f"{feature}.job.dispatch_requested",
        session_id=session_id,
        uid=user_id,
        operation_id=operation_id,
        request_id=client_request_id,
        reservationId=reservation_id,
        feature=feature,
    )

    idem_key = f"derived:{reservation_id}:{feature}"

    try:
        if feature == "summary":
            enqueue_summarize_task(
                session_id,
                user_id=user_id,
                idempotency_key=idem_key,
                usage_reserved=True,
            )
        elif feature == "summary_v2":
            # PR1: dispatch Summary v2 via the same Cloud Tasks worker
            # used by manual POST :generate so the pipeline is single-path.
            from app.task_queue import enqueue_summary_v2_task
            enqueue_summary_v2_task(
                session_id,
                user_id=user_id,
                idempotency_key=idem_key,
            )
        elif feature == "summary_quick":
            # PR1: quick-summary auto-dispatch is opt-in. The worker endpoint
            # already exists (/internal/tasks/summarize_quick); we reuse it.
            try:
                from app.task_queue import enqueue_summarize_quick_task
                enqueue_summarize_quick_task(
                    session_id,
                    user_id=user_id,
                    idempotency_key=idem_key,
                )
            except ImportError:
                logger.warning(
                    "[finalize] summary_quick requested but enqueue function absent; skipping"
                )
        elif feature == "quiz":
            enqueue_quiz_batch_tasks(
                session_id,
                user_id=user_id,
                usage_reserved=True,
            )
        elif feature == "highlights":
            enqueue_generate_highlights_task(
                session_id,
                user_id=user_id,
            )
        else:
            # 未知 feature: reservation を release してエラー応答
            credits_reservations.release(
                session_id=session_id,
                reservation_id=reservation_id,
                feature=feature,
                reason=f"unknown_feature:{feature}",
            )
            audit_emit(
                f"{feature}.job.unknown_feature",
                severity="ERROR",
                session_id=session_id,
                uid=user_id,
                operation_id=operation_id,
                request_id=client_request_id,
                feature=feature,
            )
            raise HTTPException(status_code=400, detail=f"unknown feature: {feature}")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            f"[derived_finalize] dispatch failed session={session_id} feature={feature}"
        )
        try:
            credits_reservations.release(
                session_id=session_id,
                reservation_id=reservation_id,
                feature=feature,
                reason=f"dispatch_error:{type(exc).__name__}",
            )
        except Exception:
            logger.exception("[derived_finalize] release after dispatch failure also failed")
        audit_emit(
            f"{feature}.job.dispatch_failed",
            severity="ERROR",
            session_id=session_id,
            uid=user_id,
            operation_id=operation_id,
            request_id=client_request_id,
            error=str(exc),
        )
        # Cloud Tasks に retry を促すため 500 を返す
        raise HTTPException(status_code=500, detail=f"dispatch failed: {exc}")

    # dispatch 成功 → optimistic commit
    try:
        credits_reservations.commit(
            session_id=session_id,
            reservation_id=reservation_id,
            feature=feature,
        )
    except Exception as exc:
        # commit 失敗は致命ではない (reservation は reserved のまま残る) が監査する
        logger.warning(
            f"[derived_finalize] commit failed session={session_id} feature={feature}: {exc}"
        )
        audit_emit(
            f"{feature}.job.commit_failed",
            severity="WARN",
            session_id=session_id,
            uid=user_id,
            operation_id=operation_id,
            request_id=client_request_id,
            error=str(exc),
        )

    audit_emit(
        f"{feature}.job.dispatched",
        session_id=session_id,
        uid=user_id,
        operation_id=operation_id,
        request_id=client_request_id,
        reservationId=reservation_id,
        feature=feature,
    )

    return {
        "ok": True,
        "sessionId": session_id,
        "feature": feature,
        "reservationId": reservation_id,
        "status": "dispatched",
    }


@router.post("/internal/tasks/audio-cleanup", dependencies=[Depends(verify_cloud_tasks_request)])
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


@router.post("/internal/tasks/merge_migration", dependencies=[Depends(verify_cloud_tasks_request)])
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


@router.post("/internal/tasks/account_migration", dependencies=[Depends(verify_cloud_tasks_request)])
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


@router.post("/internal/tasks/todo_extraction", dependencies=[Depends(verify_cloud_tasks_request)])
async def handle_todo_extraction_task(request: Request):
    """
    [NEW 2026-02] 非同期TODO抽出タスクハンドラ。
    要約完了後にCloud Tasksから呼び出される。
    """
    from app.services.todo_extractor import update_todos_from_summary
    from app.firebase import db

    try:
        payload = await request.json()
        session_id = payload.get("sessionId")
        account_id = payload.get("accountId")
        source_key = payload.get("sourceKey")
        summary_text = payload.get("summaryText", "")
        transcript_text = payload.get("transcriptText", "")
        mode = payload.get("mode", "lecture")

        if not session_id or not account_id or not summary_text:
            return {"status": "skipped", "reason": "missing_required_fields"}

        # Update status to processing
        doc_ref = db.collection("sessions").document(session_id)
        doc_ref.update({
            "todoStatus": "processing",
            "todoUpdatedAt": datetime.now(timezone.utc),
        })

        # Extract TODOs
        todo_stats = await update_todos_from_summary(
            session_id=session_id,
            account_id=account_id,
            source_key=source_key,
            summary_text=summary_text,
            transcript_text=transcript_text,
            mode=mode,
        )

        # Update session with results
        doc_ref.update({
            "todoStatus": "completed",
            "todoUpdatedAt": datetime.now(timezone.utc),
            "todoStats": todo_stats,
        })

        logger.info(f"[TODO] Async extraction completed for {session_id}: created={todo_stats.get('created')}")
        return {"status": "completed", "stats": todo_stats}

    except Exception as e:
        logger.exception(f"[TODO] Async extraction failed for session")
        # Mark as failed but don't cause retry (return 200)
        try:
            if session_id:
                doc_ref = db.collection("sessions").document(session_id)
                doc_ref.update({
                    "todoStatus": "failed",
                    "todoError": str(e)[:200],
                    "todoUpdatedAt": datetime.now(timezone.utc),
                })
        except Exception:
            pass
        return {"status": "failed", "error": str(e)[:200]}


@router.post("/internal/tasks/timeline", dependencies=[Depends(verify_cloud_tasks_request)])
async def handle_timeline_task(request: Request):
    """Chapter-style timeline generation Cloud Tasks handler."""
    from app.services.timeline_service import build_session_timeline
    from app.routes.jobs import start_job, complete_job, fail_job

    session_id = None
    job_id = None
    try:
        payload = await request.json()
        session_id = payload.get("sessionId")
        job_id = payload.get("jobId")
        force = bool(payload.get("force"))

        if not session_id:
            return {"status": "skipped", "reason": "missing_sessionId"}

        if job_id and start_job(job_id) is None:
            logger.info(f"[timeline task] job {job_id} already terminal — skip")
            return {"status": "skipped", "reason": "job_already_terminal"}

        result = await build_session_timeline(session_id, force=force)

        if job_id:
            if result.get("status") == "succeeded":
                complete_job(job_id, result_url=f"/sessions/{session_id}/artifacts/timeline")
            else:
                fail_job(job_id, error_reason=str(result.get("reason") or "failed"))

        return {"status": result.get("status", "unknown"), "result": result}

    except Exception as e:
        logger.exception(f"[timeline task] failed session={session_id}")
        if job_id:
            try:
                fail_job(job_id, error_reason=str(e)[:200])
            except Exception:
                pass
        # Return 200 to avoid Cloud Tasks retry storm on deterministic failures.
        return {"status": "failed", "error": str(e)[:200]}


@router.post("/internal/tasks/daily-usage-aggregation", dependencies=[Depends(verify_cloud_tasks_request)])
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


@router.post("/internal/tasks/build_transcript_chunk_index", dependencies=[Depends(verify_cloud_tasks_request)])
async def handle_build_transcript_chunk_index(request: Request):
    """Phase 7.6 prep — rebuild /transcript_chunks index for a single session.

    Payload: {"sessionId": "...", "transcriptVersion"?: number}

    Idempotent: replaces any previous rows keyed by sessionId. Safe to retry
    (Cloud Tasks native retry semantics apply).
    """
    from app.jobs.build_transcript_chunk_index import build_transcript_chunk_index_for_session
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    session_id = payload.get("sessionId")
    if not session_id:
        return {"status": "error", "message": "sessionId required"}
    try:
        return build_transcript_chunk_index_for_session(
            session_id,
            transcript_version=payload.get("transcriptVersion"),
        )
    except Exception as e:
        logger.exception(f"build_transcript_chunk_index failed for {session_id}")
        return {"status": "failed", "error": str(e)[:300]}


@router.post("/internal/tasks/build_summary_evidence_index", dependencies=[Depends(verify_cloud_tasks_request)])
async def handle_build_summary_evidence_index(request: Request):
    """Phase 7.6 prep — backfill /summary_evidence_index for a session.

    Payload: {"sessionId": "..."}

    Reads `sessions/{id}/derived/summary.result.json` (canonical) or legacy
    `derived/summary_v2`, flattens every bullet into /summary_evidence_index.
    Idempotent.
    """
    from app.jobs.build_summary_evidence_index import backfill_summary_evidence_for_session
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    session_id = payload.get("sessionId")
    if not session_id:
        return {"status": "error", "message": "sessionId required"}
    try:
        return backfill_summary_evidence_for_session(session_id)
    except Exception as e:
        logger.exception(f"build_summary_evidence_index failed for {session_id}")
        return {"status": "failed", "error": str(e)[:300]}


@router.post("/internal/tasks/account-deletion-sweep", dependencies=[Depends(verify_cloud_tasks_request)])
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





@router.post("/internal/tasks/qa", dependencies=[Depends(verify_cloud_tasks_request)])
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

        transcript = await resolve_transcript_text_async(session_id, data) or ""

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


@router.post("/internal/tasks/translate", dependencies=[Depends(verify_cloud_tasks_request)])
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

        transcript = await resolve_transcript_text_async(session_id, data) or ""

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


@router.post("/internal/tasks/transcribe", dependencies=[Depends(verify_cloud_tasks_request)])
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

        # [FIX] STT Idempotency: Skip if batch STT already completed (prevents re-billing on retries)
        if data.get("batchRetranscribeUsed") and not force:
            logger.info(f"[Transcribe] SKIPPED for session {session_id}: batchRetranscribeUsed=True (already processed, idempotency guard)")
            if job_ref:
                job_ref.set({
                    "status": "completed",
                    "result": "skipped_already_processed",
                    "reason": "Batch STT already completed (idempotency)",
                    "completedAt": datetime.now(timezone.utc)
                }, merge=True)
            return {"status": "skipped", "reason": "already_processed"}

        transcription_mode = data.get("transcriptionMode") or ""
        transcript_source = data.get("transcriptSource") or ""
        existing_transcript = data.get("transcriptText") or ""

        # On-Device / device_sherpa mode: Skip batch STT (uses local transcription only)
        if transcription_mode in ["on_device", "local", "offline", "device_sherpa"]:
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

        # LOG USAGE for Billing (with idempotency guard)
        # [FIX] Only log once per session — prevents inflation from Cloud Tasks retries
        if not data.get("transcribeUsageLogged"):
            try:
                usage_sec = float(data.get("durationSec") or 0.0)
                if usage_sec == 0.0 and segments:
                    last = segments[-1]
                    usage_sec = float(last.get("end", 0.0) or last.get("endSec", 0.0))

                if usage_sec > 0:
                    uid = data.get("ownerUserId") or data.get("userId") or "unknown_task_user"
                    if not final_user_id: final_user_id = uid

                    # [FIX] Determine type from actual transcriptionMode, not hardcoded "cloud"
                    usage_type = "cloud" if transcription_mode == "cloud_google" else "on_device"

                    await usage_logger.log(
                        user_id=uid,
                        session_id=session_id,
                        feature="transcribe",
                        event_type="success",
                        payload={
                            "recording_sec": usage_sec,
                            "type": usage_type,
                            "mode": data.get("mode"),
                            "engine": engine
                        }
                    )
                    # Mark as logged to prevent duplicate on retry
                    updates["transcribeUsageLogged"] = True
            except Exception as e:
                logger.error(f"Failed to log usage for session {session_id}: {e}")
        else:
            logger.info(f"[transcribe] Skipping usage log for {session_id} — already logged")

        doc_ref.update(updates)
        if job_ref: job_ref.set({"status": "completed"}, merge=True)

        logger.info(f"Transcription Success for {session_id}")

        # ops_logger: job completed
        log_job_transition(session_id, "transcribe", "completed", uid=final_user_id, job_id=job_id)

        # [finalize v2 Step 1] canonical import 完了を記録。
        # batchRetranscribeUsed guard により二重書き込みは起きない(MEMORY.md 記載の idempotency)。
        try:
            server_chunk_count = count_transcript_chunks(session_id)
            mark_import_completed(
                session_id,
                chunk_count=server_chunk_count,
                last_chunk_index=None,
                source="cloud",
            )
            audit_emit(
                "import_state.completed",
                session_id=session_id,
                uid=final_user_id,
                source="cloud",
                chunkCount=server_chunk_count,
                via="transcribe_worker",
            )
        except Exception as exc:
            logger.warning(f"[finalize_v2] mark_import_completed failed for {session_id}: {exc}")

        # [NEW] Free Plan: Auto-trigger Summary and Quiz
        # "Free 1 time = Cloud STT + Summary + Quiz"
        # Since they consumed their 1 credit to start this Cloud STT, we maximize their value.
        # [DISABLED] Summary/Quiz auto-trigger removed — user triggers manually via generate button

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
            
            # [DISABLED] Summary/Quiz auto-trigger removed — user triggers manually via generate button

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

@router.post("/internal/tasks/cleanup_sessions", dependencies=[Depends(verify_cloud_tasks_request)])
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


@router.post("/internal/tasks/nuke_user", dependencies=[Depends(verify_cloud_tasks_request)])
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

# NOTE: This is a DUPLICATE of line ~877. Consider consolidating.
@router.post("/internal/tasks/merge_migration_v2", dependencies=[Depends(verify_cloud_tasks_request)])
async def handle_merge_migration_task_v2(request: Request):
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


@router.post("/internal/tasks/backfill-audio-urls", dependencies=[Depends(verify_cloud_tasks_request)])
async def handle_backfill_audio_urls(request: Request):
    """
    Backfill signedGetUrl for sessions with uploaded audio but no cached URL.
    iOS reads signedGetUrl from Firestore directly, so this must be pre-populated.
    """
    from app.routes.sessions import signing_credentials, _get_signing_email

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    account_id = payload.get("accountId")
    limit_count = payload.get("limit", 200)

    sa_email = _get_signing_email()
    if not sa_email:
        return {"status": "failed", "error": "No signing SA configured"}

    creds = signing_credentials(sa_email)
    if not creds:
        return {"status": "failed", "error": "Failed to create signing creds"}

    now_utc = datetime.now(timezone.utc)
    expires = now_utc + timedelta(days=7)

    query = db.collection("sessions").where("audioStatus", "==", "uploaded")
    if account_id:
        query = query.where("ownerAccountId", "==", account_id)

    updated = 0
    skipped = 0
    errors_list = []

    def _do():
        nonlocal updated, skipped
        for doc_snap in query.limit(limit_count).stream():
            data = doc_snap.to_dict()
            cached_expires = data.get("signedGetUrlExpiresAt")
            if cached_expires and hasattr(cached_expires, "replace"):
                exp = cached_expires.replace(tzinfo=timezone.utc) if cached_expires.tzinfo is None else cached_expires
                if exp > now_utc + timedelta(hours=1):
                    skipped += 1
                    continue

            audio_info = data.get("audio") or {}
            gcs_path = audio_info.get("gcsPath") or data.get("audioPath")
            if not gcs_path:
                skipped += 1
                continue

            bucket_name = AUDIO_BUCKET_NAME
            prefix = f"gs://{bucket_name}/"
            if gcs_path.startswith(prefix):
                blob_name = gcs_path[len(prefix):]
            elif gcs_path.startswith("gs://"):
                parts = gcs_path.split("/", 3)
                blob_name = parts[3] if len(parts) > 3 else gcs_path
            else:
                blob_name = gcs_path

            try:
                blob = storage_client.bucket(bucket_name).blob(blob_name)
                url = blob.generate_signed_url(
                    version="v4", expiration=expires, method="GET", credentials=creds,
                )
                doc_snap.reference.update({
                    "signedGetUrl": url,
                    "signedGetUrlExpiresAt": expires,
                })
                updated += 1
            except Exception as e:
                errors_list.append({"id": doc_snap.id, "err": str(e)[:80]})

    await asyncio.to_thread(_do)
    logger.info(f"[BackfillAudioURLs] updated={updated}, skipped={skipped}, errors={len(errors_list)}")
    return {"status": "completed", "updated": updated, "skipped": skipped, "errors": errors_list[:5]}
