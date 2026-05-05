"""
folders.py — Folders / Library organisation (schema-compat with 5/1 build)

Stable canonical: /v1/folders + legacy /folders + /v1/sessions/{id}:move
Storage:
- folder 本体    : users/{uid}/folders/{folderId}
                   fields: name, color, description, isArchived, deletedAt, createdAt, updatedAt
- folder ID 形式 : `fld_<16-hex>` (旧 backend と同形式)
- session 紐付け : users/{uid}/sessionMeta/{sessionId}.folderId
                   (NOT sessions/{id}.folderId — 旧 schema を尊重)
- soft delete    : deletedAt をセット (hard delete しない)

See: deepnote-contracts/api/endpoints-map.md (V-017/V-018, V-029, V-034)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel, Field

from app.firebase import db
from app.dependencies import get_current_user, CurrentUser

logger = logging.getLogger("app.folders")

router = APIRouter(prefix="/v1/folders", tags=["Folders"])
legacy_router = APIRouter(prefix="/folders", tags=["Folders"], include_in_schema=False)
move_router = APIRouter(tags=["Folders"])
bulk_router = APIRouter(tags=["Folders"])  # /v1/folders:bulkImport


# =============================================================================
# Schemas (旧 backend 互換: name/color/description/isArchived/deletedAt)
# =============================================================================

class FolderCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    color: Optional[str] = Field(None, max_length=16)
    description: Optional[str] = Field(None, max_length=500)


class FolderUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    color: Optional[str] = Field(None, max_length=16)
    description: Optional[str] = Field(None, max_length=500)
    isArchived: Optional[bool] = None


class FolderResponse(BaseModel):
    id: str
    name: str
    color: Optional[str] = None
    description: Optional[str] = None
    isArchived: bool = False
    deletedAt: Optional[datetime] = None
    createdAt: Optional[datetime] = None
    updatedAt: Optional[datetime] = None
    sessionCount: int = 0


class FolderListResponse(BaseModel):
    folders: List[FolderResponse]


class MoveSessionRequest(BaseModel):
    folderId: Optional[str] = None  # null/省略 = root に戻す


class BulkImportItem(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    color: Optional[str] = Field(None, max_length=16)
    description: Optional[str] = Field(None, max_length=500)
    sessionIds: Optional[List[str]] = None
    clientId: Optional[str] = Field(None, max_length=64)


class BulkImportRequest(BaseModel):
    items: List[BulkImportItem] = Field(default_factory=list)


class BulkImportResult(BaseModel):
    folderId: str
    name: str
    status: str
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


def _session_meta_collection(uid: str):
    return db.collection("users").document(uid).collection("sessionMeta")


def _new_folder_id() -> str:
    return f"fld_{uuid.uuid4().hex[:16]}"


def _doc_to_response(doc_id: str, data: dict) -> FolderResponse:
    return FolderResponse(
        id=doc_id,
        name=data.get("name", ""),
        color=data.get("color"),
        description=data.get("description"),
        isArchived=bool(data.get("isArchived", False)),
        deletedAt=data.get("deletedAt"),
        createdAt=data.get("createdAt"),
        updatedAt=data.get("updatedAt"),
        sessionCount=int(data.get("sessionCount", 0)),
    )


def _count_sessions_for_folder(uid: str, folder_id: str) -> int:
    """sessionMeta から folder_id に紐付く session 数を集計 (deletedAt が null のもの)."""
    q = _session_meta_collection(uid).where(
        filter=FieldFilter("folderId", "==", folder_id)
    )
    try:
        # Aggregation count
        return int(q.count().get()[0][0].value)
    except Exception:
        return sum(1 for _ in q.stream())


# =============================================================================
# Implementations
# =============================================================================

def _list_folders_impl(uid: str) -> FolderListResponse:
    """deletedAt が null の folder を全件返す。sessionCount は実測。"""
    docs = list(_folders_collection(uid).stream())
    items: List[FolderResponse] = []
    for d in docs:
        data = d.to_dict() or {}
        if data.get("deletedAt"):
            continue
        # sessionCount を実測 (旧 backend 同様)
        try:
            data["sessionCount"] = _count_sessions_for_folder(uid, d.id)
        except Exception as e:
            logger.warning(f"sessionCount fallback for {d.id}: {e}")
            data["sessionCount"] = 0
        items.append(_doc_to_response(d.id, data))
    # createdAt 降順
    items.sort(key=lambda x: x.createdAt or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return FolderListResponse(folders=items)


def _create_folder_impl(uid: str, body: FolderCreateRequest) -> FolderResponse:
    folder_id = _new_folder_id()
    now = datetime.now(timezone.utc)
    data = {
        "name": body.name,
        "color": body.color,
        "description": body.description,
        "isArchived": False,
        "deletedAt": None,
        "createdAt": now,
        "updatedAt": now,
    }
    _folders_collection(uid).document(folder_id).set(data)
    return _doc_to_response(folder_id, {**data, "sessionCount": 0})


def _get_folder_impl(uid: str, folder_id: str) -> FolderResponse:
    doc = _folders_collection(uid).document(folder_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Folder not found")
    data = doc.to_dict() or {}
    if data.get("deletedAt"):
        raise HTTPException(status_code=404, detail="Folder not found")
    data["sessionCount"] = _count_sessions_for_folder(uid, doc.id)
    return _doc_to_response(doc.id, data)


def _update_folder_impl(uid: str, folder_id: str, body: FolderUpdateRequest) -> FolderResponse:
    ref = _folders_collection(uid).document(folder_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Folder not found")
    cur = snap.to_dict() or {}
    if cur.get("deletedAt"):
        raise HTTPException(status_code=404, detail="Folder not found")
    patch = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not patch:
        cur["sessionCount"] = _count_sessions_for_folder(uid, folder_id)
        return _doc_to_response(folder_id, cur)
    patch["updatedAt"] = datetime.now(timezone.utc)
    ref.update(patch)
    new_snap = ref.get()
    new_data = new_snap.to_dict() or {}
    new_data["sessionCount"] = _count_sessions_for_folder(uid, folder_id)
    return _doc_to_response(new_snap.id, new_data)


def _delete_folder_impl(uid: str, folder_id: str) -> None:
    """Soft delete: folder doc に deletedAt をセット。
    含まれる session の sessionMeta.folderId を null に detach。"""
    ref = _folders_collection(uid).document(folder_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Folder not found")
    cur = snap.to_dict() or {}
    if cur.get("deletedAt"):
        return  # 既に削除済 (idempotent)
    now = datetime.now(timezone.utc)

    # 紐付く sessionMeta の folderId を null にする
    meta_q = _session_meta_collection(uid).where(
        filter=FieldFilter("folderId", "==", folder_id)
    )
    batch = db.batch()
    n = 0
    for s in meta_q.stream():
        batch.update(s.reference, {
            "folderId": None,
            "organizationUpdatedAt": now,
            "updatedAt": now,
        })
        n += 1
        if n % 400 == 0:
            batch.commit()
            batch = db.batch()
    if n % 400 != 0:
        batch.commit()

    # folder soft delete
    ref.update({
        "deletedAt": now,
        "updatedAt": now,
    })
    logger.info(f"soft-deleted folder {folder_id} for uid={uid}, detached {n} sessionMeta")


def _list_sessions_in_folder_impl(uid: str, folder_id: str) -> dict:
    """sessionMeta.folderId == folder_id の session_id を集めて、
    sessions/{id} doc から軽量フィールドを返す。"""
    snap = _folders_collection(uid).document(folder_id).get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Folder not found")
    if (snap.to_dict() or {}).get("deletedAt"):
        raise HTTPException(status_code=404, detail="Folder not found")

    meta_q = _session_meta_collection(uid).where(
        filter=FieldFilter("folderId", "==", folder_id)
    )
    session_ids: List[str] = []
    meta_index: dict[str, dict] = {}
    for m in meta_q.stream():
        d = m.to_dict() or {}
        sid = d.get("sessionId") or m.id
        session_ids.append(sid)
        meta_index[sid] = d

    items = []
    # 500 件ずつ batch fetch
    for i in range(0, len(session_ids), 30):
        chunk = session_ids[i:i + 30]
        for sid in chunk:
            try:
                s_snap = db.collection("sessions").document(sid).get()
                if not s_snap.exists:
                    continue
                sdata = s_snap.to_dict() or {}
                if sdata.get("ownerUserId") != uid:
                    continue
                meta = meta_index.get(sid, {})
                items.append({
                    "id": s_snap.id,
                    "title": sdata.get("title"),
                    "createdAt": sdata.get("createdAt"),
                    "updatedAt": sdata.get("updatedAt"),
                    "folderId": meta.get("folderId"),
                    "status": sdata.get("status"),
                    "isPinned": meta.get("isPinned", False),
                    "lastOpenedAt": meta.get("lastOpenedAt"),
                })
            except Exception as e:
                logger.warning(f"failed to fetch session {sid} in folder {folder_id}: {e}")

    items.sort(key=lambda x: x.get("createdAt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return {"sessions": items}


def _move_session_impl(uid: str, session_id: str, folder_id: Optional[str]) -> dict:
    """session ↔ folder 紐付けを users/{uid}/sessionMeta/{session_id}.folderId に書く。
    旧 schema 互換 — sessions/{id}.folderId は触らない。"""
    sess_ref = db.collection("sessions").document(session_id)
    snap = sess_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Session not found")
    sdata = snap.to_dict() or {}
    if sdata.get("ownerUserId") != uid:
        raise HTTPException(status_code=403, detail="Not the session owner")

    if folder_id is not None:
        f_snap = _folders_collection(uid).document(folder_id).get()
        if not f_snap.exists or (f_snap.to_dict() or {}).get("deletedAt"):
            raise HTTPException(status_code=404, detail="Folder not found")

    now = datetime.now(timezone.utc)
    meta_ref = _session_meta_collection(uid).document(session_id)
    meta_snap = meta_ref.get()
    if not meta_snap.exists:
        # sessionMeta が無い場合は作成 (旧 schema fields も埋める)
        meta_ref.set({
            "sessionId": session_id,
            "folderId": folder_id,
            "isPinned": False,
            "isArchived": False,
            "role": "OWNER",
            "createdAt": now,
            "updatedAt": now,
            "organizationUpdatedAt": now,
            "lastOpenedAt": None,
        })
    else:
        old_folder = (meta_snap.to_dict() or {}).get("folderId")
        if old_folder == folder_id:
            return {"ok": True, "sessionId": session_id, "folderId": folder_id}
        meta_ref.update({
            "folderId": folder_id,
            "updatedAt": now,
            "organizationUpdatedAt": now,
        })

    return {"ok": True, "sessionId": session_id, "folderId": folder_id}


# =============================================================================
# Bulk import (client-local → server migration)
# =============================================================================

MAX_BULK_ITEMS = 200


def _bulk_import_impl(uid: str, items: List[BulkImportItem]) -> BulkImportResponse:
    if len(items) > MAX_BULK_ITEMS:
        raise HTTPException(status_code=413, detail=f"Too many items (max {MAX_BULK_ITEMS})")

    # 既存 folder name → folderId index (deletedAt が null のみ)
    existing = list(_folders_collection(uid).stream())
    name_to_id: dict[str, str] = {}
    for d in existing:
        data = d.to_dict() or {}
        if data.get("deletedAt"):
            continue
        n = (data.get("name") or "").strip()
        if n:
            name_to_id.setdefault(n, d.id)

    results: List[BulkImportResult] = []
    created = 0
    matched = 0
    sessions_assigned_total = 0
    now = datetime.now(timezone.utc)

    for item in items:
        name = item.name.strip()
        if not name:
            continue

        if name in name_to_id:
            folder_id = name_to_id[name]
            status_label = "matched"
        else:
            folder_id = _new_folder_id()
            data = {
                "name": name,
                "color": item.color,
                "description": item.description,
                "isArchived": False,
                "deletedAt": None,
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
            for sess_id in item.sessionIds[:500]:
                try:
                    sess_ref = db.collection("sessions").document(sess_id)
                    s_snap = sess_ref.get()
                    if not s_snap.exists:
                        continue
                    sdata = s_snap.to_dict() or {}
                    if sdata.get("ownerUserId") != uid:
                        continue
                    meta_ref = _session_meta_collection(uid).document(sess_id)
                    meta_snap = meta_ref.get()
                    if meta_snap.exists:
                        old_folder = (meta_snap.to_dict() or {}).get("folderId")
                        if old_folder == folder_id:
                            continue
                        meta_ref.update({
                            "folderId": folder_id,
                            "updatedAt": now,
                            "organizationUpdatedAt": now,
                        })
                    else:
                        meta_ref.set({
                            "sessionId": sess_id,
                            "folderId": folder_id,
                            "isPinned": False,
                            "isArchived": False,
                            "role": "OWNER",
                            "createdAt": now,
                            "updatedAt": now,
                            "organizationUpdatedAt": now,
                            "lastOpenedAt": None,
                        })
                    sessions_assigned += 1
                except Exception as e:
                    logger.warning(f"bulkImport: failed to assign session {sess_id} to folder {folder_id}: {e}")

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


# =============================================================================
# Routes
# =============================================================================

# /v1/folders (canonical)

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


# /folders (legacy alias) — 出荷済 client が呼ぶので維持

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


# Bulk import — colon-RPC, dedicated router (prefix の slash 結合を回避)

@bulk_router.post("/v1/folders:bulkImport", response_model=BulkImportResponse)
def bulk_import_folders(body: BulkImportRequest, current_user: CurrentUser = Depends(get_current_user)):
    return _bulk_import_impl(current_user.uid, body.items)


@bulk_router.post("/folders:bulkImport", response_model=BulkImportResponse, include_in_schema=False)
def bulk_import_folders_legacy(body: BulkImportRequest, current_user: CurrentUser = Depends(get_current_user)):
    return _bulk_import_impl(current_user.uid, body.items)


# Move endpoint — canonical /v1/sessions/{id}:move + legacy alias

@move_router.post("/v1/sessions/{session_id}:move")
def move_session(session_id: str, body: MoveSessionRequest, current_user: CurrentUser = Depends(get_current_user)):
    return _move_session_impl(current_user.uid, session_id, body.folderId)


@move_router.post("/sessions/{session_id}:move", include_in_schema=False)
def move_session_legacy(session_id: str, body: MoveSessionRequest, current_user: CurrentUser = Depends(get_current_user)):
    return _move_session_impl(current_user.uid, session_id, body.folderId)
