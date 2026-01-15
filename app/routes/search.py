
from fastapi import APIRouter, Depends, Query, HTTPException
from typing import List, Optional
from datetime import datetime
from google.cloud import firestore


from app.firebase import db
from app.dependencies import get_current_user, User
from app.util_models import SessionResponse, TaskResponse, DecisionResponse

router = APIRouter(prefix="/search", tags=["Search"])

@router.get("/sessions", response_model=List[SessionResponse])
async def search_sessions(
    q: Optional[str] = Query(None, description="Query text (title match or simple search)"),
    mode: Optional[str] = None,
    tag: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    limit: int = 20,
    current_user: User = Depends(get_current_user)
):
    """
    Search sessions with filters.
    Note: Firestore requires composite indexes for complex filtering.
    """
    # Base query: Only sessions user can access
    # Complex ACL (owner OR sharedWith) is hard in one query unless we duplicate data or use separate queries.
    # For MVP "Search", we primarily search *Owned* sessions or use a "my_sessions" collection index?
    # Security Rule: userId == current_user.uid is standard.
    # Shared sessions might be missed if we strictly query where("userId", "==", uid).
    # Spec says: "Search... keyword, tag, date".
    # User's sessions collection has "userId" (owner).
    
    # We will query Owned sessions first. Merging shared is complex without dedicated index or duplication.
    # Let's focus on Owned sessions for now, or use "ownerUd == uid" filter.
    
    # We really want:
    # collection("sessions").where("ownerUid", "==", uid)
    # AND filter by tags, mode, date.
    
    sessions_ref = db.collection("sessions")
    query = sessions_ref.where("ownerUid", "==", current_user.uid)
    
    if mode:
        query = query.where("mode", "==", mode)
    
    if tag:
        # Array-contains filter
        query = query.where("tags", "array_contains", tag)
        
    if from_date:
        # Assuming ISO string
        query = query.where("createdAt", ">=", from_date)
        
    if to_date:
        query = query.where("createdAt", "<=", to_date)

    # Determine ordering?
    # If using inequality (date), must sort by that field first.
    # If from_date/to_date is used, generic sort by createdAt is good.
    if from_date or to_date:
        query = query.order_by("createdAt", direction=firestore.Query.DESCENDING)
    else:
        # Default sort
        query = query.order_by("createdAt", direction=firestore.Query.DESCENDING)

    # Execute
    query = query.limit(limit)
    docs = query.stream()
    
    results = []
    keyword = q.lower() if q else None
    
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        
        # In-memory text filter for 'q' since Firestore native full-text is limited without Meilisearch/Algolia
        if keyword:
            title = (data.get("title") or "").lower()
            # summary?
            # description?
            if keyword not in title:
                continue
                
        # Format dates
        for key in ["createdAt", "updatedAt", "startedAt", "endedAt"]:
             if key in data and hasattr(data[key], 'isoformat'):
                 data[key] = data[key].isoformat()
                 
        results.append(SessionResponse(**data))
        
    return results


@router.get("/tasks", response_model=List[TaskResponse])
async def search_tasks(
    status: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 20,
    current_user: User = Depends(get_current_user)
):
    """
    Search tasks assigned to or owned by user.
    Assumes `tasks` root collection exists with `userId` or `assignee` fields.
    """
    tasks_ref = db.collection("tasks")
    
    # User can see tasks where they are owner OR assignee
    # Firestore OR queries are separate. For now, let's query where userId == uid (Owner/Creator context)
    # Or assignee == uid.
    # Let's fetch both or just userId for "My Meeting Tasks"?
    # Spec: "tasks/{autoId} { userId: uid, assignee: uid2 }"
    
    # Let's simple query: userId == uid (Tasks I created/own) 
    # OR assignee == uid (Tasks assigned to me)
    # Currently implementing userId match.
    
    query = tasks_ref.where("userId", "==", current_user.uid)
    
    if status:
        query = query.where("status", "==", status)
        
    query = query.order_by("createdAt", direction=firestore.Query.DESCENDING).limit(limit)
    
    docs = query.stream()
    results = []
    
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        
        if q:
            title = (data.get("title") or "").lower()
            if q.lower() not in title:
                continue
                
        if "createdAt" in data and hasattr(data["createdAt"], 'isoformat'):
             data["createdAt"] = data["createdAt"].isoformat()
             
        results.append(TaskResponse(**data))
        
    return results


@router.get("/decisions", response_model=List[DecisionResponse])
async def search_decisions(
    q: Optional[str] = None,
    limit: int = 20,
    current_user: User = Depends(get_current_user)
):
    """
    Search decisions extracted from sessions.
    Assumes `decisions` root collection? Or `summaries` collection query?
    User spec: "3.4 summaries / tasks / decisions". 
    Usually decisions are inside `summary` object or separate.
    If separate `decisions` not strictly defined as root collection, we might check `summaries`.
    User Spec 4.6 says: GET /search/decisions.
    Let's assume we maintain a `decisions` collection or query Summaries.
    For MVP, let's query `sessions` and extract decisions? No, that's heavy.
    Let's assume we query 'summaries' collection if it exists?
    Schema 3.4 says `summaries/{sessionId}`. It's 1-to-1 with session? Or root collection `summaries`?
    If root `summaries` collection exists, we can query it.
    """
    # Assuming summaries collection with userId
    summaries_ref = db.collection("summaries")
    query = summaries_ref.where("userId", "==", current_user.uid)
    
    # We can't easily array-contains-any for partial text in 'decisions' array.
    # We have to fetch recent summaries and client-side filter or rely on simple filter.
    query = query.limit(limit * 2) # Fetch more to filter in memory
    docs = query.stream()
    
    results = []
    for doc in docs:
        data = doc.to_dict()
        decisions_list = data.get("decisions", [])
        if not decisions_list: continue
        
        # Flatten
        for d_text in decisions_list:
             if q and q.lower() not in d_text.lower():
                 continue
                 
             results.append(DecisionResponse(
                 id=f"{doc.id}_{hash(d_text)}", # Pseudo ID
                 sessionId=doc.id, # Doc ID is sessionId usually
                 content=d_text,
                 createdAt=None # Summary doesn't usually track per-decision time
             ))
             if len(results) >= limit: break
        if len(results) >= limit: break
            
    return results
