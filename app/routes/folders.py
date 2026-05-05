"""
folders.py — Folders / Library organisation

Stable canonical: /v1/folders (canonical) + /folders (legacy alias)
Storage: users/{uid}/folders/{folderId} — per-user, not shared.
A session belongs to a folder via optional `folderId` field on `sessions/{id}`.

See: deepnote-contracts/api/endpoints-map.md (V-017/V-018)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, status
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel, Field

from app.firebase import db
from app.dependencies import get_current_user, CurrentUser

logger = logging.getLogger("app.folders")

# canonical (/v1) と legacy (/folders) の両方を serve
router = APIRouter(prefix="/v1/folders", tags=["Folders"])
legacy_router = APIRouter(prefix="/folders", tags=["Folders"], include_in_schema=False)
move_router = APIRouter(tags=["Folders"])  # /v1/sessions/{id}:move + legacy alias
# colon-RPC action paths (`:bulkImport`) は prefix と "/" 区切りで結合されると
# `{folder_id}` variable にマッチしてしまうため、専用 router で path を直接書く。
bulk_router = APIRouter(tags=["Folders"])


# =============================================================================
# Schemas
# =============================================================================

class FolderCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    color: Optional[str] = Field(None, max_length=16)  # "#RRGGBB" 等
    icon: Optional[str] = Field(None, max_length=64)   # SF Symbol or emoji
    order: Optional[int] = None


class FolderUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    color: Optional[str] = Field(None, max_length=16)
    icon: Optional[str] = Field(None, max_length=64)
    order: Optional[int] = None


class FolderResponse(BaseModel):
    id: str
    name: str
    color: Optional[str] = None
    icon: Optional[str] = None
    order: int
    sessionCount: int = 0
    createdAt: datetime
    updatedAt: datetime


class FolderListResponse(BaseModel):
    folders: List[FolderResponse]


class MoveSessionRequest(BaseModel):
    folderId: Optional[str] = None  # null/省略 = root に戻す


class BulkImportItem(BaseModel):
    """ローカルだけに存在していた folder を server に取り込むための entry。

    iOS UserDefaults / Desktop localStorage に蓄積された folder を初回起動時に
    bulk POST する用途。
    """
    name: str = Field(..., min_length=1, max_length=80)
    color: Optional[str] = Field(None, max_length=16)
    icon: Optional[str] = Field(None, max_length=64)
    order: Optional[int] = None
    # 既存 session を folder に紐付けるための optional 配列。
    # 各 sessionId が caller の所有である場合のみ紐付け、所有外は silently skip。
    sessionIds: Optional[List[str]] = None
    # client が一意に発行した nonce (例: UUID)。重複呼び出しを idempotent にする。
    clientId: Optional[str] = Field(None, max_length=64)


class BulkImportRequest(BaseModel):
    items: List[BulkImportItem] = Field(default_factory=list)


class BulkImportResult(BaseModel):
    folderId: str
    name: str
    status: str  # "created" | "matched" | "updated" | "skipped"
    sessionsAssigned: int = 0


class BulkImportResponse(BaseModel):
    results: List[BulkImportResult]
    created: int = 0
    matched: int = 0
    sessionsAssigned: int = 0


# =============================================================================
# Helpers
# =============================================================================

def _folders_collection(uid: str):
    return db.collection("users").document(uid).collection("folders")


def _doc_to_response(doc_id: str, data: dict) -> FolderResponse:
    return FolderResponse(
        id=doc_id,
        name=data.get("name", ""),
        color=data.get("color"),
        icon=data.get("icon"),
        order=int(data.get("order", 0)),
        sessionCount=int(data.get("sessionCount", 0)),
        createdAt=data.get("createdAt") or datetime.now(timezone.utc),
        updatedAt=data.get("updatedAt") or datetime.now(timezone.utc),
    )


def _next_order(uid: str) -> int:
    docs = list(_folders_collection(uid).order_by("order", direction=firestore.Query.DESCENDING).limit(1).stream())
    if not docs:
        return 0
    last = docs[0].to_dict() or {}
    return int(last.get("order", 0)) + 1


def _count_sessions_in_folder(uid: str, folder_id: str) -> int:
    q = (
        db.collection("sessions")
        .where(filter=FieldFilter("ownerUserId", "==", uid))
        .where(filter=FieldFilter("folderId", "==", folder_id))
    )
    try:
        # Aggregation count (Firestore Python SDK >= 2.11)
        return int(q.count().get()[0][0].value)
    except Exception:
        # Fallback (slow): stream and len
        return sum(1 for _ in q.stream())


# =============================================================================
# CRUD
# =============================================================================

def _list_folders_impl(uid: str) -> FolderListResponse:
    docs = list(_folders_collection(uid).order_by("order").stream())
    items: List[FolderResponse] = []
    for d in docs:
        data = d.to_dict() or {}
        items.append(_doc_to_response(d.id, data))
    return FolderListResponse(folders=items)


def _create_folder_impl(uid: str, body: FolderCreateRequest) -> FolderResponse:
    folder_id = uuid.uuid4().hex[:16]
    now = datetime.now(timezone.utc)
    order = body.order if body.order is not None else _next_order(uid)
    data = {
        "name": body.name,
        "color": body.color,
        "icon": body.icon,
        "order": int(order),
        "sessionCount": 0,
        "createdAt": now,
        "updatedAt": now,
    }
    _folders_collection(uid).document(folder_id).set(data)
    return _doc_to_response(folder_id, data)


def _get_folder_impl(uid: str, folder_id: str) -> FolderResponse:
    doc = _folders_collection(uid).document(folder_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Folder not found")
    return _doc_to_response(doc.id, doc.to_dict() or {})


def _update_folder_impl(uid: str, folder_id: str, body: FolderUpdateRequest) -> FolderResponse:
    ref = _folders_collection(uid).document(folder_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Folder not found")
    patch = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not patch:
        return _doc_to_response(snap.id, snap.to_dict() or {})
    patch["updatedAt"] = datetime.now(timezone.utc)
    ref.update(patch)
    new_snap = ref.get()
    return _doc_to_response(new_snap.id, new_snap.to_dict() or {})


def _delete_folder_impl(uid: str, folder_id: str) -> None:
    ref = _folders_collection(uid).document(folder_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Folder not found")

    # 含まれる sessions の folderId を null に戻す（cascade ではなく detach）
    sess_q = (
        db.collection("sessions")
        .where(filter=FieldFilter("ownerUserId", "==", uid))
        .where(filter=FieldFilter("folderId", "==", folder_id))
    )
    batch = db.batch()
    n = 0
    for s in sess_q.stream():
        batch.update(s.reference, {"folderId": None, "updatedAt": datetime.now(timezone.utc)})
        n += 1
        if n % 400 == 0:
            batch.commit()
            batch = db.batch()
    if n % 400 != 0:
        batch.commit()
    ref.delete()
    logger.info(f"deleted folder {folder_id} for uid={uid}, detached {n} sessions")


def _list_sessions_in_folder_impl(uid: str, folder_id: str) -> dict:
    # 折角の正本化なので、SessionResponse の重複定義を避けるため
    # 既存 sessions.py の helper はそのまま使わず、軽量な dict を返す。
    # クライアントは /v1/sessions に再 fetch して詳細を補完してよい。
    snap = _folders_collection(uid).document(folder_id).get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Folder not found")

    q = (
        db.collection("sessions")
        .where(filter=FieldFilter("ownerUserId", "==", uid))
        .where(filter=FieldFilter("folderId", "==", folder_id))
    )
    docs = list(q.stream())
    items = []
    for d in docs:
        data = d.to_dict() or {}
        items.append({
            "id": d.id,
            "title": data.get("title"),
            "createdAt": data.get("createdAt"),
            "updatedAt": data.get("updatedAt"),
            "folderId": data.get("folderId"),
            "status": data.get("status"),
        })
    # createdAt 降順（既存 list_sessions と同じ）
    items.sort(key=lambda x: x.get("createdAt") or datetime.min, reverse=True)
    return {"sessions": items}


def _move_session_impl(uid: str, session_id: str, folder_id: Optional[str]) -> dict:
    sess_ref = db.collection("sessions").document(session_id)
    snap = sess_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Session not found")
    data = snap.to_dict() or {}
    if data.get("ownerUserId") != uid:
        raise HTTPException(status_code=403, detail="Not the session owner")

    if folder_id is not None:
        f_snap = _folders_collection(uid).document(folder_id).get()
        if not f_snap.exists:
            raise HTTPException(status_code=404, detail="Folder not found")

    old_folder = data.get("folderId")
    if old_folder == folder_id:
        return {"ok": True, "sessionId": session_id, "folderId": folder_id}

    now = datetime.now(timezone.utc)
    sess_ref.update({"folderId": folder_id, "updatedAt": now})

    # sessionCount 更新（ベストエフォート、Firestore Increment で eventual consistency）
    if old_folder:
        try:
            _folders_collection(uid).document(old_folder).update({
                "sessionCount": firestore.Increment(-1),
                "updatedAt": now,
            })
        except Exception as e:
            logger.warning(f"old folder sessionCount decrement failed: {e}")
    if folder_id:
        try:
            _folders_collection(uid).document(folder_id).update({
                "sessionCount": firestore.Increment(1),
                "updatedAt": now,
            })
        except Exception as e:
            logger.warning(f"new folder sessionCount increment failed: {e}")

    return {"ok": True, "sessionId": session_id, "folderId": folder_id}


# =============================================================================
# Canonical routes (/v1/folders)
# =============================================================================

@router.get("", response_model=FolderListResponse)
def list_folders(current_user: CurrentUser = Depends(get_current_user)):
    return _list_folders_impl(current_user.uid)


@router.post("", response_model=FolderResponse, status_code=201)
def create_folder(body: FolderCreateRequest, current_user: CurrentUser = Depends(get_current_user)):
    return _create_folder_impl(current_user.uid, body)


@router.get("/{folder_id}", response_model=FolderResponse)
def get_folder(folder_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return _get_folder_impl(current_user.uid, folder_id)


@router.patch("/{folder_id}", response_model=FolderResponse)
def update_folder(folder_id: str, body: FolderUpdateRequest, current_user: CurrentUser = Depends(get_current_user)):
    return _update_folder_impl(current_user.uid, folder_id, body)


@router.delete("/{folder_id}", status_code=204)
def delete_folder(folder_id: str, current_user: CurrentUser = Depends(get_current_user)):
    _delete_folder_impl(current_user.uid, folder_id)
    return None


@router.get("/{folder_id}/sessions")
def list_sessions_in_folder(folder_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return _list_sessions_in_folder_impl(current_user.uid, folder_id)


# =============================================================================
# Legacy alias (/folders) — iOS/Desktop 既存呼び出しの互換
# =============================================================================

@legacy_router.get("", response_model=FolderListResponse)
def list_folders_legacy(current_user: CurrentUser = Depends(get_current_user)):
    return _list_folders_impl(current_user.uid)


@legacy_router.post("", response_model=FolderResponse, status_code=201)
def create_folder_legacy(body: FolderCreateRequest, current_user: CurrentUser = Depends(get_current_user)):
    return _create_folder_impl(current_user.uid, body)


@legacy_router.get("/{folder_id}", response_model=FolderResponse)
def get_folder_legacy(folder_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return _get_folder_impl(current_user.uid, folder_id)


@legacy_router.patch("/{folder_id}", response_model=FolderResponse)
def update_folder_legacy(folder_id: str, body: FolderUpdateRequest, current_user: CurrentUser = Depends(get_current_user)):
    return _update_folder_impl(current_user.uid, folder_id, body)


@legacy_router.delete("/{folder_id}", status_code=204)
def delete_folder_legacy(folder_id: str, current_user: CurrentUser = Depends(get_current_user)):
    _delete_folder_impl(current_user.uid, folder_id)
    return None


@legacy_router.get("/{folder_id}/sessions")
def list_sessions_in_folder_legacy(folder_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return _list_sessions_in_folder_impl(current_user.uid, folder_id)


# =============================================================================
# Move endpoint — canonical /v1/sessions/{id}:move + legacy /sessions/{id}:move
# =============================================================================

# =============================================================================
# Bulk import (client-local → server migration)
# =============================================================================
# iOS UserDefaults / Desktop localStorage に蓄積された folder を初回起動時に取り込む。
# 既存の同名 folder は skip (matched)、新規だけ create する idempotent 設計。
# session の所有者外 sessionId は silently skip (security)。
MAX_BULK_ITEMS = 200


def _bulk_import_impl(uid: str, items: List[BulkImportItem]) -> BulkImportResponse:
    if len(items) > MAX_BULK_ITEMS:
        raise HTTPException(status_code=413, detail=f"Too many items (max {MAX_BULK_ITEMS})")

    # 既存 folder name → folderId の index を 1 回だけ作る
    existing = list(_folders_collection(uid).stream())
    name_to_id: dict[str, str] = {}
    for d in existing:
        data = d.to_dict() or {}
        n = (data.get("name") or "").strip()
        if n:
            name_to_id.setdefault(n, d.id)

    base_order = _next_order(uid)
    results: List[BulkImportResult] = []
    created = 0
    matched = 0
    sessions_assigned_total = 0
    now = datetime.now(timezone.utc)

    for i, item in enumerate(items):
        name = item.name.strip()
        if not name:
            continue

        if name in name_to_id:
            folder_id = name_to_id[name]
            status_label = "matched"
        else:
            folder_id = uuid.uuid4().hex[:16]
            order = item.order if item.order is not None else (base_order + i)
            data = {
                "name": name,
                "color": item.color,
                "icon": item.icon,
                "order": int(order),
                "sessionCount": 0,
                "createdAt": now,
                "updatedAt": now,
                "importedFromClient": True,
                "clientId": item.clientId,
            }
            _folders_collection(uid).document(folder_id).set(data)
            name_to_id[name] = folder_id
            status_label = "created"
            created += 1

        sessions_assigned = 0
        if item.sessionIds:
            for sess_id in item.sessionIds[:500]:  # safety cap
                try:
                    sess_ref = db.collection("sessions").document(sess_id)
                    snap = sess_ref.get()
                    if not snap.exists:
                        continue
                    sdata = snap.to_dict() or {}
                    if sdata.get("ownerUserId") != uid:
                        continue  # 所有者外は silently skip
                    if sdata.get("folderId") == folder_id:
                        continue
                    sess_ref.update({"folderId": folder_id, "updatedAt": now})
                    sessions_assigned += 1
                except Exception as e:
                    logger.warning(f"bulkImport: failed to assign session {sess_id} to folder {folder_id}: {e}")

        if sessions_assigned > 0:
            try:
                _folders_collection(uid).document(folder_id).update({
                    "sessionCount": firestore.Increment(sessions_assigned),
                    "updatedAt": now,
                })
            except Exception as e:
                logger.warning(f"bulkImport: sessionCount update failed for {folder_id}: {e}")

        if status_label == "matched":
            matched += 1
        sessions_assigned_total += sessions_assigned
        results.append(BulkImportResult(
            folderId=folder_id,
            name=name,
            status=status_label,
            sessionsAssigned=sessions_assigned,
        ))

    return BulkImportResponse(
        results=results,
        created=created,
        matched=matched,
        sessionsAssigned=sessions_assigned_total,
    )


@bulk_router.post("/v1/folders:bulkImport", response_model=BulkImportResponse)
def bulk_import_folders(body: BulkImportRequest, current_user: CurrentUser = Depends(get_current_user)):
    return _bulk_import_impl(current_user.uid, body.items)


@bulk_router.post("/folders:bulkImport", response_model=BulkImportResponse, include_in_schema=False)
def bulk_import_folders_legacy(body: BulkImportRequest, current_user: CurrentUser = Depends(get_current_user)):
    return _bulk_import_impl(current_user.uid, body.items)


# =============================================================================
# Move endpoints (canonical /v1/sessions/{id}:move + legacy /sessions/{id}:move)
# =============================================================================

@move_router.post("/v1/sessions/{session_id}:move")
def move_session(session_id: str, body: MoveSessionRequest, current_user: CurrentUser = Depends(get_current_user)):
    return _move_session_impl(current_user.uid, session_id, body.folderId)


@move_router.post("/sessions/{session_id}:move", include_in_schema=False)
def move_session_legacy(session_id: str, body: MoveSessionRequest, current_user: CurrentUser = Depends(get_current_user)):
    return _move_session_impl(current_user.uid, session_id, body.folderId)
