"""
todos.py - TODO Management API

Endpoints for managing TODOs extracted from meeting notes and transcripts.

Features:
- CRUD operations for TODOs
- Candidate review (accept/reject)
- Date-based listing for calendar UI
- Drag-and-drop date movement
- Session source linking
"""

import logging
import hashlib
from datetime import datetime, timezone, date, timedelta
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.firebase import db
from app.dependencies import get_current_user, CurrentUser
from app.util_models import (
    TodoStatus,
    TodoPriority,
    TodoCandidateStatus,
    TodoSourceType,
    TodoEvidence,
    TodoSource,
    TodoOrigin,
    TodoDedupe,
    TodoCreateRequest,
    TodoUpdateRequest,
    TodoMoveRequest,
    TodoResponse,
    TodoListResponse,
    TodoCandidateResponse,
    TodoCandidateListResponse,
    TodoExtractRequest,
    TodoExtractResponse,
    TodoAcceptRequest,
    TodoStatsResponse,
)

logger = logging.getLogger("app.todos")
router = APIRouter(prefix="/todos", tags=["TODOs"])


# =============================================================================
# Helper Functions
# =============================================================================

def _todo_doc_to_response(doc_id: str, data: dict) -> TodoResponse:
    """Convert Firestore document to TodoResponse."""
    source_data = data.get("source")
    source = None
    if source_data:
        evidence_data = source_data.get("evidence")
        evidence = None
        if evidence_data:
            evidence = TodoEvidence(
                quote=evidence_data.get("quote"),
                time_sec=evidence_data.get("timeSec"),
            )
        source = TodoSource(
            session_id=source_data.get("sessionId", ""),
            session_title=source_data.get("sessionTitle", ""),
            created_from=source_data.get("createdFrom", TodoSourceType.MANUAL),
            evidence=evidence,
        )

    origin_data = data.get("origin")
    origin = None
    if origin_data:
        origin = TodoOrigin(
            extractor_version=origin_data.get("extractorVersion", "todo_v1"),
            confidence=origin_data.get("confidence", 0.5),
            auto_created=origin_data.get("autoCreated", False),
            user_edited=origin_data.get("userEdited", False),
            user_moved=origin_data.get("userMoved", False),
        )

    created_at = data.get("createdAt")
    updated_at = data.get("updatedAt")
    if isinstance(created_at, datetime):
        pass
    elif created_at:
        created_at = datetime.now(timezone.utc)
    else:
        created_at = datetime.now(timezone.utc)

    if isinstance(updated_at, datetime):
        pass
    elif updated_at:
        updated_at = datetime.now(timezone.utc)
    else:
        updated_at = created_at

    return TodoResponse(
        id=doc_id,
        account_id=data.get("accountId", ""),
        title=data.get("title", ""),
        notes=data.get("notes"),
        due_date=data.get("dueDate"),
        status=data.get("status", TodoStatus.OPEN),
        priority=data.get("priority", TodoPriority.NORMAL),
        source=source,
        origin=origin,
        created_at=created_at,
        updated_at=updated_at,
    )


def _candidate_doc_to_response(doc_id: str, data: dict) -> TodoCandidateResponse:
    """Convert Firestore document to TodoCandidateResponse."""
    evidence_data = data.get("evidence")
    evidence = None
    if evidence_data:
        evidence = TodoEvidence(
            quote=evidence_data.get("quote"),
            time_sec=evidence_data.get("timeSec"),
        )

    created_at = data.get("createdAt")
    if not isinstance(created_at, datetime):
        created_at = datetime.now(timezone.utc)

    return TodoCandidateResponse(
        id=doc_id,
        account_id=data.get("accountId", ""),
        session_id=data.get("sessionId", ""),
        session_title=data.get("sessionTitle", ""),
        title=data.get("title", ""),
        due_date_proposed=data.get("dueDateProposed"),
        confidence=data.get("confidence", 0.5),
        reason=data.get("reason"),
        evidence=evidence,
        status=data.get("status", TodoCandidateStatus.PENDING),
        created_at=created_at,
    )


def _generate_semantic_key(title: str, session_id: str) -> str:
    """Generate a semantic key for deduplication."""
    # Simple hash-based key (can be replaced with embedding-based similarity)
    normalized = title.lower().strip()
    content = f"{session_id}:{normalized}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


# =============================================================================
# GET /todos - List TODOs
# =============================================================================

@router.get("", response_model=TodoListResponse)
async def list_todos(
    current_user: CurrentUser = Depends(get_current_user),
    from_date: Optional[str] = Query(None, alias="from", description="Start date (YYYY-MM-DD)"),
    to_date: Optional[str] = Query(None, alias="to", description="End date (YYYY-MM-DD)"),
    status: Optional[str] = Query("open", description="Filter by status: open, done, all"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """
    List TODOs for the current user's account.

    - Default: open TODOs from 90 days ago to 180 days ahead
    - Supports date range filtering for calendar UI
    - Sorted by dueDate (nulls last)
    """
    account_id = current_user.account_id

    # Fetch all TODOs for account (avoids compound index requirement)
    # Filter status/date in Python for flexibility
    query = db.collection("todos").where(
        filter=FieldFilter("accountId", "==", account_id)
    )

    docs = list(query.stream())

    # Filter in Python to avoid compound index issues
    filtered_docs = []
    for doc in docs:
        data = doc.to_dict()

        # Status filter
        if status and status != "all":
            if data.get("status") != status:
                continue

        # Date range filter
        due = data.get("dueDate")
        if from_date and due and due < from_date:
            continue
        if to_date and due and due > to_date:
            continue

        filtered_docs.append((doc, data))

    # Sort by dueDate (nulls last)
    def sort_key(item):
        due = item[1].get("dueDate")
        return (0, due) if due else (1, "")

    filtered_docs.sort(key=sort_key)

    # Pagination
    total_count = len(filtered_docs)
    paginated = filtered_docs[offset:offset + limit]
    has_more = (offset + limit) < total_count

    todos = [_todo_doc_to_response(doc.id, data) for doc, data in paginated]

    return TodoListResponse(
        todos=todos,
        total=total_count,
        has_more=has_more,
    )


# =============================================================================
# GET /todos/stats - Get TODO statistics
# =============================================================================

@router.get("/stats", response_model=TodoStatsResponse)
async def get_todo_stats(
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get TODO statistics for the current user."""
    account_id = current_user.account_id
    today = date.today().isoformat()

    # Count open TODOs
    open_docs = list(
        db.collection("todos")
        .where(filter=FieldFilter("accountId", "==", account_id))
        .where(filter=FieldFilter("status", "==", "open"))
        .stream()
    )
    open_count = len(open_docs)

    # Count overdue (dueDate < today and status == open)
    overdue_count = sum(
        1 for doc in open_docs
        if doc.to_dict().get("dueDate") and doc.to_dict().get("dueDate") < today
    )

    # Count done TODOs
    done_count = len(list(
        db.collection("todos")
        .where(filter=FieldFilter("accountId", "==", account_id))
        .where(filter=FieldFilter("status", "==", "done"))
        .limit(1000)
        .stream()
    ))

    # Count pending candidates
    candidate_count = len(list(
        db.collection("todo_candidates")
        .where(filter=FieldFilter("accountId", "==", account_id))
        .where(filter=FieldFilter("status", "==", "pending"))
        .stream()
    ))

    return TodoStatsResponse(
        openCount=open_count,
        doneCount=done_count,
        overdueCount=overdue_count,
        candidateCount=candidate_count,
    )


# =============================================================================
# POST /todos - Create TODO manually
# =============================================================================

@router.post("", response_model=TodoResponse)
async def create_todo(
    req: TodoCreateRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Create a new TODO manually."""
    account_id = current_user.account_id
    now = datetime.now(timezone.utc)

    # Build source if session_id provided
    source = None
    if req.session_id:
        session_doc = db.collection("sessions").document(req.session_id).get()
        if session_doc.exists:
            session_data = session_doc.to_dict()
            source = {
                "sessionId": req.session_id,
                "sessionTitle": session_data.get("title", "Untitled"),
                "createdFrom": TodoSourceType.MANUAL.value,
                "evidence": None,
            }

    todo_data = {
        "accountId": account_id,
        "title": req.title,
        "notes": req.notes,
        "dueDate": req.due_date or date.today().isoformat(),
        "status": TodoStatus.OPEN.value,
        "priority": req.priority.value,
        "source": source,
        "origin": {
            "extractorVersion": "manual",
            "confidence": 1.0,
            "autoCreated": False,
            "userEdited": False,
            "userMoved": False,
        },
        "dedupe": {
            "semanticKey": _generate_semantic_key(req.title, req.session_id or "manual"),
            "rejectedByUser": False,
        },
        "createdAt": now,
        "updatedAt": now,
    }

    doc_ref = db.collection("todos").document()
    doc_ref.set(todo_data)

    logger.info(f"[todos] Created TODO {doc_ref.id} for account {account_id}")

    return _todo_doc_to_response(doc_ref.id, todo_data)


# =============================================================================
# GET /todos/candidates - List TODO candidates
# =============================================================================

@router.get("/candidates", response_model=TodoCandidateListResponse)
async def list_candidates(
    current_user: CurrentUser = Depends(get_current_user),
    session_id: Optional[str] = Query(None, description="Filter by session"),
    status: str = Query("pending", description="Filter by status: pending, all"),
):
    """List TODO candidates for review."""
    account_id = current_user.account_id

    # Fetch all candidates for account (avoids compound index requirement)
    query = db.collection("todo_candidates").where(
        filter=FieldFilter("accountId", "==", account_id)
    )

    docs = list(query.stream())

    # Filter in Python to avoid compound index issues
    filtered = []
    for doc in docs:
        data = doc.to_dict()

        # Status filter
        if status != "all" and data.get("status") != status:
            continue

        # Session filter
        if session_id and data.get("sessionId") != session_id:
            continue

        filtered.append((doc, data))

    # Sort by createdAt descending
    filtered.sort(
        key=lambda x: x[1].get("createdAt") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True
    )

    candidates = [_candidate_doc_to_response(doc.id, data) for doc, data in filtered[:100]]

    return TodoCandidateListResponse(
        candidates=candidates,
        total=len(candidates),
    )


# =============================================================================
# POST /todos/candidates/{candidate_id}:accept - Accept candidate
# =============================================================================

@router.post("/candidates/{candidate_id}:accept", response_model=TodoResponse)
async def accept_candidate(
    candidate_id: str,
    req: Optional[TodoAcceptRequest] = None,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Accept a TODO candidate and create a TODO from it."""
    doc_ref = db.collection("todo_candidates").document(candidate_id)
    doc = doc_ref.get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="Candidate not found")

    data = doc.to_dict()
    if data.get("accountId") != current_user.account_id:
        raise HTTPException(status_code=403, detail="Access denied")

    if data.get("status") != TodoCandidateStatus.PENDING.value:
        raise HTTPException(status_code=400, detail="Candidate already processed")

    now = datetime.now(timezone.utc)

    # Determine due date
    due_date = req.due_date if req and req.due_date else data.get("dueDateProposed")
    if not due_date:
        due_date = date.today().isoformat()

    # Create TODO from candidate
    todo_data = {
        "accountId": data.get("accountId"),
        "title": data.get("title"),
        "notes": None,
        "dueDate": due_date,
        "status": TodoStatus.OPEN.value,
        "priority": TodoPriority.NORMAL.value,
        "source": {
            "sessionId": data.get("sessionId"),
            "sessionTitle": data.get("sessionTitle"),
            "createdFrom": TodoSourceType.MINUTES.value,
            "evidence": data.get("evidence"),
        },
        "origin": {
            "extractorVersion": "todo_v1",
            "confidence": data.get("confidence", 0.5),
            "autoCreated": False,  # User explicitly accepted
            "userEdited": False,
            "userMoved": False,
        },
        "dedupe": {
            "semanticKey": _generate_semantic_key(data.get("title", ""), data.get("sessionId", "")),
            "rejectedByUser": False,
        },
        "createdAt": now,
        "updatedAt": now,
    }

    todo_ref = db.collection("todos").document()
    todo_ref.set(todo_data)

    # Mark candidate as accepted
    doc_ref.update({
        "status": TodoCandidateStatus.ACCEPTED.value,
        "acceptedAt": now,
        "acceptedTodoId": todo_ref.id,
    })

    logger.info(f"[todos] Accepted candidate {candidate_id} -> TODO {todo_ref.id}")

    return _todo_doc_to_response(todo_ref.id, todo_data)


# =============================================================================
# POST /todos/candidates/{candidate_id}:reject - Reject candidate
# =============================================================================

@router.post("/candidates/{candidate_id}:reject")
async def reject_candidate(
    candidate_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Reject a TODO candidate.

    This permanently marks the candidate as rejected to prevent
    re-extraction of the same TODO in future runs.
    """
    doc_ref = db.collection("todo_candidates").document(candidate_id)
    doc = doc_ref.get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="Candidate not found")

    data = doc.to_dict()
    if data.get("accountId") != current_user.account_id:
        raise HTTPException(status_code=403, detail="Access denied")

    now = datetime.now(timezone.utc)

    # Mark as rejected
    doc_ref.update({
        "status": TodoCandidateStatus.REJECTED.value,
        "rejectedAt": now,
    })

    # Also store semantic key in rejected_todo_keys for future deduplication
    semantic_key = _generate_semantic_key(data.get("title", ""), data.get("sessionId", ""))
    db.collection("rejected_todo_keys").document(semantic_key).set({
        "accountId": current_user.account_id,
        "title": data.get("title"),
        "sessionId": data.get("sessionId"),
        "rejectedAt": now,
    })

    logger.info(f"[todos] Rejected candidate {candidate_id}")

    return {"ok": True, "rejected": candidate_id}


# =============================================================================
# GET /todos/{todo_id} - Get single TODO
# =============================================================================

@router.get("/{todo_id}", response_model=TodoResponse)
async def get_todo(
    todo_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get a single TODO by ID."""
    doc = db.collection("todos").document(todo_id).get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="TODO not found")

    data = doc.to_dict()
    if data.get("accountId") != current_user.account_id:
        raise HTTPException(status_code=403, detail="Access denied")

    return _todo_doc_to_response(doc.id, data)


# =============================================================================
# PATCH /todos/{todo_id} - Update TODO
# =============================================================================

@router.patch("/{todo_id}", response_model=TodoResponse)
async def update_todo(
    todo_id: str,
    req: TodoUpdateRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Update a TODO item (title, notes, dueDate, priority)."""
    doc_ref = db.collection("todos").document(todo_id)
    doc = doc_ref.get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="TODO not found")

    data = doc.to_dict()
    if data.get("accountId") != current_user.account_id:
        raise HTTPException(status_code=403, detail="Access denied")

    now = datetime.now(timezone.utc)
    update_data = {"updatedAt": now}

    if req.title is not None:
        update_data["title"] = req.title
    if req.notes is not None:
        update_data["notes"] = req.notes
    if req.due_date is not None:
        update_data["dueDate"] = req.due_date
    if req.priority is not None:
        update_data["priority"] = req.priority.value

    # Mark as user-edited to prevent auto-overwrites
    update_data["origin.userEdited"] = True

    doc_ref.update(update_data)

    # Fetch updated doc
    updated_doc = doc_ref.get()
    return _todo_doc_to_response(updated_doc.id, updated_doc.to_dict())


# =============================================================================
# POST /todos/{todo_id}:toggle_done - Toggle TODO status
# =============================================================================

@router.post("/{todo_id}:toggle_done", response_model=TodoResponse)
async def toggle_todo_done(
    todo_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Toggle a TODO between open and done status."""
    doc_ref = db.collection("todos").document(todo_id)
    doc = doc_ref.get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="TODO not found")

    data = doc.to_dict()
    if data.get("accountId") != current_user.account_id:
        raise HTTPException(status_code=403, detail="Access denied")

    current_status = data.get("status", "open")
    new_status = TodoStatus.OPEN.value if current_status == TodoStatus.DONE.value else TodoStatus.DONE.value

    now = datetime.now(timezone.utc)
    doc_ref.update({
        "status": new_status,
        "updatedAt": now,
        "completedAt": now if new_status == TodoStatus.DONE.value else None,
    })

    updated_doc = doc_ref.get()
    return _todo_doc_to_response(updated_doc.id, updated_doc.to_dict())


# =============================================================================
# POST /todos/{todo_id}:move - Move TODO to different date
# =============================================================================

@router.post("/{todo_id}:move", response_model=TodoResponse)
async def move_todo(
    todo_id: str,
    req: TodoMoveRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Move a TODO to a different date (for drag-and-drop UI)."""
    doc_ref = db.collection("todos").document(todo_id)
    doc = doc_ref.get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="TODO not found")

    data = doc.to_dict()
    if data.get("accountId") != current_user.account_id:
        raise HTTPException(status_code=403, detail="Access denied")

    now = datetime.now(timezone.utc)
    doc_ref.update({
        "dueDate": req.due_date,
        "updatedAt": now,
        "origin.userMoved": True,  # Mark as user-moved to prevent auto-update
    })

    logger.info(f"[todos] Moved TODO {todo_id} to {req.due_date}")

    updated_doc = doc_ref.get()
    return _todo_doc_to_response(updated_doc.id, updated_doc.to_dict())


# =============================================================================
# DELETE /todos/{todo_id} - Delete TODO
# =============================================================================

@router.delete("/{todo_id}")
async def delete_todo(
    todo_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Delete a TODO item."""
    doc_ref = db.collection("todos").document(todo_id)
    doc = doc_ref.get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="TODO not found")

    data = doc.to_dict()
    if data.get("accountId") != current_user.account_id:
        raise HTTPException(status_code=403, detail="Access denied")

    doc_ref.delete()

    logger.info(f"[todos] Deleted TODO {todo_id}")

    return {"ok": True, "deleted": todo_id}


# =============================================================================
# POST /todos:cleanup - Cleanup old completed TODOs (scheduled task)
# =============================================================================

TODO_RETENTION_DAYS = 30  # 完了後30日で自動削除

@router.post(":cleanup")
async def cleanup_completed_todos(
    background_tasks: BackgroundTasks,
    dry_run: bool = Query(False, description="If true, only count without deleting"),
):
    """
    Cleanup completed TODOs older than 30 days.

    This endpoint should be called by Cloud Scheduler daily.
    Deletes TODOs where:
    - status == "done"
    - completedAt < (now - 30 days)

    Also cleans up old rejected candidates (> 90 days).
    """
    now = datetime.now(timezone.utc)
    cutoff_date = now - timedelta(days=TODO_RETENTION_DAYS)
    candidate_cutoff = now - timedelta(days=90)  # Candidates kept longer for dedup

    deleted_todos = 0
    deleted_candidates = 0
    errors = []

    # 1. Find and delete old completed TODOs
    try:
        todos_query = db.collection("todos").where(
            filter=FieldFilter("status", "==", "done")
        )

        batch = db.batch()
        batch_count = 0

        for doc in todos_query.stream():
            data = doc.to_dict()
            completed_at = data.get("completedAt")

            # Skip if no completedAt (shouldn't happen, but be safe)
            if not completed_at:
                continue

            # Check if older than retention period
            if isinstance(completed_at, datetime):
                if completed_at < cutoff_date:
                    if not dry_run:
                        batch.delete(doc.reference)
                        batch_count += 1

                        # Commit in batches of 400
                        if batch_count >= 400:
                            batch.commit()
                            batch = db.batch()
                            batch_count = 0

                    deleted_todos += 1

        # Commit remaining
        if batch_count > 0 and not dry_run:
            batch.commit()

    except Exception as e:
        logger.error(f"[todos:cleanup] Error cleaning TODOs: {e}")
        errors.append(f"todos: {str(e)}")

    # 2. Clean up old rejected/accepted candidates
    try:
        candidates_query = db.collection("todo_candidates").where(
            filter=FieldFilter("status", "in", ["rejected", "accepted"])
        )

        batch = db.batch()
        batch_count = 0

        for doc in candidates_query.stream():
            data = doc.to_dict()

            # Check rejectedAt or acceptedAt
            action_at = data.get("rejectedAt") or data.get("acceptedAt")
            if not action_at:
                continue

            if isinstance(action_at, datetime):
                if action_at < candidate_cutoff:
                    if not dry_run:
                        batch.delete(doc.reference)
                        batch_count += 1

                        if batch_count >= 400:
                            batch.commit()
                            batch = db.batch()
                            batch_count = 0

                    deleted_candidates += 1

        if batch_count > 0 and not dry_run:
            batch.commit()

    except Exception as e:
        logger.error(f"[todos:cleanup] Error cleaning candidates: {e}")
        errors.append(f"candidates: {str(e)}")

    result = {
        "dryRun": dry_run,
        "deletedTodos": deleted_todos,
        "deletedCandidates": deleted_candidates,
        "retentionDays": TODO_RETENTION_DAYS,
        "candidateRetentionDays": 90,
        "errors": errors if errors else None,
    }

    logger.info(f"[todos:cleanup] Completed: {result}")

    return result
