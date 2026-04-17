from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from google.cloud import firestore

from app.firebase import db


logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def user_folders_ref(uid: str):
    return db.collection("users").document(uid).collection("folders")


def folder_ref(uid: str, folder_id: str):
    return user_folders_ref(uid).document(folder_id)


def session_meta_ref(uid: str, session_id: str):
    return db.collection("users").document(uid).collection("sessionMeta").document(session_id)


def _session_ref_doc(uid: str, session_id: str, folder_id: str):
    return folder_ref(uid, folder_id).collection("sessions").document(session_id)


def get_folder(uid: str, folder_id: str) -> Optional[Dict[str, Any]]:
    snap = folder_ref(uid, folder_id).get()
    if not snap.exists:
        return None
    data = snap.to_dict() or {}
    if data.get("deletedAt"):
        return None
    return data


def validate_folder(uid: str, folder_id: Optional[str]) -> None:
    if not folder_id:
        return
    if not get_folder(uid, folder_id):
        raise ValueError("folder not found")


# ---------------------------------------------------------------------------
# Account-aware helpers
#
# Folders live under users/{uid}/folders/* for historical reasons. After an
# account merge (e.g. Apple uid + LINE uid), a single account can contain
# multiple Firebase uids, and each device may authenticate as a different uid.
# Without an account-aware read layer, iOS-created folders are invisible to
# a Desktop session that authenticated as a different member uid ("unknown
# folder" in the UI). These helpers resolve folders and sessionMeta across
# every member uid in the account.
# ---------------------------------------------------------------------------


def resolve_member_uids(uid: str, account_id: Optional[str]) -> List[str]:
    """Return every uid that shares the same account, calling uid first."""
    if not account_id:
        return [uid]
    try:
        snap = db.collection("accounts").document(account_id).get()
        if snap.exists:
            members = (snap.to_dict() or {}).get("memberUids") or []
            if uid in members:
                ordered = [uid] + [m for m in members if m and m != uid]
            else:
                ordered = [uid] + [m for m in members if m]
            # de-duplicate while preserving order
            seen: set[str] = set()
            result: List[str] = []
            for m in ordered:
                if m and m not in seen:
                    seen.add(m)
                    result.append(m)
            return result or [uid]
    except Exception as exc:
        logger.warning(f"[project_folders] memberUids lookup failed for account={account_id}: {exc}")
    return [uid]


def find_folder_owner_uid(
    uid: str, account_id: Optional[str], folder_id: str
) -> Optional[str]:
    """Find which member uid actually stores a live folder doc."""
    for member_uid in resolve_member_uids(uid, account_id):
        snap = folder_ref(member_uid, folder_id).get()
        if snap.exists and not (snap.to_dict() or {}).get("deletedAt"):
            return member_uid
    return None


def iter_account_folders(
    uid: str, account_id: Optional[str]
) -> Iterable[Tuple[str, str, Dict[str, Any]]]:
    """Yield (owner_uid, folder_id, data) across all member uids, deduplicated.

    The caller's own uid wins on collision so locally-created folders reflect
    the most recent write on this device.
    """
    seen: set[str] = set()
    for member_uid in resolve_member_uids(uid, account_id):
        try:
            docs = list(user_folders_ref(member_uid).stream())
        except Exception as exc:
            logger.warning(
                f"[project_folders] folder stream failed for uid={member_uid}: {exc}"
            )
            continue
        for doc in docs:
            if doc.id in seen:
                continue
            data = doc.to_dict() or {}
            if data.get("deletedAt"):
                continue
            seen.add(doc.id)
            yield member_uid, doc.id, data


def find_session_meta_uid(
    uid: str, account_id: Optional[str], session_id: str
) -> Optional[str]:
    """Return the member uid that already has sessionMeta for this session."""
    for member_uid in resolve_member_uids(uid, account_id):
        snap = session_meta_ref(member_uid, session_id).get()
        if snap.exists:
            return member_uid
    return None


def validate_folder_account(
    uid: str, account_id: Optional[str], folder_id: Optional[str]
) -> None:
    if not folder_id:
        return
    if not find_folder_owner_uid(uid, account_id, folder_id):
        raise ValueError("folder not found")


def build_session_ref_payload(
    session_id: str,
    session_data: Dict[str, Any],
    folder_id: str,
    *,
    finalized: bool = False,
) -> Dict[str, Any]:
    now = _now()
    payload: Dict[str, Any] = {
        "sessionId": session_id,
        "folderId": folder_id,
        "titleSnapshot": session_data.get("title"),
        "modeSnapshot": session_data.get("mode"),
        "statusSnapshot": session_data.get("status"),
        "startedAt": session_data.get("startedAt") or session_data.get("startAt"),
        "endedAt": session_data.get("endedAt") or session_data.get("endAt"),
        "updatedAt": now,
    }
    if finalized:
        payload["finalizedAt"] = now
    return payload


def upsert_session_reference(
    uid: str,
    session_id: str,
    folder_id: str,
    session_data: Dict[str, Any],
    *,
    finalized: bool = False,
) -> None:
    ref = _session_ref_doc(uid, session_id, folder_id)
    ref.set(
        build_session_ref_payload(
            session_id=session_id,
            session_data=session_data,
            folder_id=folder_id,
            finalized=finalized,
        ),
        merge=True,
    )


def delete_session_reference(
    uid: str,
    session_id: str,
    folder_id: Optional[str],
) -> None:
    if not folder_id:
        return
    _session_ref_doc(uid, session_id, folder_id).delete()


def set_session_organization(
    uid: str,
    session_id: str,
    folder_id: Optional[str],
    *,
    session_data: Optional[Dict[str, Any]] = None,
    finalized: bool = False,
    account_id: Optional[str] = None,
) -> Dict[str, Any]:
    # Resolve the folder across the account's member uids so a folder created
    # on iOS (uid A) can be selected from Desktop (uid B) without the caller
    # having to know which uid actually owns the doc.
    if folder_id:
        folder_owner_uid = find_folder_owner_uid(uid, account_id, folder_id)
        if not folder_owner_uid:
            # Fall back to the legacy uid-scoped check for backwards compat.
            if get_folder(uid, folder_id) is None:
                raise ValueError("folder not found")
            folder_owner_uid = uid
    else:
        folder_owner_uid = uid

    # Prefer the uid that already holds sessionMeta (sessions created by that
    # member uid); otherwise write to the caller's own uid.
    meta_owner_uid = find_session_meta_uid(uid, account_id, session_id) or uid
    meta_ref = session_meta_ref(meta_owner_uid, session_id)
    meta_snap = meta_ref.get()
    prev_meta = meta_snap.to_dict() if meta_snap.exists else {}

    prev_folder_id = prev_meta.get("folderId")
    if prev_folder_id and prev_folder_id != folder_id:
        prev_folder_owner = (
            find_folder_owner_uid(uid, account_id, prev_folder_id) or meta_owner_uid
        )
        delete_session_reference(prev_folder_owner, session_id, prev_folder_id)

    now = _now()
    meta_patch: Dict[str, Any] = {
        "folderId": folder_id,
        "updatedAt": now,
        "organizationUpdatedAt": now,
    }
    if finalized and folder_id:
        meta_patch["organizedAt"] = firestore.SERVER_TIMESTAMP
        meta_patch["organizationStatus"] = "finalized"
    meta_ref.set(meta_patch, merge=True)

    if folder_id and session_data:
        upsert_session_reference(
            uid=folder_owner_uid,
            session_id=session_id,
            folder_id=folder_id,
            session_data=session_data,
            finalized=finalized,
        )

    return {
        "sessionId": session_id,
        "folderId": folder_id,
    }


def refresh_session_organization_on_finalize(
    uid: str,
    session_id: str,
    session_data: Dict[str, Any],
    *,
    account_id: Optional[str] = None,
) -> None:
    # sessionMeta and the target folder may live under different member uids
    # (e.g. Desktop created the session; iOS owns the folder). Resolve both
    # independently across the account.
    meta_owner_uid = find_session_meta_uid(uid, account_id, session_id)
    if not meta_owner_uid:
        return
    meta_snap = session_meta_ref(meta_owner_uid, session_id).get()
    if not meta_snap.exists:
        return
    meta = meta_snap.to_dict() or {}
    folder_id = meta.get("folderId")
    if not folder_id:
        return
    folder_owner_uid = find_folder_owner_uid(uid, account_id, folder_id)
    if not folder_owner_uid:
        return
    upsert_session_reference(
        uid=folder_owner_uid,
        session_id=session_id,
        folder_id=folder_id,
        session_data=session_data,
        finalized=True,
    )
