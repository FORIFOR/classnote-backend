"""
Security Service - Time-window based risk scoring & enforcement

Design:
  - Events are logged to users/{uid}/security/events/{eventId}
  - Counters are maintained in users/{uid}/security/counters/current (10m/1h/24h windows)
  - Effective risk is computed from counters (not permanently accumulated)
  - Security state: normal → watch → restricted → blocked
  - Auto-resolve: states downgrade after quiet periods
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Literal

from google.cloud import firestore

from app.firebase import db

logger = logging.getLogger("app.security")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SECURITY_STATES = ("normal", "watch", "restricted", "blocked")

# Thresholds for state transitions
THRESHOLD_WATCH = 30
THRESHOLD_RESTRICTED = 60
THRESHOLD_BLOCKED = 90

# Auto-resolve durations (hours of quiet required to downgrade)
AUTO_RESOLVE = {
    "watch":      {"to": "normal",     "quiet_hours": 6},
    "restricted": {"to": "watch",      "quiet_hours": 24},
    "blocked":    {"to": "restricted", "quiet_hours": 24},
}

# Event weights (per-occurrence in the relevant window)
EVENT_WEIGHTS: Dict[str, Dict[str, Any]] = {
    "inflight_limit_exceeded": {
        "window": "10m",
        "first_n_free": 2,     # 1st-2nd occurrence → log only, 0 points
        "weight": 1,           # 3rd-5th → +1 each
        "heavy_threshold": 6,  # 6th+ → +3 each
        "heavy_weight": 3,
        "cap": 15,             # max contribution from this event type
    },
    "upload_denied_expired": {
        "window": "24h",
        "first_n_free": 0,
        "weight": 1,           # 1st → +1
        "heavy_threshold": 2,  # 2nd+ → +2 each
        "heavy_weight": 2,
        "cap": 9,
    },
    "upload_denied_duration": {
        "window": "24h",
        "first_n_free": 0,
        "weight": 2,           # 1st → +2
        "heavy_threshold": 3,  # 3rd+ → +4 each
        "heavy_weight": 4,
        "cap": 12,
    },
    "upload_denied_size": {
        "window": "24h",
        "first_n_free": 0,
        "weight": 2,
        "heavy_threshold": 3,
        "heavy_weight": 4,
        "cap": 12,
    },
    # Future heavy events
    "invalid_auth_token": {
        "window": "24h",
        "first_n_free": 0,
        "weight": 8,
        "heavy_threshold": 999,
        "heavy_weight": 8,
        "cap": 40,
    },
    "burst_job_create": {
        "window": "10m",
        "first_n_free": 0,
        "weight": 8,
        "heavy_threshold": 999,
        "heavy_weight": 8,
        "cap": 24,
    },
}

# Counter field suffixes by window
WINDOW_SUFFIXES = {"10m": "_10m", "1h": "_1h", "24h": "_24h"}

# ---------------------------------------------------------------------------
# Risk computation (pure function – no DB)
# ---------------------------------------------------------------------------

@dataclass
class SecurityCounters:
    """Snapshot of a user's current security counters."""
    inflight_limit_exceeded_10m: int = 0
    inflight_limit_exceeded_1h: int = 0
    upload_denied_expired_24h: int = 0
    upload_denied_duration_24h: int = 0
    upload_denied_size_24h: int = 0
    invalid_auth_token_24h: int = 0
    burst_job_create_10m: int = 0
    last_event_at: Optional[datetime] = None


def _event_score(event_type: str, count: int) -> int:
    """Compute risk contribution for a single event type given its count."""
    cfg = EVENT_WEIGHTS.get(event_type)
    if not cfg or count <= 0:
        return 0

    effective = max(0, count - cfg["first_n_free"])
    if effective == 0:
        return 0

    normal_count = min(effective, max(0, cfg["heavy_threshold"] - 1 - cfg["first_n_free"]))
    heavy_count = max(0, effective - normal_count)

    score = normal_count * cfg["weight"] + heavy_count * cfg["heavy_weight"]
    return min(score, cfg["cap"])


def compute_effective_risk(c: SecurityCounters) -> int:
    """Compute the effective risk score from current counters."""
    score = 0
    score += _event_score("inflight_limit_exceeded", c.inflight_limit_exceeded_10m)
    score += _event_score("upload_denied_expired", c.upload_denied_expired_24h)
    score += _event_score("upload_denied_duration", c.upload_denied_duration_24h)
    score += _event_score("upload_denied_size", c.upload_denied_size_24h)
    score += _event_score("invalid_auth_token", c.invalid_auth_token_24h)
    score += _event_score("burst_job_create", c.burst_job_create_10m)
    return score


def decide_security_state(score: int) -> str:
    if score >= THRESHOLD_BLOCKED:
        return "blocked"
    if score >= THRESHOLD_RESTRICTED:
        return "restricted"
    if score >= THRESHOLD_WATCH:
        return "watch"
    return "normal"


# ---------------------------------------------------------------------------
# Firestore helpers
# ---------------------------------------------------------------------------

def _security_profile_ref(uid: str):
    return db.collection("users").document(uid).collection("security").document("profile")


def _counters_ref(uid: str):
    return db.collection("users").document(uid).collection("security").document("counters")


def _events_col(uid: str):
    return db.collection("users").document(uid).collection("security").document("events_log").collection("items")


def _counter_field(event_type: str) -> str:
    """Map event_type → counter field name (e.g. 'inflight_limit_exceeded' → 'inflight_limit_exceeded_10m')."""
    cfg = EVENT_WEIGHTS.get(event_type, {})
    window = cfg.get("window", "24h")
    suffix = WINDOW_SUFFIXES.get(window, "_24h")
    return f"{event_type}{suffix}"


# ---------------------------------------------------------------------------
# SecurityService
# ---------------------------------------------------------------------------

class SecurityService:
    """Stateless service – all state lives in Firestore."""

    # ------ Event registration ------

    async def register_event(
        self,
        uid: str,
        event_type: str,
        *,
        ip_hash: str = "",
        device_id: str = "",
        session_id: str = "",
        user_agent: str = "",
    ) -> dict:
        """
        Record a security event, update counters & re-evaluate state.
        Returns {"effectiveRisk": int, "securityState": str, "changed": bool}.
        """
        now = datetime.now(timezone.utc)
        try:
            # 1. Write event log
            event_data = {
                "type": event_type,
                "createdAt": now,
                "ipHash": ip_hash,
                "deviceId": device_id,
                "sessionId": session_id,
                "userAgent": user_agent,
            }
            _events_col(uid).add(event_data)

            # 2. Refresh counters from recent events
            counters = await self._recount_from_events(uid, now)

            # 3. Compute risk & state
            risk = compute_effective_risk(counters)
            new_state = decide_security_state(risk)

            # 4. Read current profile to detect state change
            profile_ref = _security_profile_ref(uid)
            profile_snap = profile_ref.get()
            old_state = "normal"
            if profile_snap.exists:
                old_state = profile_snap.to_dict().get("securityState", "normal")

            changed = new_state != old_state

            # 5. Update profile
            profile_update: Dict[str, Any] = {
                "effectiveRisk": risk,
                "securityState": new_state,
                "lastEvaluatedAt": now,
                "lastEventAt": now,
            }
            if changed:
                if new_state == "restricted":
                    profile_update["lastRestrictedAt"] = now
                elif new_state == "blocked":
                    profile_update["lastBlockedAt"] = now

            profile_ref.set(profile_update, merge=True)

            # 6. Mirror securityState to user doc for fast auth-level checks
            db.collection("users").document(uid).update({
                "securityState": new_state,
                "riskScore": risk,
            })

            # 7. Audit log on state change
            if changed:
                db.collection("security_audit_logs").add({
                    "user_id": uid,
                    "old_state": old_state,
                    "new_state": new_state,
                    "effectiveRisk": risk,
                    "trigger_event": event_type,
                    "timestamp": now,
                })
                logger.warning(
                    f"[Security] State change for {uid}: {old_state}→{new_state} "
                    f"(risk={risk}, trigger={event_type})"
                )
            else:
                logger.info(f"[Security] Event {event_type} for {uid}: risk={risk}, state={new_state}")

            return {"effectiveRisk": risk, "securityState": new_state, "changed": changed}

        except Exception as e:
            logger.error(f"[Security] Failed to register event for {uid}: {e}")
            return {"effectiveRisk": 0, "securityState": "normal", "changed": False}

    # ------ State check ------

    async def check_state(self, uid: str) -> str:
        """
        Return the user's current securityState.
        Also performs auto-resolve if enough quiet time has passed.
        Fail-open: returns 'normal' on error.
        """
        try:
            profile_ref = _security_profile_ref(uid)
            snap = profile_ref.get()
            if not snap.exists:
                return "normal"

            data = snap.to_dict()
            state = data.get("securityState", "normal")

            if state == "normal":
                return "normal"

            # Auto-resolve check
            resolved_state = await self._try_auto_resolve(uid, data)
            return resolved_state

        except Exception as e:
            logger.error(f"[Security] State check failed for {uid}: {e}")
            return "normal"

    async def _try_auto_resolve(self, uid: str, profile_data: dict) -> str:
        """Check if the user qualifies for auto-downgrade."""
        now = datetime.now(timezone.utc)
        state = profile_data.get("securityState", "normal")
        last_event = profile_data.get("lastEventAt")

        if state not in AUTO_RESOLVE or not last_event:
            return state

        # Firestore timestamps may be naive or aware
        if last_event.tzinfo is None:
            last_event = last_event.replace(tzinfo=timezone.utc)

        quiet_hours = (now - last_event).total_seconds() / 3600
        rule = AUTO_RESOLVE[state]

        if quiet_hours >= rule["quiet_hours"]:
            # Recount to be sure
            counters = await self._recount_from_events(uid, now)
            risk = compute_effective_risk(counters)
            new_state = decide_security_state(risk)

            # Only downgrade, never upgrade during auto-resolve
            state_order = {s: i for i, s in enumerate(SECURITY_STATES)}
            if state_order.get(new_state, 0) < state_order.get(state, 0):
                resolved = new_state
            else:
                # Score still high but quiet period passed → force one-step downgrade
                resolved = rule["to"]

            profile_ref = _security_profile_ref(uid)
            profile_ref.update({
                "securityState": resolved,
                "effectiveRisk": risk,
                "lastEvaluatedAt": now,
            })
            db.collection("users").document(uid).update({
                "securityState": resolved,
                "riskScore": risk,
            })

            if resolved != state:
                db.collection("security_audit_logs").add({
                    "user_id": uid,
                    "old_state": state,
                    "new_state": resolved,
                    "effectiveRisk": risk,
                    "trigger_event": "auto_resolve",
                    "quiet_hours": round(quiet_hours, 1),
                    "timestamp": now,
                })
                logger.info(
                    f"[Security] Auto-resolve {uid}: {state}→{resolved} "
                    f"(quiet={quiet_hours:.1f}h, risk={risk})"
                )

            return resolved

        return state

    # ------ Counter recount ------

    async def _recount_from_events(self, uid: str, now: datetime) -> SecurityCounters:
        """
        Recount security events from the event log within time windows.
        This is the source of truth – counters are derived, not accumulated.
        """
        cutoff_24h = now - timedelta(hours=24)
        cutoff_1h = now - timedelta(hours=1)
        cutoff_10m = now - timedelta(minutes=10)

        try:
            events = (
                _events_col(uid)
                .where("createdAt", ">=", cutoff_24h)
                .order_by("createdAt")
                .get()
            )

            counters = SecurityCounters(last_event_at=None)
            for ev in events:
                d = ev.to_dict()
                ev_type = d.get("type", "")
                created = d.get("createdAt")
                if not created:
                    continue

                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)

                if counters.last_event_at is None or created > counters.last_event_at:
                    counters.last_event_at = created

                # Count per window
                if ev_type == "inflight_limit_exceeded":
                    if created >= cutoff_10m:
                        counters.inflight_limit_exceeded_10m += 1
                    if created >= cutoff_1h:
                        counters.inflight_limit_exceeded_1h += 1
                elif ev_type == "upload_denied_expired":
                    if created >= cutoff_24h:
                        counters.upload_denied_expired_24h += 1
                elif ev_type == "upload_denied_duration":
                    if created >= cutoff_24h:
                        counters.upload_denied_duration_24h += 1
                elif ev_type == "upload_denied_size":
                    if created >= cutoff_24h:
                        counters.upload_denied_size_24h += 1
                elif ev_type == "invalid_auth_token":
                    if created >= cutoff_24h:
                        counters.invalid_auth_token_24h += 1
                elif ev_type == "burst_job_create":
                    if created >= cutoff_10m:
                        counters.burst_job_create_10m += 1

            # Persist computed counters for observability (non-critical)
            try:
                _counters_ref(uid).set({
                    "inflight_limit_exceeded_10m": counters.inflight_limit_exceeded_10m,
                    "inflight_limit_exceeded_1h": counters.inflight_limit_exceeded_1h,
                    "upload_denied_expired_24h": counters.upload_denied_expired_24h,
                    "upload_denied_duration_24h": counters.upload_denied_duration_24h,
                    "upload_denied_size_24h": counters.upload_denied_size_24h,
                    "invalid_auth_token_24h": counters.invalid_auth_token_24h,
                    "burst_job_create_10m": counters.burst_job_create_10m,
                    "lastEventAt": counters.last_event_at,
                    "recomputedAt": now,
                })
            except Exception:
                pass  # non-critical

            return counters

        except Exception as e:
            logger.error(f"[Security] Recount failed for {uid}: {e}")
            return SecurityCounters()

    # ------ Enforcement helpers ------

    async def enforce(
        self,
        uid: str,
        operation: Literal[
            "session_create", "device_sync", "job_create",
            "upload", "websocket", "read_only"
        ],
    ) -> Optional[Dict[str, Any]]:
        """
        Check security state and return None if allowed,
        or a dict with 'status_code' and 'detail' if denied.

        Operation-specific rules:
          - normal:     everything allowed
          - watch:      everything allowed (log only)
          - restricted: high-cost ops denied (job_create, upload, session_create)
                        read/sync allowed but with 429 hint
          - blocked:    all mutating ops denied, read-only allowed
        """
        state = await self.check_state(uid)

        if state == "normal":
            return None

        if state == "watch":
            logger.info(f"[Security] Watch-mode access: uid={uid} op={operation}")
            return None  # allow but logged

        if state == "restricted":
            if operation in ("job_create", "upload", "session_create"):
                return {
                    "status_code": 403,
                    "detail": "Account temporarily restricted for high-cost operations. Please try again later.",
                }
            if operation == "device_sync":
                return {
                    "status_code": 429,
                    "detail": "Too many requests. Please slow down.",
                }
            if operation == "websocket":
                return {
                    "status_code": 403,
                    "detail": "Account temporarily restricted.",
                }
            # read_only → allow
            return None

        if state == "blocked":
            if operation == "read_only":
                return None
            return {
                "status_code": 403,
                "detail": "Account temporarily blocked due to unusual activity. Please contact support.",
            }

        return None

    # ------ Admin helpers ------

    async def get_security_profile(self, uid: str) -> dict:
        """Return full security profile for admin view."""
        try:
            profile = _security_profile_ref(uid).get()
            counters = _counters_ref(uid).get()

            result = {
                "uid": uid,
                "profile": profile.to_dict() if profile.exists else {},
                "counters": counters.to_dict() if counters.exists else {},
            }

            # Recent events (last 20)
            recent = (
                _events_col(uid)
                .order_by("createdAt", direction=firestore.Query.DESCENDING)
                .limit(20)
                .get()
            )
            result["recentEvents"] = [
                {"id": ev.id, **ev.to_dict()} for ev in recent
            ]

            return result
        except Exception as e:
            logger.error(f"[Security] Failed to get profile for {uid}: {e}")
            return {"uid": uid, "error": str(e)}

    async def admin_reset(self, uid: str, admin_uid: str, reason: str = "") -> dict:
        """Admin-initiated reset: clear state to normal, keep event logs."""
        now = datetime.now(timezone.utc)

        _security_profile_ref(uid).set({
            "securityState": "normal",
            "effectiveRisk": 0,
            "lastEvaluatedAt": now,
            "lastRestrictedAt": None,
            "lastBlockedAt": None,
            "adminResetAt": now,
            "adminResetBy": admin_uid,
        })

        db.collection("users").document(uid).update({
            "securityState": "normal",
            "riskScore": 0,
        })

        db.collection("security_audit_logs").add({
            "user_id": uid,
            "old_state": "unknown",
            "new_state": "normal",
            "effectiveRisk": 0,
            "trigger_event": "admin_reset",
            "admin_uid": admin_uid,
            "reason": reason,
            "timestamp": now,
        })

        logger.info(f"[Security] Admin reset for {uid} by {admin_uid}: {reason}")
        return {"status": "reset", "securityState": "normal"}

    async def admin_set_state(
        self, uid: str, state: str, admin_uid: str, reason: str = ""
    ) -> dict:
        """Admin-initiated state override."""
        if state not in SECURITY_STATES:
            return {"error": f"Invalid state: {state}"}

        now = datetime.now(timezone.utc)

        _security_profile_ref(uid).set({
            "securityState": state,
            "lastEvaluatedAt": now,
            "adminOverrideAt": now,
            "adminOverrideBy": admin_uid,
        }, merge=True)

        db.collection("users").document(uid).update({
            "securityState": state,
        })

        db.collection("security_audit_logs").add({
            "user_id": uid,
            "new_state": state,
            "trigger_event": "admin_override",
            "admin_uid": admin_uid,
            "reason": reason,
            "timestamp": now,
        })

        logger.info(f"[Security] Admin set state for {uid} to {state} by {admin_uid}")
        return {"status": "updated", "securityState": state}


# Singleton
security_service = SecurityService()
