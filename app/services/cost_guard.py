"""
Cost Guard Service - The 'Triple Lock' Backend Gate (vNext)
Ensures strict monthly limits are never exceeded for billable features.
"""
import logging
from datetime import datetime, timezone, timedelta
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

def _safe_dict(snap) -> dict:
    """Safely get dict from DocumentSnapshot, handling None and non-existent docs."""
    if not snap or not snap.exists:
        return {}
    return snap.to_dict() or {}


def _normalize_plan(plan: str) -> str:
    """Normalize plan name to canonical form."""
    if plan in ("pro", "premium"):
        return "premium"
    elif plan in ("basic", "standard"):
        return "basic"
    return "free"


def _resolve_plan_from_data(u_data: dict, user_id: str = None) -> str:
    """
    Resolve plan from user data with fallback to account.
    Returns normalized plan name.
    """
    plan = u_data.get("plan")

    # Fallback: If plan not set, try accounts/{accountId}.plan
    if not plan:
        account_id = u_data.get("accountId")
        if account_id:
            acc_snap = db.collection("accounts").document(account_id).get()
            if acc_snap.exists:
                plan = (acc_snap.to_dict() or {}).get("plan", "free")
                if user_id:
                    logger.info(f"[CostGuard] User {user_id} plan fallback to account {account_id}: {plan}")

    return _normalize_plan(plan or "free")


def _get_plan_limits(plan: str, feature: str) -> int | float | None:
    """Get limit for a feature based on plan."""
    limits_map = {
        "premium": PREMIUM_LIMITS,
        "basic": BASIC_LIMITS,
        "free": FREE_LIMITS,
    }
    limits = limits_map.get(plan, FREE_LIMITS)
    return limits.get(feature)


class CostGuardService:
    def _get_month_key(self):
        """Returns YYYY-MM in JST."""
        return datetime.now(JST).strftime("%Y-%m")

    def _get_monthly_doc_ref(self, id_val: str, mode: str = "account"):
        """
        id_val: accountId or userId
        mode: "account" or "user"
        """
        month_str = self._get_month_key()
        collection = "accounts" if mode == "account" else "users"
        return db.collection(collection).document(id_val).collection("monthly_usage").document(month_str)

    async def guard_can_consume(self, id_val: str, feature: str, amount: float = 1.0, transaction=None, mode: str = "account"):
        """
        [TRIPLE LOCK] Transactional Guard.
        Checks if the entity (account or user) can consume 'amount' of 'feature'.
        If allowed, INCREMENTS (RESERVES) usage immediately.
        
        Args:
            id_val: accountId (or uid if mode="user")
            feature: Feature key
            amount: Amount to consume
            transaction: Optional transaction
            mode: "account" (default) or "user" (legacy)
        """
        collection = "accounts" if mode == "account" else "users"
        entity_ref = db.collection(collection).document(id_val)
        doc_ref = self._get_monthly_doc_ref(id_val, mode=mode)
        month_str = self._get_month_key()
        
        # If external transaction provided, use it directly (Sync)
        if transaction:
            return self._check_and_reserve_logic(transaction, entity_ref, doc_ref, id_val, feature, amount, month_str)

        # Otherwise, create a new isolated transaction
        @firestore.transactional
        def txn_wrapper(txn, u_ref, m_ref):
            return self._check_and_reserve_logic(txn, u_ref, m_ref, id_val, feature, amount, month_str)

        return txn_wrapper(db.transaction(), entity_ref, doc_ref)

    async def refund_consumption(self, id_val: str, feature: str, amount: float = 1.0, mode: str = "account"):
        """
        [TRIPLE LOCK] Refund Logic.
        """
        doc_ref = self._get_monthly_doc_ref(id_val, mode=mode)
        try:
            doc_ref.set({
                feature: firestore.Increment(-amount),
                "refundedAt": firestore.SERVER_TIMESTAMP,
                "updatedAt": firestore.SERVER_TIMESTAMP
            }, merge=True)
            logger.info(f"[CostGuard] Refunded {amount} for {feature} to {mode} {id_val}")
        except Exception as e:
            logger.error(f"[CostGuard] Failed to refund {amount} for {feature} to {mode} {id_val}: {e}")

    async def get_usage_report(self, id_val: str, mode: str = "account") -> dict:
        """
        [vNext] Returns a dictionary matching CloudUsageReport model.
        Sums up limits and current usage for the current (JST) month.
        Args:
            id_val: accountId (or uid)
            mode: "account" or "user"
        """
        collection = "accounts" if mode == "account" else "users"
        entity_ref = db.collection(collection).document(id_val)
        doc_ref = self._get_monthly_doc_ref(id_val, mode=mode)

        u_snap = entity_ref.get()
        u_data = _safe_dict(u_snap)

        if not u_snap.exists:
            logger.info(f"[CostGuard] Entity {mode}/{id_val} not found, treating as new free user")

        # Resolve plan with account fallback (only for user mode)
        plan = _resolve_plan_from_data(u_data, user_id=id_val if mode == "user" else None)

        m_snap = doc_ref.get()
        m_data = _safe_dict(m_snap)

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
            "reasonIfBlocked": reason,
            # [NEW] AI Feature Counts (raw)
            "summaryGenerated": int(m_data.get("summary_generated", 0)),
            "quizGenerated": int(m_data.get("quiz_generated", 0)),
            "llmCalls": int(m_data.get("llm_calls", 0)),
            "_m_data": m_data # Expose for legacy mapping if needed
        }


    def _check_and_reserve_logic(self, transaction, u_ref, m_ref, user_id, feature, amount, month_str, u_snap=None, m_snap=None):
        # 1. Get Plan (with normalization and account fallback)
        if not u_snap:
            u_snap = u_ref.get(transaction=transaction)

        u_data = _safe_dict(u_snap)
        if not u_snap.exists:
            logger.info(f"[CostGuard] User {user_id} not found in DB, treating as new free user")

        plan = _resolve_plan_from_data(u_data, user_id=user_id)

        # 2. Get Usage
        if not m_snap:
            m_snap = m_ref.get(transaction=transaction)
        m_data = _safe_dict(m_snap)

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
            
            # [FIX] Use set(merge=True) instead of update() to handle non-existent docs
            transaction.set(u_ref, {"serverSessionCount": firestore.Increment(amount)}, merge=True)
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
