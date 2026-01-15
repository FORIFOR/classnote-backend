from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import asyncio
import struct
import uuid
import json
import logging
import time
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

from app.streaming_stt_v2 import StreamingSTTV2, compute_audio_stats
from app.firebase import db, storage_client, AUDIO_BUCKET_NAME
from app.services.usage import usage_logger

router = APIRouter()
logger = logging.getLogger("app.websocket")

# Feature flags
USE_SEQ_PROTOCOL = True  # Expect [seq(4bytes)] + pcm binary format
STT_DRAIN_TIMEOUT_SEC = 5.0


def _session_doc_ref(session_id: str):
    return db.collection("sessions").document(session_id)


def _extract_seq_and_pcm(data: bytes) -> tuple[Optional[int], bytes]:
    """
    Extract sequence number and PCM from binary message.
    Format: [seq (4 bytes, big-endian)] + [pcm_bytes]
    Returns (seq, pcm_bytes) or (None, original_data) if not using seq protocol.
    """
    if USE_SEQ_PROTOCOL and len(data) >= 4:
        try:
            seq = struct.unpack(">I", data[:4])[0]
            pcm = data[4:]
            return seq, pcm
        except Exception:
            pass
    return None, data


@router.websocket("/ws/stream/{session_id}")
async def ws_stream(websocket: WebSocket, session_id: str):
    await websocket.accept()

    doc_ref = _session_doc_ref(session_id)
    doc = doc_ref.get()

    if not doc.exists:
        logger.warning(f"[/ws/stream] Session not found: {session_id}")
        await websocket.close(code=4000, reason="session_not_found")
        return

    session_data = doc.to_dict()
    
    
    # Limit checks for Free Plan (Credit Based)
    uid = session_data.get("userId") or session_data.get("ownerUserId") or session_data.get("ownerId")
    if uid:
        # [Security] Blocked/Restricted check
        if not await usage_logger.check_security_state(uid):
             logger.warning(f"[/ws/stream] Security block for user {uid}")
             await websocket.close(code=4003, reason="security_block")
             return

        # [Security] Concurrent connection lock (Atomic)
        # Using a specialized collection to track active streams
        lock_ref = db.collection("active_streams").document(uid)
        
        @firestore.transactional
        def txn_lock(transaction, ref):
            snap = ref.get(transaction=transaction)
            if snap.exists:
                # Check for stale locks (e.g. older than 3 hours)
                last_active = snap.get("updatedAt")
                if last_active and (datetime.utcnow() - last_active.replace(tzinfo=None)).total_seconds() < 10800:
                    return False # Active connection exists
            
            transaction.set(ref, {
                "sessionId": session_id,
                "updatedAt": firestore.SERVER_TIMESTAMP
            })
            return True

        if not txn_lock(db.transaction(), lock_ref):
            logger.info(f"[/ws/stream] User {uid} already has an active stream. Rejecting concurrent connection.")
            await websocket.close(code=4003, reason="concurrent_stream_limit")
            return

        # Atomic consume (Only if this session doesn't already have an issued Cloud Ticket)
        # If a ticket exists, it means credit was already consumed at session creation.
        has_ticket = bool(session_data.get("cloudTicket"))
        if not has_ticket:
            allowed = await usage_logger.consume_free_cloud_credit(uid)
            if not allowed:
                # Release lock before closing
                lock_ref.delete()
                logger.info(f"[/ws/stream] User {uid} exhausted Free tier Cloud credits. Rejecting connection.")
                await websocket.close(code=4003, reason="free_cloud_credit_exhausted")
                return

    # Tracking for forced stop
    start_time = time.time()
    
    # 120m hard limit (default 2h if not in session_data)
    # Priority: 1. session_data[cloudAllowedUntil], 2. session_data[createdAt] + 2h, 3. 7200s fixed
    cloud_allowed_until = session_data.get("cloudAllowedUntil")
    if cloud_allowed_until:
        # datetime might be timezone-aware from Firestore
        if hasattr(cloud_allowed_until, 'timestamp'):
             MAX_STREAM_DURATION = max(0, cloud_allowed_until.timestamp() - time.time())
        else:
             MAX_STREAM_DURATION = 7200
    else:
        created_at = session_data.get("createdAt")
        if created_at and hasattr(created_at, 'timestamp'):
            MAX_STREAM_DURATION = max(0, (created_at.timestamp() + 7200) - time.time())
        else:
            MAX_STREAM_DURATION = 7200

    frame_count = 0
    audio_started_at = None
    NO_AUDIO_TIMEOUT = 20 # 20 seconds

    logger.info(f"[/ws/stream] WebSocket connected session_id={session_id}")

    tmp_dir = Path("/tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_file = tmp_dir / f"{session_id}_{uuid.uuid4().hex}.raw"

    # Backpressure Queue - Drop oldest if full to prevent latency buildup
    audio_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=50)

    stt_task = None
    stop_event = asyncio.Event()

    # Session State
    started = False
    stop_requested = False
    last_seq = -1
    audio_chunk_count = 0
    total_audio_bytes = 0
    max_audio_amplitude = 0
    last_recv_time = time.time()

    # Transcript Accumulator - CRITICAL for persistence
    final_transcripts: list[str] = []

    # Config defaults
    language_code = "ja-JP"
    sample_rate = 16000

    # STT Instance
    stt_v2 = StreamingSTTV2()

    # Generator for STT
    async def queue_generator():
        chunk_count_gen = 0
        while True:
            try:
                # Wait for audio with timeout for Heartbeat
                chunk = await asyncio.wait_for(audio_queue.get(), timeout=1.5)

                if chunk is None:
                    # End of stream
                    break

                chunk_count_gen += 1
                yield chunk

            except asyncio.TimeoutError:
                if not stop_requested and started:
                    # Heartbeat (Silence) to keep connection alive
                    logger.debug(f"[/ws/stream] Sending heartbeat silence (chunk #{chunk_count_gen})")
                    yield stt_v2.create_silence_chunk(duration_ms=100)
                continue

    async def run_stt(lang: str, rate: int):
        nonlocal final_transcripts
        logger.info(f"[/ws/stream] Starting V2 STT task (lang={lang})")

        try:
            generator = queue_generator()

            async for event in stt_v2.recognize_stream(generator, sample_rate=rate, language_code=lang):
                if stop_event.is_set():
                    break

                if "transcript" in event:
                    transcript_text = event.get("transcript", "")
                    is_final = event.get("is_final", False)

                    # Accumulate Final Transcripts for Persistence
                    if is_final and transcript_text:
                        final_transcripts.append(transcript_text)
                        logger.debug(f"[/ws/stream] Final transcript accumulated: {len(final_transcripts)} segments")

                    resp = {
                        "event": "final" if is_final else "partial",
                        "transcript": transcript_text,
                        "confidence": event.get("confidence", 0.0),
                        "seq": last_seq
                    }
                    try:
                        await websocket.send_json(resp)
                    except Exception as e:
                        logger.warning(f"[/ws/stream] Write error: {e}")
                        break

                if "vad_event" in event:
                    try:
                        await websocket.send_json({"event": "vad", "state": event["vad_event"]})
                    except Exception:
                        pass

        except Exception as e:
            logger.error(f"[/ws/stream] STT Error: {e}", exc_info=True)
            if not stop_requested:
                try:
                    await websocket.send_json({"event": "error", "message": str(e)})
                except Exception:
                    pass
        finally:
            logger.info(f"[/ws/stream] V2 STT task finished. Accumulated {len(final_transcripts)} final segments.")

    try:
        while True:
            # [Security] Forced Disconnect check (Absolute 120m limit)
            if time.time() - start_time > MAX_STREAM_DURATION:
                 logger.warning(f"[/ws/stream] Session {session_id} exceeded absolute duration limit. Forced disconnect.")
                 break
            
            # [Security] No-Audio Timeout (20s)
            # If start was received but no audio bytes arrived within 20s
            if started and total_audio_bytes == 0 and audio_started_at:
                 if time.time() - audio_started_at > NO_AUDIO_TIMEOUT:
                      logger.warning(f"[/ws/stream] Session {session_id} no-audio timeout (20s). Forced disconnect.")
                      break

            msg = await websocket.receive()

            # 1. Text Messages (JSON Control)
            if "text" in msg and msg["text"]:
                try:
                    data = json.loads(msg["text"])
                    event = data.get("event")

                    if event == "start":
                        client_config = data.get("config", {})
                        client_ticket = data.get("cloudTicket") # [NEW] Authorization Ticket
                        
                        logger.info(f"[/ws/stream] START: {json.dumps(client_config)} ticket={client_ticket}")

                        # [Security] Cloud Ticket Verification
                        # If the session is in cloud_google mode, it MUST have a valid ticket.
                        expected_mode = session_data.get("transcriptionMode", "cloud_google")
                        if expected_mode == "cloud_google":
                            expected_ticket = session_data.get("cloudTicket")
                            if not expected_ticket or client_ticket != expected_ticket:
                                logger.warning(f"[/ws/stream] Ticket mismatch: expected={expected_ticket}, got={client_ticket}")
                                await websocket.send_json({"event": "error", "message": "unauthorized_cloud_ticket"})
                                break
                            
                            # Check expiry again just in case
                            if cloud_allowed_until and time.time() > cloud_allowed_until.timestamp():
                                logger.warning(f"[/ws/stream] Ticket expired for session {session_id}")
                                await websocket.send_json({"event": "error", "message": "cloud_ticket_expired"})
                                break

                        if "languageCode" in client_config:
                            language_code = client_config["languageCode"]
                        if "sampleRateHertz" in client_config:
                            sample_rate = int(client_config["sampleRateHertz"])

                        # Start STT
                        if stt_task is None:
                            started = True
                            audio_started_at = time.time() # [NEW] Track when audio input is expected
                            stt_task = asyncio.create_task(run_stt(language_code, sample_rate))
                            await websocket.send_json({"event": "connected"})

                    elif event == "stop":
                        logger.info("[/ws/stream] STOP received")
                        stop_requested = True
                        if started:
                            # Signal generator to stop
                            await audio_queue.put(None)

                        # Wait for STT to drain
                        if stt_task:
                            try:
                                await asyncio.wait_for(stt_task, timeout=STT_DRAIN_TIMEOUT_SEC)
                            except asyncio.TimeoutError:
                                logger.warning("[/ws/stream] STT Drain timeout")
                            except Exception as e:
                                logger.error(f"[/ws/stream] STT Drain error: {e}")

                        try:
                            await websocket.send_json({"event": "done"})
                        except Exception:
                            pass
                        break

                except json.JSONDecodeError:
                    pass

            # 2. Binary Messages (Audio)
            elif "bytes" in msg and msg["bytes"]:
                raw_data = msg["bytes"]
                seq, pcm = _extract_seq_and_pcm(raw_data)

                # Seq check / Duplicate rejection
                if seq is not None:
                    if seq <= last_seq:
                        continue  # Duplicate/Old
                    last_seq = seq

                # [Security] Frame rate limit
                frame_count += 1
                if frame_count % 100 == 0:
                     if uid and not await usage_logger.check_rate_limit(uid, "ws_frames", 600): # 10 frames/sec avg
                          logger.warning(f"[/ws/stream] User {uid} exceeded frame rate limit. Disconnecting.")
                          break

                total_audio_bytes += len(pcm)
                audio_chunk_count += 1

                # Stats & Log
                if audio_chunk_count % 50 == 0:
                    stats = compute_audio_stats(pcm)
                    max_audio_amplitude = max(max_audio_amplitude, stats["max_abs"])

                # Save raw audio backup
                with tmp_file.open("ab") as f:
                    f.write(pcm)

                # Push to Queue (Backpressure)
                if started:
                    if audio_queue.full():
                        # Drop oldest to keep latency low
                        try:
                            _ = audio_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass

                    await audio_queue.put(pcm)

    except WebSocketDisconnect:
        logger.info(f"[/ws/stream] Disconnected session={session_id}")
    except Exception as e:
        logger.error(f"[/ws/stream] Unexpected: {e}", exc_info=True)
    finally:
        # Cleanup
        stop_event.set()
        if stt_task and not stt_task.done():
            stt_task.cancel()

        # [Security] Release Concurrent Lock
        if uid:
            try:
                # Atomic verification: only release if it's our session
                lock_ref = db.collection("active_streams").document(uid)
                @firestore.transactional
                def txn_release(transaction, ref):
                    snap = ref.get(transaction=transaction)
                    if snap.exists and snap.get("sessionId") == session_id:
                        transaction.delete(ref)
                
                txn_release(db.transaction(), lock_ref)
                logger.debug(f"[/ws/stream] Released concurrent lock for user {uid}")
            except Exception as e:
                logger.error(f"[/ws/stream] Failed to release lock: {e}")

        # Summary Log
        logger.info(f"[/ws/stream] Session End: bytes={total_audio_bytes}, chunks={audio_chunk_count}, max_amp={max_audio_amplitude}")

        # [NEW] Free Plan: Auto-trigger Summary and Quiz (Streaming)
        if total_audio_bytes > 0:
            uid = session_data.get("userId") or session_data.get("ownerUserId")
            if uid:
                try:
                    user_doc = db.collection("users").document(uid).get()
                    if user_doc.exists and user_doc.to_dict().get("plan", "free") == "free":
                        from app.task_queue import enqueue_summarize_task, enqueue_quiz_task
                        
                        logger.info(f"[FreePlan] Auto-triggering Summary/Quiz for {session_id} (Streaming)")
                        enqueue_summarize_task(session_id)
                        enqueue_quiz_task(session_id, count=3)
                except Exception as e:
                     logger.error(f"[FreePlan] Auto-trigger failed (Stream): {e}")

        # Log Usage for Billing
        if total_audio_bytes > 0:
            try:
                # 16kHz, 16bit (2bytes) -> 32000 bytes/sec
                rec_sec = total_audio_bytes / 32000.0
                uid = session_data.get("userId") or session_data.get("ownerUserId") or session_id
                
                # Fire and forget usage log
                asyncio.create_task(usage_logger.log(
                    user_id=uid,
                    session_id=session_id,
                    feature="transcribe",
                    event_type="success",
                    payload={
                        "recording_sec": rec_sec,
                        "type": "cloud",
                        "mode": session_data.get("mode"),
                        "tags": session_data.get("tags")
                    }
                ))
            except Exception as e:
                logger.error(f"[/ws/stream] Failed to log usage: {e}")

        # ====== CRITICAL: Persist Transcript to Firestore ======
        if final_transcripts:
            full_text = "".join(final_transcripts)
            logger.info(f"[/ws/stream] Persisting transcript ({len(full_text)} chars) for session {session_id}")
            try:
                update_data = {
                    "transcriptText": full_text,
                    "hasTranscript": True,
                    "transcriptSource": "cloud_streaming_v2",
                    "transcriptUpdatedAt": datetime.now(timezone.utc),
                    "updatedAt": datetime.now(timezone.utc),
                }
                # Only update status if not already beyond "transcribed"
                current_doc = doc_ref.get()
                current_status = current_doc.to_dict().get("status", "") if current_doc.exists else ""
                if current_status not in ["summarized", "processed", "completed"]:
                    update_data["status"] = "transcribed"

                doc_ref.update(update_data)
                logger.info(f"[/ws/stream] Successfully saved transcript to sessions/{session_id}")
            except Exception as e:
                logger.error(f"[/ws/stream] Failed to persist transcript: {e}", exc_info=True)
        else:
            logger.warning(f"[/ws/stream] No final transcripts to persist for session {session_id}")

        # Upload Backup Audio
        if total_audio_bytes > 0 and tmp_file.exists():
            try:
                bucket = storage_client.bucket(AUDIO_BUCKET_NAME)
                blob_path = f"raw_audio/{session_id}/backup_{int(time.time())}.raw"
                blob = bucket.blob(blob_path)
                blob.upload_from_filename(str(tmp_file))
                logger.info(f"[/ws/stream] Backup audio uploaded: {blob_path}")
            except Exception as up_err:
                logger.error(f"[/ws/stream] Failed backup upload: {up_err}")

        # Clean tmp
        if tmp_file.exists():
            tmp_file.unlink()

        try:
            await websocket.close()
        except Exception:
            pass
