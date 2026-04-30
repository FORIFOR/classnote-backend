"""Backfill: rescue sessions stuck at status="処理中".

Target: sessions where the post-processing pipeline succeeded
(transcriptText written + summaryStatus=completed) but session.status
was never advanced past "処理中". Symptom: desktop / iOS shows a
"transcribing" spinner forever, dashboard shows 処理中 even though
summary/quiz are visible.

Default mode is DRY-RUN. Re-run with --apply to actually write.

Usage:
    python scripts/db/fix_stuck_processing_sessions.py            # dry-run
    python scripts/db/fix_stuck_processing_sessions.py --apply    # write

Selection criteria (all must hold):
  - sessions/{sid}.status == "処理中"
  - sessions/{sid}.transcriptText is non-empty
  - sessions/{sid}.summaryStatus == "completed"

Action: status -> "完了", updatedAt -> SERVER_TIMESTAMP, audit field
backfilledFromStuckProcessingAt -> SERVER_TIMESTAMP. transactional per
document; all-or-nothing per session.

Reasoning behind the filter:
  - status="処理中" alone is too broad — recordings legitimately mid-flight
    would be touched and trigger duplicate finalize jobs.
  - transcriptText non-empty proves the transcribe step finished writing.
  - summaryStatus=completed proves the post-transcribe pipeline finished;
    only the status state-machine bit was forgotten. This is the actual
    bug class observed for ~63 sessions on 2026-04-30 (mode mix:
    device_sherpa 44 / cloud_google 20).
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Tuple

import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore as fb_firestore
from google.cloud import firestore


def _init_db():
    key_path = "classnote-api-key.json"
    if os.path.exists(key_path):
        cred = credentials.Certificate(key_path)
    else:
        cred = credentials.ApplicationDefault()
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return fb_firestore.client()


def _find_candidates(db, limit: int) -> List[Tuple[str, dict]]:
    """Stream sessions matching the rescue filter.

    Streams up to `limit` documents to avoid runaway scans on large
    collections. Returns (sid, data) tuples.
    """
    docs = (
        db.collection("sessions")
        .where("status", "==", "処理中")
        .limit(limit)
        .stream()
    )
    out: List[Tuple[str, dict]] = []
    for d in docs:
        data = d.to_dict() or {}
        if not (data.get("transcriptText") or "").strip():
            continue
        if data.get("summaryStatus") != "completed":
            continue
        out.append((d.id, data))
    return out


def _apply_one(db, sid: str) -> None:
    ref = db.collection("sessions").document(sid)

    @firestore.transactional
    def _txn(transaction):
        snap = ref.get(transaction=transaction)
        data = snap.to_dict() or {}
        # Re-check inside the transaction so we never overwrite a session
        # that has already moved on (e.g. a worker promoted it to 完了 or
        # 失敗 between scan and write).
        if data.get("status") != "処理中":
            return False, data.get("status")
        if not (data.get("transcriptText") or "").strip():
            return False, "no_transcript"
        if data.get("summaryStatus") != "completed":
            return False, "summary_not_completed"
        transaction.update(
            ref,
            {
                "status": "完了",
                "updatedAt": firestore.SERVER_TIMESTAMP,
                "backfilledFromStuckProcessingAt": firestore.SERVER_TIMESTAMP,
            },
        )
        return True, "ok"

    ok, reason = _txn(db.transaction())
    if ok:
        print(f"  [APPLIED] {sid}")
    else:
        print(f"  [SKIPPED] {sid} reason={reason}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write (default: dry-run)")
    ap.add_argument("--limit", type=int, default=500, help="max sessions to scan")
    args = ap.parse_args()

    db = _init_db()
    candidates = _find_candidates(db, args.limit)
    print(f"Found {len(candidates)} stuck sessions matching the rescue filter.")
    by_mode: dict = {}
    for sid, data in candidates:
        m = data.get("transcriptionMode") or "?"
        by_mode[m] = by_mode.get(m, 0) + 1
    print(f"  by mode: {by_mode}")

    if not candidates:
        return

    if not args.apply:
        print("\nDRY-RUN. Re-run with --apply to write. Sample (first 10):")
        for sid, data in candidates[:10]:
            tlen = len(data.get("transcriptText") or "")
            mode = data.get("transcriptionMode")
            print(f"  {sid}  mode={mode}  transcriptTextLen={tlen}  title=\"{(data.get('title') or '')[:40]}\"")
        sys.exit(0)

    print(f"\nApplying status='完了' to {len(candidates)} sessions (transactional per-doc)...")
    for sid, _ in candidates:
        _apply_one(db, sid)
    print("Done.")


if __name__ == "__main__":
    main()
