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
    "cloud_stt_sec": 0,            # [POLICY] Free plan cannot use cloud transcription
    "cloud_sessions_started": 0,   # [POLICY] Free plan cannot use cloud transcription
    "summary_generated": 3,
    "quiz_generated": 3,
    "export_generated": 3,         # [NEW] Export: 3/month for free
    "server_session": 999999,      # Unlimited server sessions
    "sessions_created": 999999,    # No creation flow limit
    "ai_credits": 40,              # AI Chat: 40 credits/month
}


# Basic/Standard plan limits
BASIC_LIMITS = {
    "cloud_stt_sec": 7200,         # Cloud STT: 120 minutes/month
    "cloud_sessions_started": 300, # Cloud sessions: 300/month
    "summary_generated": 100,      # AI Summary: 100/month
    "quiz_generated": 100,         # AI Quiz: 100/month
    "export_generated": 999999,    # [NEW] Export: unlimited for Standard
    "server_session": 999999,      # Unlimited server sessions
    "sessions_created": 999999,    # Unlimited session creation
    "llm_calls": 200,              # Combined (Summary/Quiz/QA)
    "ai_credits": 400,             # AI Chat: 400 credits/month
}

def _safe_dict(snap) -> dict:
    """Safely get dict from DocumentSnapshot, handling None and non-existent docs."""
    if not snap or not snap.exists:
        return {}
    return snap.to_dict() or {}


def _normalize_plan(plan: str) -> str:
    """Normalize plan name to canonical form."""
    if plan in ("basic", "standard"):
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

    async def guard_can_consume(self, id_val: str, feature: str, amount: float = 1.0, transaction=None, mode: str = "account", user_id: str = None):
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
            user_id: Optional uid for bonus fallback when mode="account"
        """
        collection = "accounts" if mode == "account" else "users"
        entity_ref = db.collection(collection).document(id_val)
        doc_ref = self._get_monthly_doc_ref(id_val, mode=mode)
        month_str = self._get_month_key()

        # If external transaction provided, use it directly (Sync)
        if transaction:
            return self._check_and_reserve_logic(transaction, entity_ref, doc_ref, id_val, feature, amount, month_str, fallback_user_id=user_id if mode == "account" else None)

        # Otherwise, create a new isolated transaction
        @firestore.transactional
        def txn_wrapper(txn, u_ref, m_ref):
            return self._check_and_reserve_logic(txn, u_ref, m_ref, id_val, feature, amount, month_str, fallback_user_id=user_id if mode == "account" else None)

        return txn_wrapper(db.transaction(), entity_ref, doc_ref)

    async def record_success(self, id_val: str, feature: str, mode: str = "account"):
        """
        Record a successful completion in the monthly doc.
        This tracks actual successes separately from reservations (quiz_generated),
        allowing cross-check when reservation count diverges from reality.
        """
        doc_ref = self._get_monthly_doc_ref(id_val, mode=mode)
        # Map feature to success key: "quiz_generated" -> "quiz_success"
        success_key = feature.replace("_generated", "_success")
        try:
            doc_ref.set({
                success_key: firestore.Increment(1),
                "updatedAt": firestore.SERVER_TIMESTAMP
            }, merge=True)
        except Exception as e:
            logger.warning(f"[CostGuard] Failed to record success for {success_key}: {e}")

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

    async def get_usage_report(self, id_val: str, mode: str = "account", user_id: str = None) -> dict:
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
        if plan == "basic":
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

        # Apply limits for all plans
        if used_sec >= limit_sec:
            can_start = False
            reason = "cloud_minutes_limit"
        if sessions_started >= session_limit:
            can_start = False
            reason = "cloud_session_limit"

        # Invite bonuses (primary: from entity doc)
        invite_bonus_summary = int(u_data.get("inviteBonusSummary", 0))
        invite_bonus_quiz = int(u_data.get("inviteBonusQuiz", 0))

        # Fallback: bonuses may exist on users doc but not on accounts doc
        # (legacy invites wrote only to users/{uid})
        if mode == "account" and invite_bonus_summary == 0 and invite_bonus_quiz == 0 and user_id:
            try:
                fb_snap = db.collection("users").document(user_id).get()
                if fb_snap.exists:
                    fb_data = fb_snap.to_dict() or {}
                    invite_bonus_summary = int(fb_data.get("inviteBonusSummary", 0))
                    invite_bonus_quiz = int(fb_data.get("inviteBonusQuiz", 0))
                    if invite_bonus_summary > 0 or invite_bonus_quiz > 0:
                        logger.info(f"[CostGuard] Found invite bonuses on users/{user_id} (not accounts/{id_val}): summary={invite_bonus_summary}, quiz={invite_bonus_quiz}")
            except Exception as e:
                logger.warning(f"[CostGuard] Failed to check users doc for invite bonuses: {e}")

        # AI Credits: single source of truth via ai_credits.compute_credit_report_from_data.
        # Falls back to PLAN-only values if delegation fails or mode != "account".
        ai_credits_used = int(m_data.get("ai_credits_used", 0))
        ai_credits_limit = BASIC_LIMITS.get("ai_credits", 400) if plan == "basic" else FREE_LIMITS.get("ai_credits", 40)
        ai_credits_remaining = max(0, ai_credits_limit - ai_credits_used)
        ai_credits_unlimited = False
        ai_credits_topup = 0
        # Fallback values (overwritten by delegation when mode="account" succeeds).
        # Use 0 for daily_used to avoid leaking stale prior-day counters from the raw doc.
        ai_credits_daily_used = 0
        ai_credits_daily_remaining = 60  # DAILY_SOFT_CAP default

        if mode == "account" and u_snap.exists:
            try:
                from app.services.ai_credits import ai_credits as _ai_credits, DAILY_SOFT_CAP
                cr = _ai_credits.compute_credit_report_from_data(u_data, m_data)
                ai_credits_unlimited = bool(cr.get("unlimitedCredits", False))
                ai_credits_topup = int(cr.get("topupCredits", 0))
                ai_credits_used = int(cr.get("used", ai_credits_used))
                ai_credits_daily_used = int(cr.get("dailyUsed", 0))
                ai_credits_daily_remaining = max(0, int(cr.get("dailySoftCap", DAILY_SOFT_CAP)) - ai_credits_daily_used)
                if ai_credits_unlimited:
                    # Keep int contract for client compatibility; use a large but
                    # non-magic value AND surface aiCreditsUnlimited so clients can
                    # branch on the flag instead of the number.
                    ai_credits_limit = int(cr["monthlyLimit"]) + ai_credits_topup
                    ai_credits_remaining = max(0, ai_credits_limit - ai_credits_used)
                else:
                    ai_credits_limit = int(cr["monthlyLimit"]) + ai_credits_topup
                    ai_credits_remaining = int(cr.get("remaining", max(0, ai_credits_limit - ai_credits_used)))
            except Exception as e:
                logger.error(
                    f"[CostGuard] ai_credits delegation failed for account={id_val}: {e}",
                    exc_info=True,
                )
                # fall through to PLAN-only fallback values set above

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
            "summarySuccess": int(m_data.get("summary_success", 0)),
            "quizGenerated": int(m_data.get("quiz_generated", 0)),
            "quizSuccess": int(m_data.get("quiz_success", 0)),
            "llmCalls": int(m_data.get("llm_calls", 0)),
            "exportGenerated": int(m_data.get("export_generated", 0)),
            # Invite bonuses
            "bonusSummary": invite_bonus_summary,
            "bonusQuiz": invite_bonus_quiz,
            # AI Credits (delegated to ai_credits.compute_credit_report_from_data when mode="account")
            "aiCreditsUsed": ai_credits_used,
            "aiCreditsLimit": ai_credits_limit,
            "aiCreditsRemaining": ai_credits_remaining,
            "aiCreditsUnlimited": ai_credits_unlimited,
            "aiCreditsTopup": ai_credits_topup,
            "aiCreditsDailyUsed": ai_credits_daily_used,
            "aiCreditsDailyRemaining": ai_credits_daily_remaining,
            "_m_data": m_data # Expose for legacy mapping if needed
        }


    def _check_and_reserve_logic(self, transaction, u_ref, m_ref, user_id, feature, amount, month_str, u_snap=None, m_snap=None, fallback_user_id=None):
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
            if plan == "basic":
                limit = BASIC_LIMITS["server_session"]
            else:
                limit = FREE_LIMITS["server_session"]
            current = int(u_data.get("serverSessionCount", 0))

            if current + amount > limit:
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

        # Invite bonuses (stored on account/user doc)
        invite_bonus_summary = int(u_data.get("inviteBonusSummary", 0))
        invite_bonus_quiz = int(u_data.get("inviteBonusQuiz", 0))

        # Fallback: legacy invites wrote bonuses only to users/{uid}
        if invite_bonus_summary == 0 and invite_bonus_quiz == 0 and fallback_user_id:
            try:
                fb_snap = db.collection("users").document(fallback_user_id).get(transaction=transaction)
                if fb_snap.exists:
                    fb_data = fb_snap.to_dict() or {}
                    invite_bonus_summary = int(fb_data.get("inviteBonusSummary", 0))
                    invite_bonus_quiz = int(fb_data.get("inviteBonusQuiz", 0))
            except Exception:
                pass

        # Select limits based on plan
        if plan == "basic":
            if feature == "cloud_stt_sec":
                limit = BASIC_LIMITS["cloud_stt_sec"]
                current = float(m_data.get("cloud_stt_sec", 0.0))
            elif feature == "cloud_sessions_started":
                limit = BASIC_LIMITS["cloud_sessions_started"]
                current = int(m_data.get("cloud_sessions_started", 0))
            elif feature == "summary_generated":
                limit = BASIC_LIMITS["summary_generated"] + invite_bonus_summary
                current = int(m_data.get("summary_generated", 0))
            elif feature == "quiz_generated":
                limit = BASIC_LIMITS["quiz_generated"] + invite_bonus_quiz
                current = int(m_data.get("quiz_generated", 0))
            elif feature == "export_generated":
                limit = BASIC_LIMITS["export_generated"]
                current = int(m_data.get("export_generated", 0))
            elif feature == "llm_calls":
                # For Basic, also combine LLM calls
                limit = BASIC_LIMITS["summary_generated"] + BASIC_LIMITS["quiz_generated"] + invite_bonus_summary + invite_bonus_quiz
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
                limit = FREE_LIMITS["summary_generated"] + invite_bonus_summary
                current = int(m_data.get("summary_generated", 0))
            elif feature == "quiz_generated":
                limit = FREE_LIMITS["quiz_generated"] + invite_bonus_quiz
                current = int(m_data.get("quiz_generated", 0))
            elif feature == "export_generated":
                limit = FREE_LIMITS["export_generated"]
                current = int(m_data.get("export_generated", 0))
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
        updates[feature] = firestore.Increment(amount)

        # Execute Update
        if not m_snap.exists:
            transaction.set(m_ref, updates, merge=True)
        else:
            transaction.update(m_ref, updates)

        return True, None

# Singleton
cost_guard = CostGuardService()
