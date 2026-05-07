"""Backfill ``line_group_links`` from existing shared-session signals.

Phase 1 group ACL relies on ``line_group_links/{group_id}`` to know who
the data / billing owner of a LINE group is. Before this branch shipped,
the bot used the *requester*'s personal link instead — so groups that
were already operating have no link doc yet.

This one-shot script discovers existing groups by scanning
``sessions.sharedToWorkspaceTeams`` (the inverse map written when the
bot replied to a group) and creates the missing ``line_group_links``
entries with the session owner promoted to ``owner`` role.

Slack channels are NOT backfilled because ``sharedToWorkspaceTeams``
only stores ``slack:{team_id}`` (no channel_id). Slack channels must
issue ``DeepNote 接続`` once after deploy.

Usage::

    python tools/init_group_links_from_existing_links.py            # dry-run
    python tools/init_group_links_from_existing_links.py --apply    # actually write

Idempotent — running twice is a no-op for groups that already have a
``line_group_links`` doc.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from typing import Dict, Optional, Set, Tuple

# Allow `python tools/...` from repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("backfill_group_links")


def _scan_groups_from_sessions(db) -> Dict[str, Set[str]]:
    """Return ``{group_id: {ownerAccountId, ...}}`` for every LINE
    group seen in ``sessions.sharedToWorkspaceTeams``.

    A group can have multiple session owners — we'll pick the most
    common one (mode) as the data_owner candidate; ties broken by
    earliest ``createdAt``.
    """
    out: Dict[str, Set[str]] = defaultdict(set)
    log.info("Scanning sessions for sharedToWorkspaceTeams …")
    n_seen = 0
    n_match = 0
    for snap in db.collection("sessions").stream():
        n_seen += 1
        d = snap.to_dict() or {}
        ws = d.get("sharedToWorkspaceTeams") or []
        owner = d.get("ownerAccountId")
        if not ws or not owner:
            continue
        for k in ws:
            if isinstance(k, str) and k.startswith("line:"):
                gid = k[len("line:"):]
                if gid:
                    out[gid].add(owner)
                    n_match += 1
    log.info("  scanned %d sessions; %d shared-to-line entries; %d unique groups",
             n_seen, n_match, len(out))
    return out


def _pick_owner_account(account_ids: Set[str]) -> Optional[str]:
    """For a group, pick the most plausible data_owner account. We
    currently take the first ``accountId`` (callers can pass an ordered
    iterable if a tie-break is needed later)."""
    if not account_ids:
        return None
    return sorted(account_ids)[0]


def _find_line_user_for_account(db, account_id: str) -> Optional[Tuple[str, str]]:
    """Return ``(line_user_id, deepnote_uid)`` for a linked user matching
    this accountId, or ``None`` if no LINE user is linked yet."""
    query = (
        db.collection("line_user_links")
        .where("accountId", "==", account_id)
        .limit(1)
    )
    for snap in query.stream():
        d = snap.to_dict() or {}
        return snap.id, d.get("deepnoteUid", "")
    return None


def main(apply_changes: bool) -> int:
    from app.firebase import db  # heavy import; deferred for --help speed
    from app.services import group_acl

    groups = _scan_groups_from_sessions(db)
    if not groups:
        log.info("No groups discovered from sessions. Nothing to backfill.")
        return 0

    created = 0
    skipped_existing = 0
    skipped_no_account = 0
    skipped_no_line_user = 0

    for group_id, account_ids in groups.items():
        if group_acl.get_group_link("line", group_id):
            skipped_existing += 1
            continue
        owner_account = _pick_owner_account(account_ids)
        if not owner_account:
            skipped_no_account += 1
            continue
        line_user_match = _find_line_user_for_account(db, owner_account)
        if not line_user_match:
            log.info("  [skip] group=%s account=%s — no linked LINE user (will need explicit DeepNote 接続)",
                     group_id[:8] + "…", owner_account[:8] + "…")
            skipped_no_line_user += 1
            continue
        line_user_id, deepnote_uid = line_user_match
        log.info("  [%s] group=%s … account=%s line_user=%s",
                 "apply" if apply_changes else "dryrun",
                 group_id[:8] + "…", owner_account[:8] + "…", line_user_id[:8] + "…")
        if apply_changes:
            try:
                group_acl.create_group_link(
                    "line", group_id,
                    owner_deepnote_uid=deepnote_uid,
                    owner_account_id=owner_account,
                    created_by_source_user_id=line_user_id,
                )
            except Exception as e:
                log.warning("    create_group_link failed: %s", e)
                continue
            created += 1
        else:
            created += 1  # would-create

    log.info("Done. %s=%d, skipped_existing=%d, skipped_no_account=%d, skipped_no_line_user=%d",
             "created" if apply_changes else "would_create",
             created, skipped_existing, skipped_no_account, skipped_no_line_user)
    if not apply_changes:
        log.info("(dry-run — pass --apply to actually create line_group_links)")
    return 0


if __name__ == "__main__":
    desc = (__doc__ or "Backfill line_group_links from sessions").split("\n\n")[0]
    p = argparse.ArgumentParser(description=desc)
    p.add_argument("--apply", action="store_true",
                   help="Actually write line_group_links (default: dry-run)")
    ns = p.parse_args()
    sys.exit(main(apply_changes=ns.apply))
