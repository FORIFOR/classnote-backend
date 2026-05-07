"""Recover sessions stranded at summaryStatus IN ("queued", "running").

Background:
    Vertex AI 429 ResourceExhausted bursts caused Cloud Tasks to exhaust
    its retry budget. The pre-fix worker raised 503 on transient errors
    even on the final attempt, so when Cloud Tasks gave up the session
    was left at summaryStatus="running" with no recovery path. This
    script scans Firestore for those stranded sessions and re-enqueues
    the summarize task. Safe to run repeatedly (idempotency keys are
    used so completed work is not duplicated).

Usage (from project root):
    GOOGLE_CLOUD_PROJECT=classnote-x-dev \
    python tools/recover_stuck_summaries.py --dry-run --age-minutes 30

    # Actually re-enqueue (default age threshold 30 min, no limit):
    GOOGLE_CLOUD_PROJECT=classnote-x-dev \
    python tools/recover_stuck_summaries.py --age-minutes 30 --limit 200
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone


def _stuck_query(db, age_minutes: int):
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    sessions = db.collection("sessions")
    rows = []
    seen = set()
    for status in ("queued", "running"):
        q = sessions.where("summaryStatus", "==", status).limit(1000)
        for snap in q.stream():
            if snap.id in seen:
                continue
            data = snap.to_dict() or {}
            updated = data.get("summaryUpdatedAt") or data.get("summaryQueuedAt") or data.get("updatedAt")
            if hasattr(updated, "to_datetime"):
                try:
                    updated = updated.to_datetime()
                except Exception:
                    updated = None
            if updated is None:
                # No timestamp — treat as stale (worst case re-enqueue once)
                rows.append((snap.id, data, None))
                seen.add(snap.id)
                continue
            if isinstance(updated, datetime) and updated <= cutoff:
                rows.append((snap.id, data, updated))
                seen.add(snap.id)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover stuck summary sessions")
    parser.add_argument("--age-minutes", type=int, default=30,
                        help="Only re-enqueue sessions not updated for this many minutes (default 30)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Maximum sessions to re-enqueue this run (0 = no limit)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be re-enqueued, do not call Cloud Tasks")
    parser.add_argument("--throttle-seconds", type=float, default=0.5,
                        help="Sleep between re-enqueues so Vertex AI quota isn't slammed again")
    args = parser.parse_args()

    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
    if not project_id:
        print("ERROR: GOOGLE_CLOUD_PROJECT not set", file=sys.stderr)
        return 2

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from google.cloud import firestore
    db = firestore.Client(project=project_id)

    rows = _stuck_query(db, args.age_minutes)
    print(f"Found {len(rows)} stuck session(s) (age >= {args.age_minutes} min)")

    if args.dry_run:
        for sid, data, updated in rows[: args.limit or len(rows)]:
            print(f"  [DRY] {sid} status={data.get('summaryStatus')} "
                  f"updated={updated} owner={data.get('ownerAccountId')}")
        return 0

    # Standalone Cloud Tasks enqueue — avoid importing app.task_queue so this
    # script runs without fastapi installed locally. Mirrors the production
    # ``enqueue_summarize_task`` payload (sessionId / jobId / idempotencyKey
    # / userId / usageReserved) and target URL exactly.
    import json as _json
    from google.cloud import tasks_v2  # type: ignore

    location = os.environ.get("TASKS_LOCATION", "asia-northeast1")
    queue_name = os.environ.get("SUMMARIZE_QUEUE", "summarize-queue")
    cloud_run_url = os.environ.get("CLOUD_RUN_SERVICE_URL")
    if not cloud_run_url:
        print("ERROR: CLOUD_RUN_SERVICE_URL not set "
              "(e.g. https://deepnote-api-...run.app)", file=sys.stderr)
        return 2

    tasks_client = tasks_v2.CloudTasksClient()
    parent = tasks_client.queue_path(project_id, location, queue_name)
    target_url = f"{cloud_run_url.rstrip('/')}/internal/tasks/summarize"

    enqueued = 0
    failed = 0
    for sid, data, updated in rows:
        if args.limit and enqueued >= args.limit:
            break
        owner_uid = data.get("ownerUserId") or data.get("userId") or data.get("ownerUid")
        idem = f"recover-stuck:{sid}:{int(time.time() // 60)}"
        payload = {
            "sessionId": sid,
            "jobId": None,
            "idempotencyKey": idem,
            "userId": owner_uid,
            "usageReserved": False,
        }
        task = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": target_url,
                "headers": {"Content-Type": "application/json"},
                "body": _json.dumps(payload).encode(),
            },
            "dispatch_deadline": {"seconds": 1800},
        }
        try:
            db.collection("sessions").document(sid).update({
                "summaryStatus": "queued",
                "summaryError": None,
                "summaryUpdatedAt": datetime.now(timezone.utc),
                "summaryRecoveredAt": datetime.now(timezone.utc),
            })
            tasks_client.create_task(parent=parent, task=task)
            enqueued += 1
            print(f"  [REQUEUE] {sid} (owner={owner_uid})")
        except Exception as e:
            failed += 1
            print(f"  [FAIL] {sid}: {e}")
        time.sleep(args.throttle_seconds)

    print(f"Done. enqueued={enqueued} failed={failed} skipped={len(rows) - enqueued - failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
