from fastapi import APIRouter, Depends, HTTPException, status
from google.cloud import firestore
import secrets
import os
from datetime import datetime, timedelta, timezone

from app.firebase import db
from app.dependencies import get_current_user, User, ensure_is_owner
from app.util_models import (
    ShareResponse, ShareLinkResponse, ShareByCodeRequest, ShareCodeLookupResponse,
    SharedSessionDTO
)

router = APIRouter()

# [FIX] Use Cloud Run URL or Env Var. "deepnote.app" is not active yet.
# Default to current Dev Cloud Run URL if not set.
DEFAULT_URL = "https://classnote-api-900324644592.asia-northeast1.run.app"
FRONTEND_BASE_URL = os.environ.get("FRONTEND_BASE_URL", DEFAULT_URL)

MEMBER_ROLES = {"owner", "editor", "viewer"}
ROLE_PRIORITY = {"viewer": 1, "editor": 2, "owner": 3}

def _normalize_member_role(raw: str | None, default: str = "viewer") -> str:
    if not raw:
        return default
    role = raw.lower()
    if role not in MEMBER_ROLES:
        raise HTTPException(status_code=400, detail="Invalid role")
    return role

def _merge_member_role(existing: str | None, requested: str) -> str:
    if not existing:
        return requested
    if ROLE_PRIORITY.get(existing, 0) >= ROLE_PRIORITY.get(requested, 0):
        return existing
    return requested

def _session_member_ref(session_id: str, user_id: str):
    return db.collection("session_members").document(f"{session_id}_{user_id}")

def _resolve_display_name(user_doc: dict | None, fallback: str | None = None) -> str | None:
    if user_doc:
        return user_doc.get("displayName") or user_doc.get("name") or user_doc.get("email") or fallback
    return fallback

def _upsert_session_member(session_id: str, user_id: str, role: str, source: str, display_name: str | None):
    now = datetime.now(timezone.utc)
    member_ref = _session_member_ref(session_id, user_id)
    member_doc = member_ref.get()
    payload = {
        "sessionId": session_id,
        "userId": user_id,
        "role": role,
        "displayNameSnapshot": display_name,
        "updatedAt": now,
    }
    if not member_doc.exists:
        payload["source"] = source
        payload["joinedAt"] = now
        payload["createdAt"] = now
    member_ref.set(payload, merge=True)

    # [NEW] Also update participants map in session doc
    db.collection("sessions").document(session_id).set({
        "participants": {
            user_id: {
                "role": role,
                "joinedAt": payload.get("joinedAt") or now,
                "updatedAt": now
            }
        }
    }, merge=True)


def _ensure_session_meta(user_id: str, session_id: str, role: str):
    now = datetime.now(timezone.utc)
    meta_ref = db.collection("users").document(user_id).collection("sessionMeta").document(session_id)
    meta_doc = meta_ref.get()
    if meta_doc.exists:
        meta_ref.update({"role": role, "updatedAt": now})
        return
    meta_ref.set({
        "sessionId": session_id,
        "role": role,
        "isPinned": False,
        "isArchived": False,
        "lastOpenedAt": None,
        "createdAt": now,
        "updatedAt": now,
    })

def _add_participant_to_session(session_id: str, user_id: str):
    db.collection("sessions").document(session_id).update({
        "participantUserIds": firestore.ArrayUnion([user_id]),
        "sharedWithUserIds": firestore.ArrayUnion([user_id]),
        "sharedUserIds": firestore.ArrayUnion([user_id]),
        f"sharedWith.{user_id}": True,
        "visibility": "shared",
    })

def _remove_participant_from_session(session_id: str, user_id: str):
    db.collection("sessions").document(session_id).update({
        "participantUserIds": firestore.ArrayRemove([user_id]),
        "sharedWithUserIds": firestore.ArrayRemove([user_id]),
        "sharedUserIds": firestore.ArrayRemove([user_id]),
        f"sharedWith.{user_id}": firestore.DELETE_FIELD,
        f"participants.{user_id}": firestore.DELETE_FIELD, # [NEW]
    })

# --- User Sharing ---

@router.get("/share-code/{code}", response_model=ShareCodeLookupResponse)
async def lookup_user_by_share_code(code: str, current_user: User = Depends(get_current_user)):
    code = code.strip()
    if not code:
        raise HTTPException(status_code=400, detail="Code is required")
        
    # 1. Try O(1) lookup
    code_ref = db.collection("shareCodes").document(code)
    code_snap = code_ref.get()
    
    target_uid = None
    if code_snap.exists:
        target_uid = code_snap.to_dict().get("userId")
        
    # 2. Fallback to legacy
    if not target_uid:
        qs = db.collection("users").where("shareCode", "==", code).limit(1).stream()
        docs = list(qs)
        if docs:
            target_uid = docs[0].id
            
    if not target_uid:
        raise HTTPException(status_code=404, detail="User not found")
        
    if target_uid == current_user.uid:
        # Optional: Allow looking up self? User spec says "Cannot share to yourself", but lookup might be allowed.
        # But commonly we might verify it's valid.
        pass

    target_user_doc = db.collection("users").document(target_uid).get()
    if not target_user_doc.exists:
        raise HTTPException(status_code=404, detail="Target user not found")
        
    data = target_user_doc.to_dict()
    return ShareCodeLookupResponse(
        userId=target_uid,
        displayName=data.get("displayName"),
        email=data.get("email")
    )

@router.post("/sessions/{session_id}/share", response_model=ShareResponse)
async def share_session_to_user(
    session_id: str, 
    body: ShareByCodeRequest,
    current_user: User = Depends(get_current_user)
):
    doc_ref = db.collection("sessions").document(session_id)
    snapshot = doc_ref.get()
    
    if not snapshot.exists:
        raise HTTPException(status_code=404, detail="Session not found")
        
    session_data = snapshot.to_dict()
    ensure_is_owner(session_data, current_user.uid, session_id)

    code = (body.targetShareCode or "").strip()
    # Support "code" field as well if body model allows, but sticking to targetShareCode as per defined model or user request
    if hasattr(body, "code") and body.code:
        code = body.code
        
    if not code:
        raise HTTPException(status_code=400, detail="Code is required")

    # Efficient Lookup via shareCodes collection
    code_ref = db.collection("shareCodes").document(code)
    code_snap = code_ref.get()
    
    if not code_snap.exists:
         # Fallback to legacy query if needed? Or just fail. User wants O(1).
         # Let's try legacy query just in case migration is slow? 
         # No, User emphasized "You are NOT saving code to standard location". 
         # I should trust the new location primarily. But to be safe:
         qs = db.collection("users").where("shareCode", "==", code).limit(1).stream()
         docs = list(qs)
         if not docs:
             raise HTTPException(status_code=404, detail="User not found for this code")
         target_uid = docs[0].id
    else:
         target_uid = code_snap.to_dict()["userId"]

    if target_uid == current_user.uid:
        raise HTTPException(status_code=400, detail="Cannot share with yourself")

    target_user_doc = db.collection("users").document(target_uid).get()
    if not target_user_doc.exists:
        raise HTTPException(status_code=404, detail="User not found")

    tdata = target_user_doc.to_dict() or {}
    # Allow sharing if isShareable is missing (default True)
    if tdata.get("isShareable", tdata.get("allowSearch", True)) is False:
        raise HTTPException(status_code=400, detail="Target user does not accept shares")
        
    member_doc = _session_member_ref(session_id, target_uid).get()
    requested_role = _normalize_member_role("viewer")
    existing_role = None
    if member_doc.exists:
        existing_role = (member_doc.to_dict() or {}).get("role")
    role = _merge_member_role(existing_role, requested_role)
    _upsert_session_member(
        session_id=session_id,
        user_id=target_uid,
        role=role,
        source="directInvite",
        display_name=_resolve_display_name(tdata),
    )
    _add_participant_to_session(session_id, target_uid)
    _ensure_session_meta(target_uid, session_id, role.upper())
    
    # Return updated state
    updated_snap = doc_ref.get()
    updated_data = updated_snap.to_dict()
    
    shared_list = list((updated_data.get("sharedWith") or {}).keys()) or updated_data.get("sharedWithUserIds") or updated_data.get("sharedUserIds") or []
    return ShareResponse(
        sessionId=session_id,
        sharedUserIds=shared_list
    )

@router.delete("/sessions/{session_id}/share/{target_uid}", status_code=204)
async def revoke_share(
    session_id: str,
    target_uid: str,
    current_user: User = Depends(get_current_user)
):
    doc_ref = db.collection("sessions").document(session_id)
    snapshot = doc_ref.get()
    
    if not snapshot.exists:
        raise HTTPException(status_code=404, detail="Session not found")
        
    session_data = snapshot.to_dict()
    ensure_is_owner(session_data, current_user.uid, session_id)

    owner_id = session_data.get("ownerUserId") or session_data.get("ownerUid") or session_data.get("userId")
    if target_uid == owner_id:
        raise HTTPException(status_code=400, detail="Cannot remove owner")
    
    _remove_participant_from_session(session_id, target_uid)
    _session_member_ref(session_id, target_uid).delete()
    db.collection("users").document(target_uid).collection("sessionMeta").document(session_id).delete()
    
    return

# --- Web Fallback (Universal Links) ---

from fastapi.responses import HTMLResponse

@router.get("/s/{token}")
async def share_fallback(token: str):
    """
    Universal Links Fallback Page.
    If the app is installed, the link 'https://deepnote.app/s/{token}' should open the app directly.
    If not installed (or failed), this HTML page is shown.
    """
    html = f"""
    <html><head>
      <meta name="viewport" content="width=device-width,initial-scale=1"/>
      <title>DeepNote</title>
      <style>
        body {{ font-family: -apple-system, sans-serif; padding: 24px; text-align: center; color: #333; }}
        .btn {{ display: inline-block; background: #007AFF; color: white; padding: 12px 24px; border-radius: 8px; text-decoration: none; font-weight: bold; margin-top: 20px; }}
        .token {{ background: #f0f0f0; padding: 8px; border-radius: 4px; font-family: monospace; display: inline-block; word-break: break-all; }}
      </style>
    </head>
    <body>
      <h2>DeepNote 共有リンク</h2>
      <p>アプリで開こうとしています...</p>
      
      <p>もしアプリが開かない場合は、<br>まだインストールされていない可能性があります。</p>
      
      <a href="https://apps.apple.com/jp/app/classnotex/id6739505779" class="btn">App Storeで入手</a>
      
      <br><br>
      <p style="font-size: 12px; color: #888;">Token: <span class="token">{token}</span></p>
    </body></html>
    """
    return HTMLResponse(html)

# --- Link Sharing ---

@router.api_route("/sessions/{session_id}/share_link", methods=["GET", "POST"], response_model=ShareLinkResponse)
async def create_share_link(
    session_id: str,
    current_user: User = Depends(get_current_user)
):
    doc_ref = db.collection("sessions").document(session_id)
    snapshot = doc_ref.get()
    
    if not snapshot.exists:
        raise HTTPException(status_code=404, detail="Session not found")
        
    session_data = snapshot.to_dict()
    ensure_is_owner(session_data, current_user.uid, session_id)
    
    # [FIX] Reuse existing valid link if available (Idempotency)
    # Query shareLinks for this session
    existing_docs = db.collection("shareLinks").where("sessionId", "==", session_id).stream()
    now = datetime.now(timezone.utc)
    
    for d in existing_docs:
        d_data = d.to_dict()
        expires_at = d_data.get("expiresAt")
        # Check if expired
        if expires_at and expires_at.replace(tzinfo=timezone.utc) > now:
            # Found valid token! Use it.
            token = d.id
            url = f"{FRONTEND_BASE_URL}/s/{token}"
            return ShareLinkResponse(url=url)
    
    # Generate New Token if no valid one found
    token = secrets.token_urlsafe(16)
    expires_at = now + timedelta(days=7)
    
    db.collection("shareLinks").document(token).set({
        "sessionId": session_id,
        "ownerId": current_user.uid,
        "expiresAt": expires_at,
        "createdAt": firestore.SERVER_TIMESTAMP
    })
    
    url = f"{FRONTEND_BASE_URL}/s/{token}"
    return ShareLinkResponse(url=url)

@router.get("/share/{token}")
async def resolve_share_link(token: str):
    """
    Share Link Fallback (Proposal B):
    If opened in browser (no app installed), redirect to App Store.
    App with Universal Links will capture this URL and call /share/{token}/info instead.
    """
    # TODO: Use environment variable or config for App ID
    APP_STORE_URL = "https://apps.apple.com/jp/app/classnotex/id6739505779" 
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=APP_STORE_URL, status_code=302)

@router.get("/share/{token}/info")
async def resolve_share_link_info(token: str):
    """
    Endpoint for the App to resolve token to sessionId.
    """
    link_doc = db.collection("shareLinks").document(token).get()
    if not link_doc.exists:
        raise HTTPException(status_code=404, detail="Share link not found")
        
    link_data = link_doc.to_dict()
    
    # Check Expiration
    expires_at = link_data.get("expiresAt")
    if expires_at:
        # Check if expired
        # firestore timestamp to datetime
        if expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
             raise HTTPException(status_code=410, detail="Share link expired")
             
    return {"sessionId": link_data["sessionId"]}

@router.post("/share/{token}/join", response_model=ShareResponse)
async def join_via_share_link(
    token: str,
    current_user: User = Depends(get_current_user)
):
    link_doc = db.collection("shareLinks").document(token).get()
    if not link_doc.exists:
        raise HTTPException(status_code=404, detail="Share link not found")
        
    link_data = link_doc.to_dict()
    
    # Check Expiration
    expires_at = link_data.get("expiresAt")
    if expires_at:
        if expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
             raise HTTPException(status_code=410, detail="Share link expired")

    session_id = link_data["sessionId"]
    
    # Get user profile for display name
    user_doc = db.collection("users").document(current_user.uid).get()
    user_data = user_doc.to_dict() or {}
    
    if user_data.get("isShareable", user_data.get("allowSearch", True)) is False:
        # If user explicitly disabled sharing, can they join? Usually yes, but they might not be discoverable.
        # Allowing join.
        pass

    _upsert_session_member(
        session_id=session_id,
        user_id=current_user.uid,
        role="viewer",
        source="link",
        display_name=_resolve_display_name(user_data),
    )
    _add_participant_to_session(session_id, current_user.uid)
    _ensure_session_meta(current_user.uid, session_id, "VIEWER")
    
    sess_doc = db.collection("sessions").document(session_id).get()
    updated_data = sess_doc.to_dict() or {}
    shared_list = updated_data.get("participantUserIds") or []
    
    return ShareResponse(
        sessionId=session_id,
        sharedUserIds=shared_list
    )
@router.get("/shares/{token}", response_model=SharedSessionDTO)
async def get_shared_session_details(token: str):
    """
    Public read-only session detail view for App (Universal Links).
    No Authentication required (Token is key).
    """
    # 1. Resolve Token
    link_doc = db.collection("shareLinks").document(token).get()
    if not link_doc.exists:
        raise HTTPException(status_code=404, detail="Share link not found")
        
    link_data = link_doc.to_dict()
    
    # Check Expiration
    expires_at = link_data.get("expiresAt")
    if expires_at:
        # Check if expired
        if expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
             raise HTTPException(status_code=410, detail="Share link expired")

    session_id = link_data["sessionId"]
    owner_id = link_data.get("ownerId")

    # 2. Fetch Session Data
    session_doc = db.collection("sessions").document(session_id).get()
    if not session_doc.exists:
        raise HTTPException(status_code=404, detail="Session not found")
    
    s_data = session_doc.to_dict()

    # 3. Fetch Owner Display Name (Optional)
    owner_name = "Unknown"
    if owner_id:
        u_doc = db.collection("users").document(owner_id).get()
        if u_doc.exists:
            u_data = u_doc.to_dict()
            owner_name = u_data.get("displayName") or u_data.get("email") or "Unknown"

    # 4. Resolve Transcript (Simple fetch for now)
    # Ideally use transcript service if complex resolution needed, but usually it's in doc or subcollection
    transcript_text = s_data.get("transcriptText")
    
    # If not in main doc, check subcollection (rare case in this architecture but possible)
    # For now, MVP assumes it's denormalized or in main doc if ready. 
    # Actually, resolve_transcript_text logic is better but importing it might cause circular dep?
    # Let's check imports.
    
    # 5. Summary
    summary_markdown = s_data.get("summaryMarkdown")

    return SharedSessionDTO(
        sessionId=session_id,
        title=s_data.get("title", "No Title"),
        transcriptText=transcript_text,
        summaryMarkdown=summary_markdown,
        ownerDisplayName=owner_name,
        createdAt=s_data.get("createdAt")
    )
