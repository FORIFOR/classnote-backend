"""
Cost Guard Service - The 'Triple Lock' Backend Gate (vNext)
Ensures strict monthly limits are never exceeded for billable features.
"""
import logging
from datetime import datetime, date, timezone, timedelta
from google.cloud import firestore
from app.firebase import db

logger = logging.getLogger("app.cost_guard")

# --- CONFIGURE JST ---
JST = timezone(timedelta(hours=9))

# --- PLAN LIMITS (DEFINITIVE vNext) ---
FREE_LIMITS = {
    "cloud_stt_sec": 1800.0,       # 30 mins
    "cloud_sessions_started": 10,
    "summary_generated": 3,
    "quiz_generated": 3,
    "server_session": 5,           # Max server sessions
    "sessions_created": 999999     # No creation flow limit (bound by server session slots)
}


# [FIX] Added Basic/Standard plan limits
BASIC_LIMITS = {
    "cloud_stt_sec": 7200.0,       # 120 mins (same as Premium)
    "cloud_sessions_started": 100, # No practical limit (bounded by STT duration)
    "summary_generated": 100,      # AI Summary: 100/month
    "quiz_generated": 100,         # AI Quiz: 100/month
    "server_session": 300,         # Max server sessions (soft limit with cleanup)
    "sessions_created": 100        # Monthly session creation limit
}



PREMIUM_LIMITS = {
    "cloud_stt_sec": 7200.0,       # 120 mins per session (no monthly cap)
    "llm_calls": 1000,             # Combined (Summary/Quiz/QA)
    "server_session": 300          # Soft limit with auto cleanup
}

class CostGuardService:
    def _get_month_key(self):
        """Returns YYYY-MM in JST."""
        return datetime.now(JST).strftime("%Y-%m")

    def _get_monthly_doc_ref(self, user_id: str):
        month_str = self._get_month_key()
        return db.collection("users").document(user_id).collection("monthly_usage").document(month_str)

    async def guard_can_consume(self, user_id: str, feature: str, amount: float = 1.0, transaction=None):
        """
        [TRIPLE LOCK] Transactional Guard.
        Checks if the user can consume 'amount' of 'feature' based on their plan.
        If allowed, INCREMENTS (RESERVES) usage immediately.
        
        Args:
            user_id: User UID
            feature: "server_session", "cloud_stt_sec", "cloud_sessions_started", "summary_generated", "quiz_generated", "llm_calls"
            amount: Amount to consume
            transaction: Optional existing Firestore transaction (sync object) to participate in.
            
        Returns:
            (Allowed, Meta)
            Tuple[bool, Optional[dict]]
        """
        user_ref = db.collection("users").document(user_id)
        doc_ref = self._get_monthly_doc_ref(user_id)
        month_str = self._get_month_key()
        
        # If external transaction provided, use it directly (Sync)
        if transaction:
            return self._check_and_reserve_logic(transaction, user_ref, doc_ref, user_id, feature, amount, month_str)

        # Otherwise, create a new isolated transaction
        @firestore.transactional
        def txn_wrapper(txn, u_ref, m_ref):
            return self._check_and_reserve_logic(txn, u_ref, m_ref, user_id, feature, amount, month_str)

        return txn_wrapper(db.transaction(), user_ref, doc_ref)

    async def get_usage_report(self, user_id: str) -> dict:
        """
        [vNext] Returns a dictionary matching CloudUsageReport model.
        Sums up limits and current usage for the current (JST) month.
        """
        user_ref = db.collection("users").document(user_id)
        doc_ref = self._get_monthly_doc_ref(user_id)

        u_snap = user_ref.get()
        if not u_snap.exists:
            return {}
        u_data = u_snap.to_dict()
        plan = u_data.get("plan", "free")
        # [FIX] Normalize all plan variants
        if plan in ("pro", "premium"):
            plan = "premium"
        elif plan in ("basic", "standard"):
            plan = "basic"
        else:
            plan = "free"

        m_snap = doc_ref.get()
        m_data = m_snap.to_dict() if m_snap.exists else {}

        # 1. Cloud Seconds - select limit based on plan
        if plan == "premium":
            limit_sec = PREMIUM_LIMITS["cloud_stt_sec"]
            session_limit = 999999
        elif plan == "basic":
            limit_sec = BASIC_LIMITS["cloud_stt_sec"]
            session_limit = BASIC_LIMITS["cloud_sessions_started"]
        else:
            limit_sec = FREE_LIMITS["cloud_stt_sec"]
            session_limit = FREE_LIMITS["cloud_sessions_started"]

        used_sec = float(m_data.get("cloud_stt_sec", 0.0))
        sessions_started = int(m_data.get("cloud_sessions_started", 0))

        # 2. Decision Logic
        can_start = True
        reason = None

        # Apply limits for non-premium plans
        if plan != "premium":
            if used_sec >= limit_sec:
                can_start = False
                reason = "cloud_minutes_limit"
            if sessions_started >= session_limit:
                can_start = False
                reason = "cloud_session_limit"
        
        return {
            "plan": plan,
            "limitSeconds": limit_sec,
            "usedSeconds": used_sec,
            "remainingSeconds": max(0.0, limit_sec - used_sec),
            "sessionLimit": session_limit,
            "sessionsStarted": sessions_started,
            "canStart": can_start,
            "reasonIfBlocked": reason
        }

    def _check_and_reserve_logic(self, transaction, u_ref, m_ref, user_id, feature, amount, month_str, u_snap=None, m_snap=None):
        # 1. Get Plan (with normalization)
        if not u_snap:
            u_snap = u_ref.get(transaction=transaction)
        
        if not u_snap.exists:
            return False, {"error": "user_not_found"}

        u_data = u_snap.to_dict()
        plan = u_data.get("plan", "free")
        # [FIX] Normalize all plan variants
        if plan in ("pro", "premium"):
            plan = "premium"
        elif plan in ("basic", "standard"):
            plan = "basic"
        else:
            plan = "free"

        # 2. Get Usage
        if not m_snap:
            m_snap = m_ref.get(transaction=transaction)
        m_data = m_snap.to_dict() if m_snap.exists else {}

        # 3. Apply Limit Logic
        limit = None
        current = 0

        # Special: server_session (on User doc, not monthly)
        if feature == "server_session":
            if plan == "premium":
                limit = PREMIUM_LIMITS["server_session"]
            elif plan == "basic":
                limit = BASIC_LIMITS["server_session"]
            else:
                limit = FREE_LIMITS["server_session"]
            current = int(u_data.get("serverSessionCount", 0))

            if current + amount > limit:
                # For Premium, allow but trigger cleanup outside (soft limit)
                if plan == "premium":
                    pass
                else:
                    logger.warning(f"[CostGuard] BLOCKED {user_id} ({plan}) server_session: {current}+{amount} > {limit}")
                    return False, {
                        "limit": limit,
                        "used": current,
                        "plan": plan,
                        "rule": "server_session_limit",
                        "uid": user_id
                    }
            
            transaction.update(u_ref, {"serverSessionCount": firestore.Increment(amount)})
            return True, None


        # Select limits based on plan
        if plan == "premium":
            if feature == "cloud_stt_sec":
                limit = PREMIUM_LIMITS["cloud_stt_sec"]
                current = float(m_data.get("cloud_stt_sec", 0.0))
            elif feature in ["summary_generated", "quiz_generated", "llm_calls"]:
                limit = PREMIUM_LIMITS["llm_calls"]
                current = int(m_data.get("llm_calls", 0))
            elif feature == "cloud_sessions_started":
                limit = 999999  # No session count limit for premium
                current = int(m_data.get("cloud_sessions_started", 0))
            elif feature == "sessions_created":
                limit = 999999  # No monthly session creation limit for premium
                current = int(m_data.get("sessions_created", 0))

        elif plan == "basic":
            # [FIX] Added Basic plan handling
            if feature == "cloud_stt_sec":
                limit = BASIC_LIMITS["cloud_stt_sec"]
                current = float(m_data.get("cloud_stt_sec", 0.0))
            elif feature == "cloud_sessions_started":
                limit = BASIC_LIMITS["cloud_sessions_started"]
                current = int(m_data.get("cloud_sessions_started", 0))
            elif feature == "summary_generated":
                limit = BASIC_LIMITS["summary_generated"]
                current = int(m_data.get("summary_generated", 0))
            elif feature == "quiz_generated":
                limit = BASIC_LIMITS["quiz_generated"]
                current = int(m_data.get("quiz_generated", 0))
            elif feature == "llm_calls":
                # For Basic, also combine LLM calls
                limit = BASIC_LIMITS["summary_generated"] + BASIC_LIMITS["quiz_generated"]
                current = int(m_data.get("llm_calls", 0))
            elif feature == "sessions_created":
                limit = BASIC_LIMITS["sessions_created"]
                current = int(m_data.get("sessions_created", 0))

        else:
            # Free Plan
            if feature == "cloud_stt_sec":
                limit = FREE_LIMITS["cloud_stt_sec"]
                current = float(m_data.get("cloud_stt_sec", 0.0))
            elif feature == "cloud_sessions_started":
                limit = FREE_LIMITS["cloud_sessions_started"]
                current = int(m_data.get("cloud_sessions_started", 0))
            elif feature == "summary_generated":
                limit = FREE_LIMITS["summary_generated"]
                current = int(m_data.get("summary_generated", 0))
            elif feature == "quiz_generated":
                limit = FREE_LIMITS["quiz_generated"]
                current = int(m_data.get("quiz_generated", 0))
            elif feature == "llm_calls":
                # For Free, fail closed to force specific feature check
                return False, {"error": "feature_not_supported_in_free", "plan": "free"}
            elif feature == "sessions_created":
                limit = FREE_LIMITS["sessions_created"]
                current = int(m_data.get("sessions_created", 0))



        if limit is None:
            logger.error(f"[CostGuard] No limit defined for {plan}/{feature}")
            return False, {"error": "no_limit_defined", "plan": plan, "feature": feature}


        if current + amount > limit:
            logger.warning(f"[CostGuard] BLOCKED {user_id} ({plan}) {feature}: {current}+{amount} > {limit}")
            return False, {
                "limit": limit,
                "used": current,
                "plan": plan,
                "rule": f"{feature}_limit",
                "uid": user_id,
                "monthKey": month_str
            }


        # 4. Reserve Quota
        updates = {"updated_at": datetime.now(timezone.utc)}
        
        # Mapping for update
        target_field = feature
        if plan == "premium" and feature in ["summary_generated", "quiz_generated"]:
            target_field = "llm_calls"
        
        updates[target_field] = firestore.Increment(amount)

        # Execute Update
        if not m_snap.exists:
            transaction.set(m_ref, updates, merge=True)
        else:
            transaction.update(m_ref, updates)
        
        return True, None

# Singleton
cost_guard = CostGuardService()
