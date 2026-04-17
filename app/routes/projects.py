from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import CurrentUser, get_current_user, ensure_can_view
from app.routes.sessions import _resolve_session
from app.services import project_folders


logger = logging.getLogger(__name__)

router = APIRouter(tags=["Folders"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize_doc(doc_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    out = {"id": doc_id}
    out.update(data)
    return out


class FolderCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    color: Optional[str] = Field(None, max_length=32)
    description: Optional[str] = Field(None, max_length=500)


class FolderUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    color: Optional[str] = Field(None, max_length=32)
    description: Optional[str] = Field(None, max_length=500)
    isArchived: Optional[bool] = None


class SessionOrganizationRequest(BaseModel):
    folderId: Optional[str] = None


# ────────────────────────────────────────────────────────────
# Folders CRUD
# ────────────────────────────────────────────────────────────

@router.post("/folders")
async def create_folder(
    body: FolderCreateRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    now = _now()
    folder_id = f"fld_{uuid.uuid4().hex[:16]}"
    payload = {
        "name": body.name,
        "color": body.color,
        "description": body.description,
        "isArchived": False,
        "createdAt": now,
        "updatedAt": now,
        "deletedAt": None,
    }
    project_folders.folder_ref(current_user.uid, folder_id).set(payload)
    return _serialize_doc(folder_id, payload)


@router.get("/folders")
async def list_folders(
    current_user: CurrentUser = Depends(get_current_user),
):
    # Folders are stored under users/{uid}/folders/* but a single account can
    # contain multiple member uids (iOS + Desktop + LINE etc.). Union across
    # every member uid so folders created on one device are visible from the
    # others, deduplicated by folder_id.
    rows = []
    for owner_uid, folder_id, data in project_folders.iter_account_folders(
        current_user.uid, current_user.account_id
    ):
        serialized = _serialize_doc(folder_id, data)
        # Count sessions in the folder subcollection under the uid that
        # actually stores the folder doc.
        session_refs = list(
            project_folders.folder_ref(owner_uid, folder_id)
            .collection("sessions")
            .stream()
        )
        serialized["sessionCount"] = len(session_refs)
        rows.append(serialized)
    rows.sort(key=lambda x: x.get("createdAt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return rows


@router.patch("/folders/{folder_id}")
async def update_folder(
    folder_id: str,
    body: FolderUpdateRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    owner_uid = project_folders.find_folder_owner_uid(
        current_user.uid, current_user.account_id, folder_id
    )
    if not owner_uid:
        raise HTTPException(status_code=404, detail="Folder not found")
    ref = project_folders.folder_ref(owner_uid, folder_id)
    snap = ref.get()
    if not snap.exists or (snap.to_dict() or {}).get("deletedAt"):
        raise HTTPException(status_code=404, detail="Folder not found")
    patch = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    patch["updatedAt"] = _now()
    ref.set(patch, merge=True)
    data = snap.to_dict() or {}
    data.update(patch)
    return _serialize_doc(folder_id, data)


@router.delete("/folders/{folder_id}")
async def delete_folder(
    folder_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    owner_uid = project_folders.find_folder_owner_uid(
        current_user.uid, current_user.account_id, folder_id
    )
    if not owner_uid:
        raise HTTPException(status_code=404, detail="Folder not found")
    ref = project_folders.folder_ref(owner_uid, folder_id)
    snap = ref.get()
    if not snap.exists or (snap.to_dict() or {}).get("deletedAt"):
        raise HTTPException(status_code=404, detail="Folder not found")
    ref.set({"deletedAt": _now(), "updatedAt": _now(), "isArchived": True}, merge=True)
    return {"ok": True, "folderId": folder_id}


@router.get("/folders/{folder_id}/sessions")
async def list_folder_sessions(
    folder_id: str,
    limit: int = 100,
    current_user: CurrentUser = Depends(get_current_user),
):
    owner_uid = project_folders.find_folder_owner_uid(
        current_user.uid, current_user.account_id, folder_id
    )
    if not owner_uid:
        raise HTTPException(status_code=404, detail="Folder not found")
    coll = project_folders.folder_ref(owner_uid, folder_id).collection("sessions")
    docs = list(coll.stream())
    rows = []
    for doc in docs[:limit]:
        data = doc.to_dict() or {}
        rows.append(_serialize_doc(doc.id, data))
    rows.sort(key=lambda x: x.get("updatedAt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return rows


# ────────────────────────────────────────────────────────────
# Session organization
# ────────────────────────────────────────────────────────────

@router.put("/sessions/{session_id}/organization")
async def set_session_organization(
    session_id: str,
    body: SessionOrganizationRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    _, snapshot, resolved_id = _resolve_session(session_id, current_user.uid, current_user.account_id)
    session_data = snapshot.to_dict() or {}
    ensure_can_view(session_data, current_user, resolved_id)

    try:
        result = project_folders.set_session_organization(
            uid=current_user.uid,
            session_id=resolved_id,
            folder_id=body.folderId,
            session_data=session_data,
            finalized=(session_data.get("status") == "final"),
            account_id=current_user.account_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return {"ok": True, **result}


@router.get("/sessions/{session_id}/organization")
async def get_session_organization(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    _, snapshot, resolved_id = _resolve_session(session_id, current_user.uid, current_user.account_id)
    session_data = snapshot.to_dict() or {}
    ensure_can_view(session_data, current_user, resolved_id)

    # sessionMeta may have been written under any member uid of the account
    # (e.g. the session was created on iOS while this request came from Desktop).
    meta_owner_uid = project_folders.find_session_meta_uid(
        current_user.uid, current_user.account_id, resolved_id
    )
    if not meta_owner_uid:
        return {"sessionId": resolved_id, "folderId": None}
    meta_snap = project_folders.session_meta_ref(meta_owner_uid, resolved_id).get()
    if not meta_snap.exists:
        return {"sessionId": resolved_id, "folderId": None}
    meta = meta_snap.to_dict() or {}
    return {
        "sessionId": resolved_id,
        "folderId": meta.get("folderId"),
        "organizedAt": meta.get("organizedAt"),
        "organizationStatus": meta.get("organizationStatus"),
        "updatedAt": meta.get("organizationUpdatedAt") or meta.get("updatedAt"),
    }


# ────────────────────────────────────────────────────────────
# Deprecated legacy /projects* routes → 410 Gone
# ────────────────────────────────────────────────────────────

_GONE_DETAIL = {
    "error": "endpoint_removed",
    "message": "Project layer has been removed. Use /folders and /folders/{id}/sessions instead.",
    "migration": {
        "POST /projects": None,
        "GET /projects": "GET /folders",
        "POST /projects/{pid}/folders": "POST /folders",
        "GET /projects/{pid}/folders": "GET /folders",
        "PATCH /projects/{pid}/folders/{fid}": "PATCH /folders/{fid}",
        "DELETE /projects/{pid}/folders/{fid}": "DELETE /folders/{fid}",
        "GET /projects/{pid}/sessions": "GET /folders/{fid}/sessions",
    },
}


def _gone(path: str, uid: Optional[str] = None):
    logger.warning(f"[DEPRECATED 410] {path} called by uid={uid}")
    raise HTTPException(status_code=410, detail=_GONE_DETAIL)


@router.post("/projects", include_in_schema=False, deprecated=True)
async def _gone_create_project(current_user: CurrentUser = Depends(get_current_user)):
    _gone("POST /projects", current_user.uid)


@router.get("/projects", include_in_schema=False, deprecated=True)
async def _gone_list_projects(current_user: CurrentUser = Depends(get_current_user)):
    _gone("GET /projects", current_user.uid)


@router.patch("/projects/{project_id}", include_in_schema=False, deprecated=True)
async def _gone_patch_project(project_id: str, current_user: CurrentUser = Depends(get_current_user)):
    _gone("PATCH /projects/{id}", current_user.uid)


@router.delete("/projects/{project_id}", include_in_schema=False, deprecated=True)
async def _gone_delete_project(project_id: str, current_user: CurrentUser = Depends(get_current_user)):
    _gone("DELETE /projects/{id}", current_user.uid)


@router.post("/projects/{project_id}/folders", include_in_schema=False, deprecated=True)
async def _gone_create_project_folder(project_id: str, current_user: CurrentUser = Depends(get_current_user)):
    _gone("POST /projects/{id}/folders", current_user.uid)


@router.get("/projects/{project_id}/folders", include_in_schema=False, deprecated=True)
async def _gone_list_project_folders(project_id: str, current_user: CurrentUser = Depends(get_current_user)):
    _gone("GET /projects/{id}/folders", current_user.uid)


@router.patch("/projects/{project_id}/folders/{folder_id}", include_in_schema=False, deprecated=True)
async def _gone_patch_project_folder(project_id: str, folder_id: str, current_user: CurrentUser = Depends(get_current_user)):
    _gone("PATCH /projects/{id}/folders/{id}", current_user.uid)


@router.delete("/projects/{project_id}/folders/{folder_id}", include_in_schema=False, deprecated=True)
async def _gone_delete_project_folder(project_id: str, folder_id: str, current_user: CurrentUser = Depends(get_current_user)):
    _gone("DELETE /projects/{id}/folders/{id}", current_user.uid)


@router.get("/projects/{project_id}/sessions", include_in_schema=False, deprecated=True)
async def _gone_list_project_sessions(project_id: str, current_user: CurrentUser = Depends(get_current_user)):
    _gone("GET /projects/{id}/sessions", current_user.uid)
