from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
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
from google.cloud import firestore
from app.services.usage import usage_logger
from app.dependencies import ensure_can_view, _resolve_user_from_token
from app.services.session_event_bus import session_event_bus
from app.services.cost_guard import cost_guard  # [FIX] Add Cost Guard for proper limit checking

router = APIRouter()
logger = logging.getLogger("app.websocket")

# Feature flags
USE_SEQ_PROTOCOL = True  # Expect [seq(4bytes)] + pcm binary format
STT_DRAIN_TIMEOUT_SEC = 5.0


def _session_doc_ref(session_id: str):
    return db.collection("sessions").document(session_id)


def _resolve_session_ws(session_id: str):
    """
    Resolve session by server ID or clientSessionId fallback (WebSocket version).
    Returns (doc_ref, snapshot, resolved_session_id) or (None, None, None) if not found.
    """
    # 1. Direct lookup
    doc_ref = _session_doc_ref(session_id)
    snapshot = doc_ref.get()
    if snapshot.exists:
        return doc_ref, snapshot, session_id

    # 2. Fallback: Query by clientSessionId
    try:
        results = list(db.collection("sessions")
            .where("clientSessionId", "==", session_id)
            .limit(1).stream())
        if results:
            resolved_doc = results[0]
            resolved_id = resolved_doc.id
            logger.info(f"[WS] Resolved clientSessionId {session_id} -> serverId {resolved_id}")
            return _session_doc_ref(resolved_id), resolved_doc, resolved_id
    except Exception as e:
        logger.warning(f"[WS] clientSessionId fallback failed for {session_id}: {e}")

    return None, None, None


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


@router.websocket("/ws/sessions")
@router.websocket("/ws/sessions/")
async def ws_session_events(websocket: WebSocket):
    try:
        auth_header = websocket.headers.get("authorization") or ""
        masked_auth = (auth_header[:10] + "...") if len(auth_header) > 10 else "None"
        logger.info(f"[ws_sessions] Connection attempt. Auth: {masked_auth}")
        
        await websocket.accept()
        logger.info("[ws_sessions] Accepted connection")
    except Exception as e:
        logger.error(f"[ws_sessions] Failed to accept or unexpected error pre-handshake: {e}", exc_info=True)
        # Try to close if possible, though it might be dead
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
        return

    # Post-accept logic
    auth_header = websocket.headers.get("authorization") or ""
    token = auth_header
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    
    # [FIX] Support Query Param "token" for clients that can't set headers
    if not token:
        token = websocket.query_params.get("token")

    if not token:
        await websocket.send_json({"type": "error", "code": "unauthorized"})
        await websocket.close(code=4401, reason="unauthorized")
        return

    try:
        user = _resolve_user_from_token(token)
    except HTTPException:
        await websocket.send_json({"type": "error", "code": "unauthorized"})
        await websocket.close(code=4401, reason="unauthorized")
        return

    conn_id = await session_event_bus.register(websocket, user.uid)
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            
            if "text" in message:
                raw = message["text"]
            elif "bytes" in message:
                # Ignore binary frames (likely keepalives or errors from client)
                continue
            else:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "code": "invalid_json"})
                continue

            msg_type = msg.get("type")
            if msg_type == "subscribe":
                requested_id = msg.get("sessionId")
                if not requested_id:
                    await websocket.send_json({"type": "error", "code": "missing_session_id"})
                    continue
                doc_ref, snapshot, resolved_id = _resolve_session_ws(requested_id)
                if snapshot is None or not snapshot.exists:
                    await websocket.send_json({"type": "error", "code": "session_not_found"})
                    continue
                try:
                    ensure_can_view(snapshot.to_dict() or {}, user, resolved_id)
                except HTTPException:
                    await websocket.send_json({"type": "error", "code": "forbidden"})
                    await websocket.close(code=4403, reason="forbidden")
                    return
                await session_event_bus.subscribe(conn_id, resolved_id)
                await websocket.send_json({"type": "subscribed", "sessionId": resolved_id})
            elif msg_type == "unsubscribe":
                requested_id = msg.get("sessionId")
                if not requested_id:
                    await websocket.send_json({"type": "error", "code": "missing_session_id"})
                    continue
                doc_ref, snapshot, resolved_id = _resolve_session_ws(requested_id)
                if resolved_id:
                    await session_event_bus.unsubscribe(conn_id, resolved_id)
                await websocket.send_json({"type": "unsubscribed", "sessionId": requested_id})
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})
            else:
                await websocket.send_json({"type": "error", "code": "unsupported_type"})
    except WebSocketDisconnect:
        pass
    finally:
        await session_event_bus.unregister(conn_id)


@router.websocket("/ws/stream/{session_id}")
async def ws_stream(websocket: WebSocket, session_id: str):
    try:
        # [Debug] Log connection attempt details
        auth_header = websocket.headers.get("authorization") or ""
        query_token = websocket.query_params.get("token")
        
        masked_auth = (auth_header[:10] + "...") if len(auth_header) > 10 else "None"
        masked_query = (query_token[:10] + "...") if query_token and len(query_token) > 10 else "None"
        
        logger.info(f"[/ws/stream] Connection attempt for {session_id}. AuthHeader: {masked_auth}, QueryToken: {masked_query}")
        await websocket.accept()
        logger.info(f"[/ws/stream] Accepted connection for {session_id}")
    except Exception as e:
        logger.error(f"[/ws/stream] Failed to accept or unexpected error pre-handshake: {e}", exc_info=True)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
        return
    logger.info(f"[/ws/stream] WebSocket connected session_id={session_id}")

    # Session State Data (Defined early for scope)
    session_data = {}
    uid = None
    start_time = time.time()
    limit_sec = 0.0
    used_sec = 0.0
    remaining_sec = 0.0
    plan = "free"
    NO_AUDIO_TIMEOUT = 20.0
    audio_started_at = None
    frame_count = 0

    # --- Connection Phase (Authentication & Validation) ---
    try:
        # 1. Unified Authentication
        auth_header = websocket.headers.get("authorization") or ""
        token = None
        if auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ", 1)[1].strip()
        
        if not token:
            token = websocket.query_params.get("token")
        
        if not token:
            logger.warning(f"[/ws/stream] Missing authentication for session {session_id}")
            await websocket.send_json({"event": "error", "code": "unauthorized", "reason": "missing_token"})
            await websocket.close(code=1008, reason="unauthorized")
            return

        try:
            user = _resolve_user_from_token(token)
            uid = user.uid
        except Exception as auth_err:
            logger.warning(f"[/ws/stream] Invalid token for session {session_id}: {auth_err}")
            await websocket.send_json({"event": "error", "code": "unauthorized", "reason": "invalid_token"})
            await websocket.close(code=1008, reason="unauthorized")
            return

        # 2. Session Resolution
        doc_ref, doc, session_id = _resolve_session_ws(session_id)

        if doc is None or not doc.exists:
            logger.warning(f"[/ws/stream] Session not found: {session_id}")
            await websocket.send_json({"event": "error", "code": "session_not_found"})
            await websocket.close(code=1008, reason="session_not_found")
            return

        session_data = doc.to_dict()
        
        # 3. Authorization Check
        try:
            ensure_can_view(session_data, user, session_id)
        except Exception as perm_err:
            logger.warning(f"[/ws/stream] Forbidden access for user {uid} to session {session_id}")
            await websocket.send_json({"event": "error", "code": "forbidden"})
            await websocket.close(code=1008, reason="forbidden")
            return
        
        # Limit checks for Free Plan (Credit Based)
        uid = session_data.get("userId") or session_data.get("ownerUserId") or session_data.get("ownerId")
        if uid:
            # [FIX] Fetch accountId for unified quota management
            # PHASE 1: Always use mode="user" since accounts/{accountId} doesn't have plan field yet
            # PHASE 2 (future): After migrating plan to accounts collection, switch to mode="account"
            user_doc = db.collection("users").document(uid).get()
            user_data = user_doc.to_dict() if user_doc.exists else {}
            account_id = user_data.get("accountId")
            # For now, always use uid with mode="user" until accounts collection has plan data
            quota_id = uid
            quota_mode = "user"
            logger.info(f"[/ws/stream] Quota lookup: uid={uid}, accountId={account_id}, mode={quota_mode}")

            # [Security] Blocked/Restricted check
            if not await usage_logger.check_security_state(uid):
                 logger.warning(f"[/ws/stream] Security block for user {uid}")
                 await websocket.send_json({"event": "error", "code": "security_block"})
                 await websocket.close(code=1008, reason="security_block")
                 return

            # [Security] Concurrent connection lock (Atomic)
            lock_ref = db.collection("active_streams").document(uid)
            
            # [FIX] Reduced timeout from 3 hours (10800s) to 5 minutes (300s)
            # to prevent users being locked out after crashes
            CONCURRENT_LOCK_TIMEOUT_SEC = 300

            @firestore.transactional
            def txn_lock(transaction, ref):
                snap = ref.get(transaction=transaction)
                if snap.exists:
                    last_active = snap.get("updatedAt")
                    if last_active and (datetime.now(timezone.utc) - last_active.replace(tzinfo=timezone.utc)).total_seconds() < CONCURRENT_LOCK_TIMEOUT_SEC:
                        return False # Active connection exists

                transaction.set(ref, {
                    "sessionId": session_id,
                    "updatedAt": firestore.SERVER_TIMESTAMP
                })
                return True

            if not txn_lock(db.transaction(), lock_ref):
                logger.info(f"[/ws/stream] User {uid} already has an active stream. Rejecting concurrent connection.")
                await websocket.send_json({"event": "error", "code": "concurrent_stream_limit"})
                await websocket.close(code=1008, reason="concurrent_stream_limit")
                return

            # [FIX] Use Atomic Transaction for ticket issuance to prevent double-counting
            @firestore.transactional
            def txn_issue_ticket(transaction, s_ref, u_uid, q_id, q_mode):
                s_snap = s_ref.get(transaction=transaction)
                s_data = s_snap.to_dict() or {}

                # 1. Already has ticket?
                if s_data.get("cloudTicket"):
                    return True, s_data.get("cloudTicket")

                # 2. Check and increment usage (passing transaction)
                # Note: cost_guard.guard_can_consume (transactional)
                # However, guard_can_consume is async and transactional decorator is sync.
                # We should use _check_and_reserve_logic directly if we are inside a sync txn.

                # [FIX] Use quota_id and quota_mode for unified account-based quota
                m_ref = cost_guard._get_monthly_doc_ref(q_id, mode=q_mode)
                # Entity ref for plan lookup. 
                # If mode is account, we must check for plan in BOTH account and user docs?
                # Actually cost_guard._check_and_reserve_logic handles this if we pass u_ref correctly.
                # Currently it expects u_ref to point to the entity that HAS the 'plan' field.
                
                # Check where plan is stored
                plan_entity_ref = db.collection("accounts").document(q_id) if q_mode == "account" else db.collection("users").document(u_uid)
                
                allowed, meta = cost_guard._check_and_reserve_logic(
                    transaction, plan_entity_ref, m_ref, q_id, "cloud_sessions_started", 1, cost_guard._get_month_key()
                )

                if not allowed:
                    return False, "cloud_session_limit_exceeded"

                # 3. Issue and persist ticket
                new_ticket = str(uuid.uuid4())
                transaction.update(s_ref, {
                    "cloudTicket": new_ticket,
                    "cloudTicketIssuedAt": firestore.SERVER_TIMESTAMP,
                    "transcriptionMode": "cloud_google" # Auto-upgrade mode
                })
                return True, new_ticket

            # Execute ticket issuance
            success, result_or_ticket = txn_issue_ticket(db.transaction(), doc_ref, uid, quota_id, quota_mode)
            if not success:
                lock_ref.delete()
                logger.info(f"[/ws/stream] User {uid} rejected: {result_or_ticket}")
                await websocket.send_json({"event": "error", "code": result_or_ticket})
                await websocket.close(code=1008, reason=result_or_ticket)
                return
            
            # Refresh session_data with the new ticket/mode
            if not session_data.get("cloudTicket"):
                session_data["cloudTicket"] = result_or_ticket
                session_data["transcriptionMode"] = "cloud_google"

            # [FIX] Check remaining cloud_stt_sec quota and enforce monthly limits
            # Use quota_id and quota_mode for unified account-based quota lookup
            usage_report = await cost_guard.get_usage_report(quota_id, mode=quota_mode)

            # [SAFETY] Handle missing/empty usage data as internal error, not quota limit
            if not usage_report:
                lock_ref.delete()
                reason = "usage_data_missing"
                logger.error(f"[/ws/stream] Usage data missing for {quota_mode}={quota_id}. This is a data sync issue, not a quota limit.")
                await websocket.send_json({"event": "error", "code": reason})
                await websocket.close(code=1011, reason=reason)  # 1011 = internal error
                return

            limit_sec = float(usage_report.get("limitSeconds", 0.0))
            used_sec = float(usage_report.get("usedSeconds", 0.0))
            remaining_sec = float(usage_report.get("remainingSeconds", 0.0))
            can_start = usage_report.get("canStart", True)
            plan = usage_report.get("plan", "free")

            if not can_start or remaining_sec <= 0:
                lock_ref.delete()
                reason = usage_report.get("reasonIfBlocked", "cloud_minutes_limit")
                logger.info(f"[/ws/stream] User {uid} has no remaining cloud STT quota. Rejecting. reason={reason}")
                await websocket.send_json({"event": "error", "code": reason})
                await websocket.close(code=1008, reason=reason)
                return
            logger.info(f"[/ws/stream] User {uid} (quota_id={quota_id}) remaining quota: {remaining_sec:.0f}s (limit={limit_sec:.0f}s, used={used_sec:.0f}s, plan={plan})")

        logger.info(f"[/ws/stream] Setup complete for session_id={session_id}, user_id={uid}, plan={plan}")
    except Exception as e:
        logger.error(f"[/ws/stream] Initial setup failed: {e}", exc_info=True)
        try:
            await websocket.send_json({"event": "error", "code": "internal_setup_error", "message": str(e)})
        except Exception:
            pass
        await websocket.close(code=1011, reason="internal_setup_error")
        return

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
    segment_index = 0
    last_seq = -1
    audio_chunk_count = 0
    total_audio_bytes = 0
    max_audio_amplitude = 0
    last_recv_time = time.time()
    consumed_quota_sec = 0.0 # [NEW] Track real-time consumption
    quota_warning_sent = False


    # Transcript Accumulator - Segment-based for proper persistence
    # Each segment: {"id": str, "text": str, "startMs": int, "endMs": int, "isFinal": bool, "segmentIndex": int}
    transcript_segments: list[dict] = []
    current_partial: str = ""  # Latest partial for draft saving
    segment_counter = 0
    last_final_end_ms = 0  # Track end time for next segment

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
        nonlocal transcript_segments, current_partial, segment_counter, last_final_end_ms
        logger.info(f"[/ws/stream] Starting V2 STT task (lang={lang})")

        try:
            generator = queue_generator()

            async for event in stt_v2.recognize_stream(generator, sample_rate=rate, language_code=lang):
                if stop_event.is_set():
                    break

                if "transcript" in event:
                    transcript_text = event.get("transcript", "")
                    is_final = event.get("is_final", False)
                    
                    # Estimate timing based on audio bytes received
                    current_time_ms = int((total_audio_bytes / 32.0))  # 16kHz * 2bytes = 32000 bytes/sec

                    if is_final and transcript_text:
                        # Save as confirmed segment
                        segment_counter += 1
                        segment = {
                            "id": f"seg_{segment_counter:04d}",
                            "text": transcript_text,
                            "startMs": last_final_end_ms,
                            "endMs": current_time_ms,
                            "isFinal": True,
                            "segmentIndex": segment_index,
                            "seq": last_seq
                        }
                        transcript_segments.append(segment)
                        last_final_end_ms = current_time_ms
                        current_partial = ""  # Clear partial after final
                        logger.debug(f"[/ws/stream] Final segment #{segment_counter}: {len(transcript_text)} chars (segmentIndex={segment_index})")
                    else:
                        # Update current partial (for draft)
                        current_partial = transcript_text

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
            logger.info(f"[/ws/stream] V2 STT task finished. Accumulated {len(transcript_segments)} final segments.")

    try:
        while True:
            # [Security] No-Audio Timeout (20s) - Only if not started or inactivity
            if total_audio_bytes == 0 and audio_started_at:
                 if time.time() - audio_started_at > NO_AUDIO_TIMEOUT:
                      logger.warning(f"[/ws/stream] Session {session_id} no-audio timeout (20s). Forced disconnect.")
                      await websocket.send_json({"event": "error", "code": "no_audio_timeout"})
                      break

            msg = await websocket.receive()

            # 1. Text Messages (JSON Control)
            if "text" in msg and msg["text"]:
                try:
                    data = json.loads(msg["text"])
                    event = data.get("event")

                    if event == "start":
                        if started:
                            logger.warning("[/ws/stream] Multiplexed START received - ignoring.")
                            continue

                        client_config = data.get("config", {})
                        client_ticket = data.get("cloudTicket") # [NEW] Authorization Ticket
                        try:
                            segment_index = int(data.get("segmentIndex", 0))
                        except (TypeError, ValueError):
                            segment_index = 0
                        
                        masked_ticket = (client_ticket[:8] + "...") if client_ticket and len(client_ticket) > 8 else "None"
                        logger.info(f"[/ws/stream] START: {json.dumps(client_config)} ticket={masked_ticket} segmentIndex={segment_index}")

                        # [Security] Cloud Ticket Verification
                        # If the session is in cloud_google mode, it MUST have a valid ticket.
                        expected_mode = session_data.get("transcriptionMode", "cloud_google")
                        if expected_mode == "cloud_google":
                            expected_ticket = session_data.get("cloudTicket")
                            if not expected_ticket or client_ticket != expected_ticket:
                                logger.warning(f"[/ws/stream] Ticket mismatch: expected={expected_ticket}, got={client_ticket}")
                                await websocket.send_json({"event": "error", "code": "unauthorized_cloud_ticket"})
                                await websocket.close(code=1008, reason="unauthorized_cloud_ticket")
                                return
                            
                            # [NOTE] cloud_allowed_until was undefined here causing NameError crashes.
                            # Ticket verification above is sufficient for security.
                            pass

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
                    logger.warning(f"[/ws/stream] Invalid JSON received: {msg.get('text')}")
                    await websocket.send_json({"event": "error", "code": "invalid_json"})

            # 2. Binary Messages (Audio)
            elif "bytes" in msg and msg["bytes"]:
                if not started:
                    logger.warning("[/ws/stream] Received audio before START handshake. Rejection expected.")
                    await websocket.send_json({"event": "error", "code": "protocol_violation", "reason": "audio_before_start"})
                    await websocket.close(code=1002) # Protocol Error
                    return

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

                # [NEW] Real-time Quota Check
                if started:
                    # Calculate consumed seconds (16kHz, 16bit = 32000 bytes/sec)
                    consumed_quota_sec += len(pcm) / 32000.0
                    
                    remaining_now = max(0.0, remaining_sec - consumed_quota_sec)
                    if remaining_sec > 0 and not quota_warning_sent and remaining_now <= 300.0:
                        try:
                            await websocket.send_json({
                                "event": "quota_warning",
                                "remainingSeconds": remaining_now,
                                "limitSeconds": limit_sec,
                                "usedSeconds": used_sec + consumed_quota_sec,
                                "plan": plan,
                                "thresholdSeconds": 300
                            })
                            quota_warning_sent = True
                        except Exception:
                            pass

                    if remaining_sec > 0 and consumed_quota_sec >= remaining_sec:
                        logger.warning(f"[/ws/stream] Quota exhausted during stream. Consumed: {consumed_quota_sec:.2f}s, Limit: {remaining_sec:.2f}s")
                        
                        # Calculate Next Month 1st
                        from datetime import timedelta
                        JST = timezone(timedelta(hours=9))
                        now_jst = datetime.now(JST)
                        if now_jst.month == 12:
                            next_month = now_jst.replace(year=now_jst.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
                        else:
                            next_month = now_jst.replace(month=now_jst.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
                        
                        # 1. Send Event
                        try:
                            await websocket.send_json({
                                "event": "quota_exhausted",
                                "lockedUntil": next_month.isoformat(),
                                "consumedSeconds": consumed_quota_sec,
                                "remainingSeconds": 0.0,
                                "limitSeconds": limit_sec,
                                "usedSeconds": used_sec + consumed_quota_sec,
                                "plan": plan
                            })
                            # Small delay to ensure client receives the message
                            await asyncio.sleep(0.5)
                        except Exception:
                            pass
                            
                        # 2. Force Disconnect (Stop STT)
                        stop_requested = True
                        if started:
                            await audio_queue.put(None) # Signal generator to stop
                        break

                    # Push to Queue (Backpressure)
                    if audio_queue.full():
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
        logger.info(f"[/ws/stream] Closing WebSocket for session {session_id}. started={started}, stop_requested={stop_requested}")
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
                        enqueue_summarize_task(session_id, user_id=uid)
                        enqueue_quiz_task(session_id, count=3, user_id=uid)
                except Exception as e:
                     logger.error(f"[FreePlan] Auto-trigger failed (Stream): {e}")

        # Log Usage for Billing
        if total_audio_bytes > 0:
            try:
                # 16kHz, 16bit (2bytes) -> 32000 bytes/sec
                rec_sec = total_audio_bytes / 32000.0
                uid = session_data.get("userId") or session_data.get("ownerUserId") or session_id
                target_account_id = session_data.get("ownerAccountId") or uid
                
                # Fire and forget usage log
                asyncio.create_task(usage_logger.log(
                    user_id=target_account_id, # Log to account for quota enforcement
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
        try:
            current_doc = doc_ref.get()
            current_data = current_doc.to_dict() if current_doc.exists else {}
        except Exception:
            current_doc = None
            current_data = {}

        existing_segments = current_data.get("transcriptSegments", []) or []
        if transcript_segments:
            existing_segments = [
                seg for seg in existing_segments
                if seg.get("segmentIndex", 0) != segment_index
            ]
            merged_segments = existing_segments + transcript_segments
        else:
            merged_segments = existing_segments

        merged_segments.sort(key=lambda seg: (seg.get("segmentIndex", 0), seg.get("startMs", 0)))

        full_text = "".join(seg.get("text", "") for seg in merged_segments)
        draft_text = full_text + current_partial if current_partial else full_text
        total_chars = len(full_text)

        logger.info(
            f"[/ws/stream] Persisting transcript: segments={len(merged_segments)}, chars={total_chars}, "
            f"draft={len(draft_text)} chars, segmentIndex={segment_index}"
        )

        if merged_segments or current_partial:
            try:
                update_data = {
                    "transcriptText": full_text,  # Confirmed finals only
                    "transcriptDraft": draft_text if current_partial else None,  # Include trailing partial
                    "transcriptSegments": merged_segments,
                    "transcriptSegmentCount": len(merged_segments),
                    "hasTranscript": total_chars > 0,
                    "transcriptSource": "cloud_streaming_v2",
                    "transcriptUpdatedAt": datetime.now(timezone.utc),
                    "updatedAt": datetime.now(timezone.utc),
                }
                # Only update status if not already beyond "transcribed"
                current_status = current_data.get("status", "") if current_data else ""
                if current_status not in ["summarized", "processed", "completed"]:
                    update_data["status"] = "transcribed"

                doc_ref.update(update_data)
                logger.info(f"[/ws/stream] Successfully saved transcript to sessions/{session_id}")
            except Exception as e:
                logger.error(f"[/ws/stream] Failed to persist transcript: {e}", exc_info=True)
        else:
            logger.warning(f"[/ws/stream] No transcripts to persist for session {session_id}")

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
