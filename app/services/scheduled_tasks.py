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
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

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


def create(account_id: str, *, body: Dict[str, Any]) -> Dict[str, Any]:
    if not account_id:
        raise ValueError("account_id required")
    rrule = body.get("rrule") or ""
    if not rrule:
        raise ValueError("rrule required")
    tzname = body.get("timezone") or "Asia/Tokyo"
    next_run = compute_next_run(rrule, tzname=tzname)
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
        upd["nextRunAt"] = compute_next_run(rrule, tzname=tzname)
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

def find_due(now: Optional[datetime] = None, limit: int = 200) -> List[Tuple[str, Dict[str, Any]]]:
    """Return [(account_id, task_doc), ...] for every enabled task whose
    nextRunAt is <= now. Cross-account collection_group query keeps the
    tick endpoint a single read regardless of how many accounts exist.
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
    return out


def mark_run(account_id: str, task_id: str, *, success: bool, message: str = "") -> None:
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
    }
    if success:
        upd["lastRunOutcome"] = "ok"
    else:
        upd["lastRunOutcome"] = "failed"
        upd["lastRunError"] = message[:500]
    ref.update(upd)
