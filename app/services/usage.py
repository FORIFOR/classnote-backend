"""
Usage Logger Service - Tracks all API usage for analytics and billing
"""
import time
import logging
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, Literal


from google.cloud import firestore

from app.firebase import db
from app.usage_models import UsageEvent, UsageEventPayload

logger = logging.getLogger("app.usage")


class UsageLogger:
    """
    Central usage logging service.
    
    Usage:
        await usage_logger.log(
            user_id="uid_xxx",
            session_id="session_abc",
            feature="summary",
            event_type="success",
            duration_ms=1234,
            payload={"input_tokens": 500, "output_tokens": 200}
        )
    """
    
    EVENTS_COLLECTION = "usage_events"
    DAILY_USAGE_COLLECTION = "user_daily_usage"
    
    @staticmethod
    def _today_str() -> str:
        return date.today().isoformat()
    
    @staticmethod
    def _daily_doc_id(user_id: str, date_str: str) -> str:
        return f"{user_id}_{date_str}"
    
    async def log(
        self,
        user_id: str,
        feature: Literal[
            "recording", "summary", "quiz", "highlights",
            "playlist", "diarization", "qa", "share", "export"
        ],
        event_type: Literal["invoke", "success", "error", "cancel"],
        session_id: Optional[str] = None,
        duration_ms: Optional[int] = None,
        payload: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Log a usage event and update daily aggregates.
        
        This method is fire-and-forget safe - it catches exceptions
        to avoid breaking the main API flow.
        """
        try:
            await self._log_event(
                user_id=user_id,
                session_id=session_id,
                feature=feature,
                event_type=event_type,
                duration_ms=duration_ms,
                payload=payload
            )
            await self._update_daily_aggregate(
                user_id=user_id,
                feature=feature,
                event_type=event_type,
                payload=payload
            )
        except Exception as e:
            logger.exception(f"[UsageLogger] Failed to log usage: {e}")
    
    async def _log_event(
        self,
        user_id: str,
        feature: str,
        event_type: str,
        session_id: Optional[str],
        duration_ms: Optional[int],
        payload: Optional[Dict[str, Any]]
    ) -> None:
        """Write raw event to usage_events collection"""
        event_data = {
            "user_id": user_id,
            "session_id": session_id,
            "feature": feature,
            "event_type": event_type,
            "timestamp": datetime.utcnow(),
            "duration_ms": duration_ms,
            "payload": payload or {}
        }
        db.collection(self.EVENTS_COLLECTION).add(event_data)
        logger.info(f"[UsageLogger] Event logged: {user_id}/{feature}/{event_type}")
    
    async def _update_daily_aggregate(
        self,
        user_id: str,
        feature: str,
        event_type: str,
        payload: Optional[Dict[str, Any]]
    ) -> None:
        """Increment daily usage counters atomically"""
        date_str = self._today_str()
        doc_id = self._daily_doc_id(user_id, date_str)
        doc_ref = db.collection(self.DAILY_USAGE_COLLECTION).document(doc_id)
        
        # Build increments based on feature and event type
        increments = {}
        
        # Feature-specific counters
        if feature == "summary":
            increments["summary_invocations"] = firestore.Increment(1)
            if event_type == "success":
                increments["summary_success"] = firestore.Increment(1)
            elif event_type == "error":
                increments["summary_error"] = firestore.Increment(1)
                
        elif feature == "quiz":
            increments["quiz_invocations"] = firestore.Increment(1)
            if event_type == "success":
                increments["quiz_success"] = firestore.Increment(1)
            elif event_type == "error":
                increments["quiz_error"] = firestore.Increment(1)
                
        elif feature == "diarization":
            increments["diarization_invocations"] = firestore.Increment(1)
            if event_type == "success":
                increments["diarization_success"] = firestore.Increment(1)
                
        elif feature == "qa":
            increments["qa_invocations"] = firestore.Increment(1)
            if event_type == "success":
                increments["qa_success"] = firestore.Increment(1)
                
        elif feature == "recording":
            increments["session_count"] = firestore.Increment(1)
            if payload and payload.get("recording_sec"):
                increments["total_recording_sec"] = firestore.Increment(
                    float(payload.get("recording_sec", 0))
                )
                
        elif feature == "share":
            increments["share_count"] = firestore.Increment(1)
            
        elif feature == "export":
            increments["export_count"] = firestore.Increment(1)
        
        # LLM token tracking (any feature)
        if payload:
            if payload.get("input_tokens"):
                increments["llm_input_tokens"] = firestore.Increment(
                    int(payload.get("input_tokens", 0))
                )
            if payload.get("output_tokens"):
                increments["llm_output_tokens"] = firestore.Increment(
                    int(payload.get("output_tokens", 0))
                )
        
        # Transcribe (Recording) tracking
        if feature == "transcribe" and payload:
            rec_sec = float(payload.get("recording_sec", 0))
            rec_type = payload.get("type", "cloud") # "cloud" or "on_device"
            
            if rec_type == "cloud":
                increments["total_recording_cloud_sec"] = firestore.Increment(rec_sec)
            elif rec_type == "on_device":
                increments["total_recording_ondevice_sec"] = firestore.Increment(rec_sec)
        
        # Mode & Tag tracking (for recording events)
        if feature == "recording" and payload:
            rec_sec = float(payload.get("recording_sec", 0))
            rec_type = payload.get("type")
            
            if rec_sec > 0:
                # Mode
                mode = payload.get("mode")
                if mode:
                    increments[f"usage_by_mode.{mode}"] = firestore.Increment(rec_sec)
                
                # Tags
                tags = payload.get("tags") or []
                for tag in tags:
                    # Sanitize tag for field name (replace . with _)
                    safe_tag = tag.replace(".", "_")
                    increments[f"usage_by_tag.{safe_tag}"] = firestore.Increment(rec_sec)
        
        if increments:
            # Ensure base fields exist
            increments["user_id"] = user_id
            increments["date"] = date_str
            
            doc_ref.set(increments, merge=True)
            logger.debug(f"[UsageLogger] Daily usage updated: {doc_id}")

        if event_type == "success":
             pass # No flag tracking needed, we consume at start.

    async def consume_free_cloud_credit(self, user_id: str) -> bool:
        """
        Atomically decrement 'freeCloudCreditsRemaining' for Free plan users.
        Returns True if allowed (decremented or not applicable).
        Returns False if credit exhausted.
        """
        user_ref = db.collection("users").document(user_id)
        
        @firestore.transactional
        def txn_consume(transaction, ref):
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                # Implicit create? Or just fail? 
                # Better to assume user exists if we are here (auth passed)
                # But if doc missing, default to 1 -> 0
                transaction.set(ref, {"freeCloudCreditsRemaining": 0}, merge=True)
                return True
                
            data = snapshot.to_dict()
            plan = data.get("plan", "free")
            
            if plan != "free":
                return True # Not limited
                
            # Default to 1 if not present
            credits = data.get("freeCloudCreditsRemaining")
            if credits is None:
                credits = 1
                
            if credits > 0:
                transaction.update(ref, {"freeCloudCreditsRemaining": credits - 1})
                return True
            else:
                return False

        transaction = db.transaction()
        try:
            return txn_consume(transaction, user_ref)
        except Exception as e:
            logger.error(f"Credit consumption failed: {e}")
            return False # Fail closed on error

    async def check_rate_limit(self, user_id: str, key: str, limit: int, window_sec: int = 60) -> bool:
        """
        Check if a user has exceeded a rate limit for a specific key.
        Uses a 1-minute bucket (default) in Firestore.
        Returns True if ALLOWED, False if LIMITED.
        """
        # Create a bucket ID based on current time window
        bucket_ts = int(time.time() / window_sec)
        bucket_id = f"{user_id}_{key}_{bucket_ts}"
        doc_ref = db.collection("usage_limits").document(bucket_id)
        
        try:
            # Atomic increment
            # We don't use transactional read-then-write here to keep it fast.
            # Increment and then check the result (Firestore returns the new value).
            # Wait, doc_ref.set with Increment doesn't return the value directly in the same call 
            # without a transaction or a separate get. 
            # To be strictly atomic and efficient, we use a transaction for exact threshold check.
            
            @firestore.transactional
            def txn_check(transaction, ref):
                snapshot = ref.get(transaction=transaction)
                current = snapshot.get("count") if snapshot.exists else 0
                if current >= limit:
                    return False
                
                if snapshot.exists:
                    transaction.update(ref, {"count": firestore.Increment(1)})
                else:
                    transaction.set(ref, {
                        "count": 1, 
                        "user_id": user_id, 
                        "key": key, 
                        "expiresAt": datetime.utcnow() + timedelta(seconds=window_sec * 2)
                    })
                return True

            transaction = db.transaction()
            return txn_check(transaction, doc_ref)
        except Exception as e:
            logger.error(f"Rate limit check failed: {e}")
            return True # Fail open

    async def check_security_state(self, user_id: str, required_states: list = ["normal"]) -> bool:
        """
        Verify if the user's security state allows the operation.
        Returns False if BLOCKED/RESTRICTED.
        """
        try:
            doc = db.collection("users").document(user_id).get(["securityState", "plan"])
            if not doc.exists: return True
            
            data = doc.to_dict()
            state = data.get("securityState", "normal")
            
            if state == "blocked":
                return False
            
            if state == "restricted" and "restricted" not in required_states:
                return False
                
            return True
        except Exception as e:
            logger.error(f"Security state check failed: {e}")
            return True # Fail open

    async def track_security_event(self, user_id: str, risk_delta: int, reason: str) -> None:
        """
        Increment risk score and update security state if threshold exceeded.
        """
        user_ref = db.collection("users").document(user_id)
        
        try:
            @firestore.transactional
            def txn_risk(transaction, ref):
                snapshot = ref.get(transaction=transaction)
                if not snapshot.exists: return
                
                data = snapshot.to_dict()
                old_score = data.get("riskScore", 0)
                new_score = old_score + risk_delta
                
                updates = {"riskScore": new_score}
                
                # Thresholds
                if new_score >= 90:
                    updates["securityState"] = "blocked"
                elif new_score >= 60 and data.get("securityState") != "blocked":
                    updates["securityState"] = "restricted"
                
                transaction.update(ref, updates)
                
                # Log to a separate collection for audit
                audit_ref = db.collection("security_audit_logs").document()
                transaction.set(audit_ref, {
                    "user_id": user_id,
                    "delta": risk_delta,
                    "new_score": new_score,
                    "reason": reason,
                    "timestamp": datetime.utcnow()
                })

            transaction = db.transaction()
            txn_risk(transaction, user_ref)
            logger.warning(f"[Security] Risk event for {user_id}: {reason} (delta={risk_delta})")
        except Exception as e:
            logger.error(f"Failed to track security event: {e}")
    
    async def get_user_usage_summary(
        self,
        user_id: str,
        from_date: str,
        to_date: str
    ) -> Dict[str, Any]:
        """
        Get aggregated usage for a user over a date range.
        
        Args:
            user_id: The user ID
            from_date: Start date (yyyy-MM-dd)
            to_date: End date (yyyy-MM-dd)
            
        Returns:
            Aggregated usage summary
        """
        # Generate usage document IDs for the date range
        # This avoids the need for a composite index on (user_id, date) by using deterministic IDs.
        start_dt = date.fromisoformat(from_date)
        end_dt = date.fromisoformat(to_date)
        delta_days = (end_dt - start_dt).days
        
        doc_refs = []
        for i in range(delta_days + 1):
            d_str = (start_dt + timedelta(days=i)).isoformat()
            doc_id = self._daily_doc_id(user_id, d_str)
            doc_refs.append(db.collection(self.DAILY_USAGE_COLLECTION).document(doc_id))
            
        # Batch get (efficient and no index required)
        docs = db.get_all(doc_refs)
        
        # Aggregate
        totals = {
            "user_id": user_id,
            "from_date": from_date,
            "to_date": to_date,
            "total_recording_sec": 0.0,
            "session_count": 0,
            "summary_invocations": 0,
            "summary_success": 0,
            "quiz_invocations": 0,
            "quiz_success": 0,
            "diarization_invocations": 0,
            "qa_invocations": 0,
            "llm_input_tokens": 0,
            "llm_output_tokens": 0,
            "share_count": 0,
            "export_count": 0,
            "total_recording_cloud_sec": 0.0,
            "total_recording_ondevice_sec": 0.0
        }

        
        # Detailed Aggregation
        timeline_daily = []
        by_mode = {}
        by_tag = {}
        
        for doc in docs:
            if not doc.exists:
                continue
            data = doc.to_dict()
            
            # 1. Timeline
            timeline_daily.append({
                "date": data.get("date"),
                "recording_sec": data.get("total_recording_sec", 0),
                "session_count": data.get("session_count", 0)
            })
            
            # 2. Basic Totals
            for key in totals:
                if key in data and isinstance(data[key], (int, float)):
                    totals[key] += data[key]
            
            # 3. Mode Aggregation
            u_mode = data.get("usage_by_mode", {})
            for m, sec in u_mode.items():
                by_mode[m] = by_mode.get(m, 0) + sec
                
            # 4. Tag Aggregation
            u_tag = data.get("usage_by_tag", {})
            for t, sec in u_tag.items():
                by_tag[t] = by_tag.get(t, 0) + sec
        
        # Sort top tags
        top_tags = sorted(
            [{"tag": k, "recording_sec": v} for k, v in by_tag.items()],
            key=lambda x: x["recording_sec"],
            reverse=True
        )[:5]
        
        totals["timeline_daily"] = sorted(timeline_daily, key=lambda x: x["date"])
        totals["by_mode"] = by_mode
        totals["topTags"] = top_tags
        
        # Derived Total (Optional consistency check or UI helper)
        # However, total_recording_sec is already tracked independently.
        # We can leave it as is.
        
        return totals


# Singleton instance
usage_logger = UsageLogger()
