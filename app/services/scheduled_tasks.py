"""DeepNote Scheduled Tasks (Phase B foundation).

Per-account cron-style automations for chat-driven ops:

  ・毎週月曜 9:00 に先週の会議 TODO を Slack に
  ・毎朝 8:00 に未完了 TODO を LINE DM に
  ・毎週金曜 17:00 に今週の会議サマリーを Slack に
  ・etc.

Storage::

    accounts/{accountId}/scheduled_tasks/{taskId}
        type:        "weekly_meeting_digest" | "daily_open_todos" | "weekly_open_todos"
        channel:     "slack" | "line"
        destination: { workspaceId, channelId } | { lineUserId } | { lineGroupId }
        rrule:       "FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0"   (RFC 5545 subset)
        timezone:    "Asia/Tokyo"
        enabled:     bool
        lastRunAt:   timestamp | None
        nextRunAt:   timestamp                                   (precomputed)
        filters:     { folderId?, sessionRange? }
        output:      { includeSummary, includeTodos, includeDecisions, attachPdf }
        createdAt, updatedAt

Phase B implements the data model + a tick endpoint that fires due
tasks. We intentionally support only a small RRULE subset (FREQ=DAILY |
WEEKLY, BYDAY, BYHOUR, BYMINUTE) so we don't pull in dateutil.rrule.
Anything more complex falls through unchanged on the next tick.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from google.cloud import firestore  # type: ignore

from app.firebase import db

logger = logging.getLogger("app.services.scheduled_tasks")


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

WEEKDAY_MAP = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _parse_rrule(rrule: str) -> Dict[str, Any]:
    """Parse the small RFC 5545 subset we accept. Unknown keys are kept
    as raw strings; the scheduler ignores them.
    """
    out: Dict[str, Any] = {}
    if not rrule:
        return out
    parts = [p for p in rrule.replace("RRULE:", "").split(";") if p.strip()]
    for p in parts:
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        out[k.strip().upper()] = v.strip()
    return out


def _tz_offset(tzname: str) -> timedelta:
    """Return UTC offset for the few timezones we actually need. Falls
    back to UTC. (Avoids pytz/zoneinfo dependency for portability.)
    """
    if tzname in ("Asia/Tokyo", "JST"):
        return timedelta(hours=9)
    if tzname in ("UTC", "Etc/UTC"):
        return timedelta(0)
    # Default: assume UTC. SRE can extend later.
    return timedelta(0)


def compute_next_run(rrule: str, *, tzname: str = "Asia/Tokyo", after: Optional[datetime] = None) -> Optional[datetime]:
    """Best-effort next-run computation in UTC. Returns None if the
    rule is unsupported (caller can leave nextRunAt unset).
    """
    spec = _parse_rrule(rrule)
    freq = spec.get("FREQ")
    if freq not in ("DAILY", "WEEKLY"):
        return None
    by_hour = int(spec.get("BYHOUR", 9))
    by_minute = int(spec.get("BYMINUTE", 0))
    by_day_raw = spec.get("BYDAY")
    by_days: List[int] = []
    if by_day_raw:
        for d in by_day_raw.split(","):
            wd = WEEKDAY_MAP.get(d.strip().upper())
            if wd is not None:
                by_days.append(wd)

    base_after = after or datetime.now(timezone.utc)
    offset = _tz_offset(tzname)
    # Search up to 14 days forward; that's enough for any DAILY / WEEKLY pattern.
    for delta in range(0, 14):
        candidate_local = (base_after + offset + timedelta(days=delta)).replace(
            hour=by_hour, minute=by_minute, second=0, microsecond=0
        )
        candidate_utc = candidate_local - offset
        if freq == "WEEKLY" and by_days:
            if candidate_local.weekday() not in by_days:
                continue
        if candidate_utc <= base_after:
            continue
        return candidate_utc
    return None


# ──────────────────────────────────────────────────────────────────────
# CRUD
# ──────────────────────────────────────────────────────────────────────

def _coll(account_id: str):
    return db.collection("accounts").document(account_id).collection("scheduled_tasks")


class InvalidRRuleError(ValueError):
    """Raised when an RRULE string is missing, syntactically invalid, or
    cannot produce a future ``nextRunAt``. Routes translate this into
    HTTP 400 with ``code=invalid_rrule``."""


def create(account_id: str, *, body: Dict[str, Any]) -> Dict[str, Any]:
    if not account_id:
        raise ValueError("account_id required")
    rrule = body.get("rrule") or ""
    if not rrule:
        raise InvalidRRuleError("rrule required")
    tzname = body.get("timezone") or "Asia/Tokyo"
    next_run = compute_next_run(rrule, tzname=tzname)
    if next_run is None:
        # Strict validation (2026-05-08): we used to silently store the
        # task with ``nextRunAt=None`` for unknown FREQ values; that
        # turned every typo into a permanently-stuck task. Reject at
        # create time instead so the client sees the failure.
        raise InvalidRRuleError(
            f"rrule could not be parsed into a next run time: {rrule!r}"
        )
    task_id = body.get("taskId") or f"st_{uuid.uuid4().hex[:16]}"
    now = datetime.now(timezone.utc)
    doc = {
        "taskId": task_id,
        "type": body.get("type") or "custom",
        "channel": body.get("channel") or "slack",
        "destination": body.get("destination") or {},
        "rrule": rrule,
        "timezone": tzname,
        "enabled": bool(body.get("enabled", True)),
        "filters": body.get("filters") or {},
        "output": body.get("output") or {
            "includeSummary": True,
            "includeTodos": True,
            "includeDecisions": True,
            "attachPdf": False,
        },
        "lastRunAt": None,
        "nextRunAt": next_run,
        "createdAt": now,
        "updatedAt": now,
    }
    _coll(account_id).document(task_id).set(doc)
    return doc


def list_for(account_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    out = []
    for s in _coll(account_id).limit(limit).stream():
        d = s.to_dict() or {}
        d["taskId"] = s.id
        out.append(d)
    return out


def update(account_id: str, task_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ref = _coll(account_id).document(task_id)
    snap = ref.get()
    if not snap.exists:
        return None
    upd = dict(patch)
    if "rrule" in upd or "timezone" in upd:
        cur = snap.to_dict() or {}
        rrule = upd.get("rrule") or cur.get("rrule") or ""
        tzname = upd.get("timezone") or cur.get("timezone") or "Asia/Tokyo"
        if not rrule:
            raise InvalidRRuleError("rrule required")
        next_run = compute_next_run(rrule, tzname=tzname)
        if next_run is None:
            raise InvalidRRuleError(
                f"rrule could not be parsed into a next run time: {rrule!r}"
            )
        upd["nextRunAt"] = next_run
    upd["updatedAt"] = datetime.now(timezone.utc)
    ref.update(upd)
    snap2 = ref.get()
    return snap2.to_dict() if snap2.exists else None


def delete(account_id: str, task_id: str) -> bool:
    ref = _coll(account_id).document(task_id)
    if not ref.get().exists:
        return False
    ref.delete()
    return True


# ──────────────────────────────────────────────────────────────────────
# Scheduler tick
# ──────────────────────────────────────────────────────────────────────

class SchedulerDueQueryError(RuntimeError):
    """Raised when find_due cannot complete (typically a missing
    Firestore composite index). Propagated to scheduler_tick which
    surfaces it to the caller as ``errorCode=scheduler_due_query_failed``
    so silent failure is impossible."""


def find_due(now: Optional[datetime] = None, limit: int = 200) -> List[Tuple[str, Dict[str, Any]]]:
    """Return [(account_id, task_doc), ...] for every enabled task whose
    nextRunAt is <= now. Cross-account collection_group query keeps the
    tick endpoint a single read regardless of how many accounts exist.

    Behaviour change (2026-05-08): exceptions are propagated as
    :class:`SchedulerDueQueryError` instead of being swallowed and
    returning ``[]``. The previous silent-fail mode let composite index
    errors masquerade as ``due=0`` for days. The caller is responsible
    for translating the exception into a structured response.
    """
    now = now or datetime.now(timezone.utc)
    out: List[Tuple[str, Dict[str, Any]]] = []
    try:
        from google.cloud import firestore as _fs  # type: ignore
        q = (
            db.collection_group("scheduled_tasks")
            .where(filter=_fs.FieldFilter("enabled", "==", True))
            .where(filter=_fs.FieldFilter("nextRunAt", "<=", now))
            .limit(limit)
        )
        for s in q.stream():
            d = s.to_dict() or {}
            d["taskId"] = s.id
            account_id = ""
            try:
                # path: accounts/{accountId}/scheduled_tasks/{taskId}
                parts = s.reference.path.split("/")
                if len(parts) >= 4 and parts[0] == "accounts":
                    account_id = parts[1]
            except Exception:
                pass
            if account_id:
                out.append((account_id, d))
    except Exception as e:
        logger.warning("[scheduled_tasks.find_due] failed: %s", e)
        raise SchedulerDueQueryError(str(e)) from e
    return out


def mark_run(account_id: str, task_id: str, *, success: bool, message: str = "",
             run_slot: Optional[datetime] = None) -> None:
    """Advance ``nextRunAt`` and record outcome of the run.

    ``run_slot`` is the original due ``nextRunAt`` of this fire — stored
    in ``lastRunSlot`` so a second tick with the same slot can detect
    duplicate dispatch via ``notification_events.idempotencyKey``.
    """
    ref = _coll(account_id).document(task_id)
    snap = ref.get()
    if not snap.exists:
        return
    d = snap.to_dict() or {}
    rrule = d.get("rrule") or ""
    tzname = d.get("timezone") or "Asia/Tokyo"
    now = datetime.now(timezone.utc)
    next_run = compute_next_run(rrule, tzname=tzname, after=now)
    upd: Dict[str, Any] = {
        "lastRunAt": now,
        "nextRunAt": next_run,
        "updatedAt": now,
        # Lease release: tick implementations always pair acquire/release.
        "leaseUntil": None,
        "runningJobId": None,
    }
    if run_slot is not None:
        upd["lastRunSlot"] = run_slot
    if success:
        upd["lastRunOutcome"] = "ok"
        upd["lastError"] = None
    else:
        upd["lastRunOutcome"] = "failed"
        upd["lastError"] = {"message": message[:500], "at": now}
    ref.update(upd)
    # Append a row to scheduled_task_runs for audit-grade history.
    # Failure to write the run doc must not block lease release; tick
    # cannot retry mark_run safely once nextRunAt has advanced.
    try:
        record_run(
            account_id=account_id,
            task_id=task_id,
            run_slot=run_slot,
            status="succeeded" if success else "failed",
            finished_at=now,
            error=None if success else (message or None),
        )
    except Exception as e:
        logger.warning("[scheduled_tasks.mark_run] record_run failed task=%s err=%s",
                       task_id, e)


def record_run(
    *,
    account_id: str,
    task_id: str,
    run_slot: Optional[datetime],
    status: str,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
    error: Optional[str] = None,
    result: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Append a single run-history row to ``scheduled_task_runs/{runId}``.

    Idempotent: a second call with the same (taskId, runSlot) returns the
    existing run id without writing a duplicate. ``run_slot`` may be None
    for runs that don't correspond to a scheduler-issued slot (e.g. a
    manual ``:run`` admin call) — those rows skip the idempotency check.
    """
    if status not in ("succeeded", "failed"):
        raise ValueError(f"record_run: invalid status {status!r}")
    finished_at = finished_at or datetime.now(timezone.utc)
    started_at = started_at or finished_at
    coll = db.collection("scheduled_task_runs")
    idem_key: Optional[str] = None
    if run_slot is not None:
        slot_iso = run_slot.isoformat() if hasattr(run_slot, "isoformat") else str(run_slot)
        idem_key = f"scheduled_task:{task_id}:{slot_iso}"
        try:
            existing = list(
                coll.where(filter=firestore.FieldFilter("idempotencyKey", "==", idem_key))
                .limit(1)
                .stream()
            )
            if existing:
                return existing[0].id
        except Exception as e:
            # Index-missing or transient: do not block — fall through to
            # write. A duplicate doc is preferable to a missing audit row.
            logger.warning("[scheduled_tasks.record_run] idem lookup failed: %s", e)

    run_id = f"run_{uuid.uuid4().hex[:16]}"
    doc: Dict[str, Any] = {
        "id": run_id,
        "taskId": task_id,
        "accountId": account_id,
        "status": status,
        "startedAt": started_at,
        "finishedAt": finished_at,
    }
    if run_slot is not None:
        doc["runSlot"] = run_slot
    if idem_key:
        doc["idempotencyKey"] = idem_key
    if error:
        doc["error"] = {"message": str(error)[:500]}
    if result:
        doc["result"] = result
    coll.document(run_id).set(doc)
    return run_id


# ──────────────────────────────────────────────────────────────────────
# Lease acquisition (multi-tick idempotency)
# ──────────────────────────────────────────────────────────────────────

LEASE_TTL_SECONDS = 300  # 5 minutes — long enough to outlast a slow dispatch,
                         # short enough that a crashed tick releases its tasks.


def try_acquire_lease(account_id: str, task_id: str, *,
                      run_slot: datetime, lease_holder_id: str) -> bool:
    """Atomically claim the right to dispatch this task for ``run_slot``.

    Returns True if the caller acquired the lease and should dispatch.
    Returns False if another tick already claimed it (their lease is
    still valid OR they already advanced ``lastRunSlot`` past this slot).

    Implementation: a Firestore transactional read-then-write that
    writes ``leaseUntil = now + LEASE_TTL_SECONDS`` and
    ``runningJobId = lease_holder_id`` only if the prior lease has
    expired AND ``lastRunSlot != run_slot``.
    """
    ref = _coll(account_id).document(task_id)
    now = datetime.now(timezone.utc)
    lease_until = now + timedelta(seconds=LEASE_TTL_SECONDS)

    transaction = db.transaction()

    @firestore.transactional  # type: ignore[attr-defined]
    def _txn(tx) -> bool:
        snap = ref.get(transaction=tx)
        if not snap.exists:
            return False
        d = snap.to_dict() or {}
        last_slot = d.get("lastRunSlot")
        if last_slot is not None and last_slot == run_slot:
            return False  # already dispatched this slot
        existing_lease = d.get("leaseUntil")
        if existing_lease is not None and existing_lease > now:
            return False  # someone else holds the lease
        tx.update(ref, {
            "leaseUntil": lease_until,
            "runningJobId": lease_holder_id,
            "updatedAt": now,
        })
        return True

    try:
        return bool(_txn(transaction))
    except Exception as e:
        logger.warning("[scheduled_tasks.lease] tx failed task=%s: %s", task_id, e)
        return False


def release_lease(account_id: str, task_id: str) -> None:
    """Clear lease fields (idempotent). Called on dispatch failure when
    we don't want to advance ``lastRunSlot`` either."""
    ref = _coll(account_id).document(task_id)
    try:
        ref.update({
            "leaseUntil": None,
            "runningJobId": None,
            "updatedAt": datetime.now(timezone.utc),
        })
    except Exception as e:
        logger.warning("[scheduled_tasks.lease] release failed task=%s: %s", task_id, e)


def build_run_slot_key(task_id: str, run_slot: datetime) -> str:
    """Idempotency key shared between scheduled_tasks and notification_events.
    Format ``scheduled_task:{taskId}:{run_slot ISO 8601 UTC}``."""
    iso = run_slot.replace(microsecond=(run_slot.microsecond // 1000) * 1000).isoformat()
    return f"scheduled_task:{task_id}:{iso}"
