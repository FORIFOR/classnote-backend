"""AI Credit System — usage-based billing with mode-aware costs.

Credit costs:
  - session_grounded (要約/TODO/要点):  1
  - session_plus_general:              2
  - general_static (一般質問):          2
  - general_fresh (最新情報調査):        5
  - summary_generated (要約生成):       1
  - quiz_generated (クイズ生成):         2
  - export_generated (共有文作成):       1

Plan allocations:
  - Free:     40 credits/month
  - Standard: 400 credits/month

Daily soft cap: 60 credits (prevents burst)
"""

import logging
from datetime import datetime, timezone, timedelta

from google.cloud import firestore
from app.firebase import db

logger = logging.getLogger("app.services.ai_credits")

# ── JST ──
JST = timezone(timedelta(hours=9))

# ── Credit costs per mode ──
CREDIT_COST = {
    # Chat modes
    "session_grounded": 1,
    "session_plus_general": 2,
    "general_static": 2,
    "general_fresh": 5,
    # Background tasks
    "summary_generated": 1,
    "quiz_generated": 2,
    "export_generated": 1,
    "audio_download": 1,
    # Overlay Assist
    "assist": 2,
}

# ── Plan allocations ──
PLAN_CREDITS = {
    "free": 40,
    "basic": 400,  # Standard plan
}

DAILY_SOFT_CAP = 60


def estimate_cost(mode: str) -> int:
    """Return the credit cost for a given mode/operation."""
    return CREDIT_COST.get(mode, 2)


def _month_key() -> str:
    return datetime.now(JST).strftime("%Y-%m")


def _today_key() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")


def _resolve_plan(u_data: dict) -> str:
    plan = u_data.get("plan", "free")
    # PR1 business-license: "business" is a paid tier provisioned via
    # POST /v1/licenses:redeem. Treat it as basic-equivalent for AI credit
    # accounting until/unless a business-specific cap is introduced.
    if plan in ("basic", "standard", "business"):
        return "basic"
    return "free"


class AICreditService:
    """Manages AI credit consumption with monthly + daily limits."""

    def _get_monthly_ref(self, account_id: str):
        return (
            db.collection("accounts")
            .document(account_id)
            .collection("monthly_usage")
            .document(_month_key())
        )

    def compute_credit_report_from_data(self, acc_data: dict, m_data: dict) -> dict:
        """Pure (no-IO) variant of get_credit_report.

        Useful when the caller has already fetched accounts/{id} and
        accounts/{id}/monthly_usage/{YYYY-MM}. Avoids duplicate Firestore reads
        from cost_guard delegation paths.
        """
        plan = _resolve_plan(acc_data or {})
        unlimited = bool((acc_data or {}).get("unlimitedCredits", False))

        monthly_limit = PLAN_CREDITS.get(plan, 10)
        topup = int((acc_data or {}).get("topupCredits", 0) or 0)

        used = int((m_data or {}).get("ai_credits_used", 0) or 0)

        daily_used = int((m_data or {}).get("ai_credits_daily", 0) or 0)
        daily_date = (m_data or {}).get("ai_credits_daily_date", "")
        if daily_date != _today_key():
            daily_used = 0  # auto-reset (actual reset happens on consume)

        total_limit = monthly_limit + topup
        remaining = max(0, total_limit - used)

        return {
            "plan": plan,
            "monthlyLimit": monthly_limit,
            "topupCredits": topup,
            "used": used,
            "remaining": remaining,
            "dailyUsed": daily_used,
            "dailySoftCap": DAILY_SOFT_CAP,
            "unlimitedCredits": unlimited,
        }

    def get_credit_report(self, account_id: str) -> dict:
        """Get current credit status for the account.

        Returns dict with:
          monthlyLimit, used, remaining, dailyUsed, dailySoftCap, plan, unlimitedCredits, topupCredits
        """
        acc_snap = db.collection("accounts").document(account_id).get()
        acc_data = acc_snap.to_dict() or {} if acc_snap.exists else {}
        m_snap = self._get_monthly_ref(account_id).get()
        m_data = m_snap.to_dict() or {} if m_snap.exists else {}
        return self.compute_credit_report_from_data(acc_data, m_data)

    def can_consume(self, account_id: str, cost: int) -> tuple[bool, dict]:
        """Check if account can consume `cost` credits.

        Returns (allowed, info_dict).
        """
        report = self.get_credit_report(account_id)

        # Admin/master accounts with unlimitedCredits bypass all limits
        if report.get("unlimitedCredits"):
            return True, report

        if report["remaining"] < cost:
            logger.warning(
                f"[AICredits] BLOCKED {account_id}: remaining={report['remaining']} < cost={cost}"
            )
            return False, {
                "reason": "monthly_credit_limit",
                "remaining": report["remaining"],
                "cost": cost,
                **report,
            }

        if report["dailyUsed"] + cost > DAILY_SOFT_CAP:
            logger.warning(
                f"[AICredits] DAILY_CAP {account_id}: daily={report['dailyUsed']}+{cost} > {DAILY_SOFT_CAP}"
            )
            return False, {
                "reason": "daily_credit_limit",
                "dailyUsed": report["dailyUsed"],
                "cost": cost,
                **report,
            }

        return True, report

    def consume(self, account_id: str, cost: int, mode: str) -> dict:
        """Atomically consume credits. Returns updated report.

        Uses Firestore transaction to prevent race conditions.
        """
        acc_ref = db.collection("accounts").document(account_id)
        m_ref = self._get_monthly_ref(account_id)
        today = _today_key()

        @firestore.transactional
        def _txn(transaction):
            acc_snap = acc_ref.get(transaction=transaction)
            acc_data = acc_snap.to_dict() or {} if acc_snap.exists else {}
            plan = _resolve_plan(acc_data)
            unlimited = bool(acc_data.get("unlimitedCredits", False))

            m_snap = m_ref.get(transaction=transaction)
            m_data = m_snap.to_dict() or {} if m_snap.exists else {}

            monthly_limit = PLAN_CREDITS.get(plan, 10) + int(acc_data.get("topupCredits", 0))
            used = int(m_data.get("ai_credits_used", 0))

            # Admin/master accounts with unlimitedCredits bypass all limit checks
            if not unlimited:
                if used + cost > monthly_limit:
                    return False, {
                        "reason": "monthly_credit_limit",
                        "remaining": max(0, monthly_limit - used),
                        "cost": cost,
                    }

                # Daily reset if new day
                daily_date = m_data.get("ai_credits_daily_date", "")
                daily_used = int(m_data.get("ai_credits_daily", 0))
                if daily_date != today:
                    daily_used = 0

                if daily_used + cost > DAILY_SOFT_CAP:
                    return False, {
                        "reason": "daily_credit_limit",
                        "dailyUsed": daily_used,
                        "cost": cost,
                    }

            # Daily reset if new day (needed for tracking even in unlimited mode)
            daily_date = m_data.get("ai_credits_daily_date", "")
            daily_used = int(m_data.get("ai_credits_daily", 0))
            if daily_date != today:
                daily_used = 0

            # Consume
            updates = {
                "ai_credits_used": firestore.Increment(cost),
                "ai_credits_daily": daily_used + cost if daily_date != today else firestore.Increment(cost),
                "ai_credits_daily_date": today,
                f"ai_credits_by_mode.{mode}": firestore.Increment(cost),
                "updated_at": datetime.now(timezone.utc),
            }

            if m_snap.exists:
                transaction.update(m_ref, updates)
            else:
                transaction.set(m_ref, updates, merge=True)

            new_used = used + cost
            new_daily = daily_used + cost
            return True, {
                "plan": plan,
                "monthlyLimit": monthly_limit,
                "used": new_used,
                "remaining": max(0, monthly_limit - new_used),
                "dailyUsed": new_daily,
                "dailySoftCap": DAILY_SOFT_CAP,
            }

        return _txn(db.transaction())

    def refund(self, account_id: str, cost: int, mode: str):
        """Refund credits on failure."""
        m_ref = self._get_monthly_ref(account_id)
        try:
            m_ref.set(
                {
                    "ai_credits_used": firestore.Increment(-cost),
                    "ai_credits_daily": firestore.Increment(-cost),
                    f"ai_credits_by_mode.{mode}": firestore.Increment(-cost),
                    "updated_at": datetime.now(timezone.utc),
                },
                merge=True,
            )
            logger.info(f"[AICredits] Refunded {cost} credits for {mode} to {account_id}")
        except Exception as e:
            logger.error(f"[AICredits] Refund failed for {account_id}: {e}")


# Singleton
ai_credits = AICreditService()
