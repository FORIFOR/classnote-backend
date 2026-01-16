import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks

from app.firebase import db, storage_client, AUDIO_BUCKET_NAME, MEDIA_BUCKET_NAME
from app.dependencies import get_current_user, User, ensure_can_view, ensure_is_owner
from app.routes.sessions import _session_doc_ref, _derived_doc_ref, _map_derived_status
from app.task_queue import enqueue_summarize_task, enqueue_quiz_task, enqueue_explain_task, enqueue_playlist_task
from app.util_models import (
    AssetManifest,
    AssetItem,
    AssetStatus,
    AssetResolveRequest,
    AssetResolveResponse,
    ResolvedAsset,
    AssetResolveResponse,
    ResolvedAsset,
    JobStatus,
    DerivedStatusResponse,
)


router = APIRouter()
logger = logging.getLogger("app.assets")

@router.get("/assets/ping", include_in_schema=False)
async def ping_assets():
    return {"status": "ok", "msg": "Assets router is mounted"}

def _get_asset_item_from_derived(session_id: str, type_key: str, data: dict, derived_map: dict) -> Optional[AssetItem]:
    """
    Construct AssetItem from Session Data + Derived Data.
    Handles legacy/compatibility logic.
    """
    status = AssetStatus.MISSING
    version = 1
    updated_at = None
    content_type = None
    size_bytes = None
    sha256 = None
    error = None
    
    # Check Derived first (New Generation)
    derived = derived_map.get(type_key)
    if derived:
        st = _map_derived_status(derived.get("status"))
        if st == JobStatus.COMPLETED:
            status = AssetStatus.READY
            version = derived.get("version", 1) # Future proofing
            updated_at = derived.get("updatedAt")
        elif st == JobStatus.FAILED:
            status = AssetStatus.ERROR
            error = derived.get("errorReason")
        elif st in (JobStatus.PENDING, JobStatus.RUNNING):
            status = AssetStatus.PROCESSING
    
    # Fallback/Compat with Session Data
    if status == AssetStatus.MISSING:
        # Check session fields
        # e.g. summaryStatus / summaryMarkdown
        session_status = _map_derived_status(data.get(f"{type_key}Status"))
        if session_status == JobStatus.COMPLETED:
            # Check if content actually exists
            has_content = False
            if type_key == "transcript":
                has_content = bool(data.get("transcriptText"))
            elif type_key == "summary":
                has_content = bool(data.get("summaryMarkdown"))
            elif type_key == "quiz":
                has_content = bool(data.get("quizMarkdown"))
            elif type_key == "playlist":
                has_content = bool(data.get("playlist")) or bool(data.get("summaryMarkdown")) # Legacy coupled
            
            if has_content:
                status = AssetStatus.READY
                updated_at = data.get(f"{type_key}UpdatedAt")
        elif session_status in (JobStatus.PENDING, JobStatus.RUNNING):
            status = AssetStatus.PROCESSING
    
    # Return NOT_STARTED instead of None to prevent client from deleting local data
    if status == AssetStatus.MISSING:
        return AssetItem(status=AssetStatus.NOT_STARTED)

    # Determine Content Type (approximation for manifest)
    if type_key == "audio":
        content_type = "audio/mp4" # Default
    elif type_key == "transcript":
        content_type = "application/json"
    elif type_key == "summary":
        content_type = "text/markdown"
    elif type_key == "quiz":
        content_type = "application/json"
    
    return AssetItem(
        status=status,
        version=version,
        updatedAt=updated_at,
        contentType=content_type,
        sizeBytes=size_bytes,
        sha256=sha256,
        error=error
    )

def _get_audio_asset(data: dict) -> Optional[AssetItem]:
    """Special handling for audio asset from session data."""
    # Check audioStatus or audioPath
    path = data.get("audioPath")
    status_str = data.get("audioStatus")
    
    status = AssetStatus.MISSING
    if status_str == "uploaded" or (path and status_str != "failed"):
        status = AssetStatus.READY
    elif status_str == "uploading":
        status = AssetStatus.PROCESSING
    elif status_str == "failed":
        status = AssetStatus.ERROR
    
    if status == AssetStatus.MISSING:
        return None

    # Extract metadata if available
    meta = data.get("audioMeta") or {}
    
    return AssetItem(
        status=status,
        version=1,
        updatedAt=data.get("createdAt"), # Fallback
        contentType=meta.get("container") and f"audio/{meta['container']}" or "audio/mp4",
        sizeBytes=meta.get("sizeBytes"),
        sha256=meta.get("payloadSha256"),
        error=None 
    )


@router.get("/sessions/{session_id}/assets", response_model=AssetManifest)
async def get_session_assets(session_id: str, current_user: User = Depends(get_current_user)):
    """
    Get Asset Manifest for a session.
    Tells client what exists and its status.
    """
    doc_ref = _session_doc_ref(session_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Session not found")
    
    data = doc.to_dict()
    ensure_can_view(data, current_user.uid, session_id)
    
    # Fetch all derived docs in parallel (efficient)
    derived_refs = [
        _derived_doc_ref(session_id, "summary"),
        _derived_doc_ref(session_id, "quiz"),
        _derived_doc_ref(session_id, "playlist"),
        _derived_doc_ref(session_id, "explain"),
    ]
    derived_snaps = db.get_all(derived_refs)
    derived_map = {}
    for snap in derived_snaps:
        if snap.exists:
            # key is assuming predictable collection name? 
            # actually snap.id is "summary" etc.
            derived_map[snap.id] = snap.to_dict()
            
    manifest = AssetManifest()
    
    # 1. Audio
    manifest.audio = _get_audio_asset(data)
    
    # 2. Transcript (Special: currently only in session doc)
    # Future: _derived_doc_ref(session_id, "transcribe")
    trans_text = data.get("transcriptText")
    session_status = data.get("status", "")

    if trans_text:
        # Transcript exists and is ready
        manifest.transcript = AssetItem(
            status=AssetStatus.READY,
            version=1,
            updatedAt=data.get("transcriptUpdatedAt") or data.get("updatedAt"),
            contentType="application/json",
        )
    elif session_status in ["録音中", "recording", "processing"]:
        # Recording or processing in progress
        manifest.transcript = AssetItem(status=AssetStatus.PROCESSING)
    else:
        # No transcript yet - return NOT_STARTED instead of null
        # This prevents client from treating null as "server has no data, delete local"
        manifest.transcript = AssetItem(status=AssetStatus.NOT_STARTED)
    
    # 3. Summary
    manifest.summary = _get_asset_item_from_derived(session_id, "summary", data, derived_map)
    
    # 4. Quiz
    manifest.quiz = _get_asset_item_from_derived(session_id, "quiz", data, derived_map)
    
    # 5. Playlist
    manifest.playlist = _get_asset_item_from_derived(session_id, "playlist", data, derived_map)
    
    # 6. Images (Placeholder for now, assumes specific structure in future)
    # For now, if we have imageNotes collection or similar?
    # Keeping empty as per current implementation status.
    
    return manifest


@router.post("/sessions/{session_id}/assets/resolve", response_model=AssetResolveResponse)
async def resolve_session_assets(
    session_id: str, 
    req: AssetResolveRequest, 
    current_user: User = Depends(get_current_user)
):
    """
    Generate Signed URLs for requested assets.
    Handles legacy text assets by serving them directly or uploading to GCS on-demand (TBD).
    For now: Signed URLs for GCS, direct content for text (via special internal URL or data URI?).
    User requirement: "resolve -> URL".
    """
    doc_ref = _session_doc_ref(session_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Session not found")
    
    data = doc.to_dict()
    ensure_can_view(data, current_user.uid, session_id)
    
    allowed_types = {"audio", "summary", "quiz", "transcript"}
    unknown_types = [type_key for type_key in req.types if type_key not in allowed_types]
    if unknown_types:
        raise HTTPException(status_code=400, detail=f"Unsupported asset types: {', '.join(unknown_types)}")

    response = AssetResolveResponse(assets={})
    expiration = timedelta(minutes=15)
    
    for type_key in req.types:
        resolved = None
        
        if type_key == "audio":
            # GCS Path
            gcs_path = data.get("audioPath")
            
            # [FIX] usage: Auto-provision audioPath if missing so client can upload
            if not gcs_path:
                 gcs_path = f"sessions/{session_id}/audio.m4a" # Default path
                 # Update DB immediately so subsequent calls work and we have a record
                 doc_ref.update({"audioPath": gcs_path, "updatedAt": datetime.now(timezone.utc)})
                 logger.info(f"[/assets/resolve] Auto-provisioned audioPath: {gcs_path}")

            # If path is "imports/..." or "sessions/..."
            if gcs_path:
                blob = storage_client.bucket(AUDIO_BUCKET_NAME).blob(gcs_path)
                url = blob.generate_signed_url(
                    version="v4",
                    expiration=expiration,
                    method="PUT", # Assuming client needs to PUT (upload)
                )
                
                # Meta
                meta = data.get("audioMeta") or {}
                resolved = ResolvedAsset(
                    url=url,
                    expiresAt=datetime.now(timezone.utc) + expiration,
                    sha256=meta.get("payloadSha256"),
                    contentType=meta.get("container") and f"audio/{meta['container']}" or "audio/mp4"
                )
                
        # For Text Assets (Summary, Transcript, Quiz), we have a dilemma:
        # They are in Firestore strings. Client wants a URL.
        # Strategy: Return a "data:..." URI? Or a link to a new endpoint `/sessions/{id}/content/{type}`?
        # A Signed URL is best for caching. 
        # But we don't have GCS files for them yet.
        # Short-term solution for "Rebuild": 
        # Create a TEMPORARY GCS file? No, that's slow.
        # Return a custom URL scheme? `classnote-api://...`?
        # Actually, let's implement a wrapper endpoint `GET /sessions/{id}/content/{type}` that streams the content.
        # And return THAT url in resolve.
        # BUT `resolve` is supposed to give a cachable, signed URL.
        #
        # Better Strategy for "Rebuild" (Serverless-friendly):
        # 1. Use the new `/sessions/{id}/artifacts/{type}` endpoint but with a token?
        # 2. Or just return the content in a separate field? (Manifest/Resolve distinct)
        # 
        # User Requirement: "resolve -> URL"
        # "GET /sessions/{id}/assets/resolve -> { 'summary': { 'url': '...' } }"
        #
        # Let's use a proxy endpoint for Firestore content that validates specific short-term token?
        # Or simpler: Just return the `/sessions/{id}/artifacts/{type}` URL and let client use Auth Header?
        # "resolve" implies getting a direct download link.
        # If we return `https://api.../sessions/{id}/artifacts/summary`, client can DL it.
        # It's not a "Sign GCS URL", but it works.
        elif type_key in ["summary", "quiz", "transcript"]:
             # Check availability
             # (Reuse logic or just construct URL)
                  # Use CLOUD_RUN_SERVICE_URL from environment for self-referencing URLs
                  service_url = os.environ.get("CLOUD_RUN_SERVICE_URL", "https://api.deepnote.app")
                  resolved = ResolvedAsset(
                      url=f"{service_url}/sessions/{session_id}/artifacts/{type_key}?format=json",
                      contentType="application/json",
                      version=1
                  )
             
        if resolved:
            response.assets[type_key] = resolved
            
    return response


@router.post("/sessions/{session_id}/assets/{asset_type}/ensure")
async def ensure_asset_generation(
    session_id: str, 
    asset_type: str, 
    current_user: User = Depends(get_current_user)
):
    """
    Idempotent generation trigger.
    If asset is missing or failed, triggers generation task.
    If ready or processing, does nothing.
    """
    doc_ref = _session_doc_ref(session_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(404, "Session not found")
    data = doc.to_dict()
    ensure_is_owner(data, current_user.uid, session_id) # Owners only for generation
    
    # Check current status
    # We can use the helper _get_asset_item_from_derived logic, but simpler to check derived doc directly?
    # Use helper to handle legacy fields correctly.
    
    derived_refs = [
        _derived_doc_ref(session_id, "summary"),
        _derived_doc_ref(session_id, "quiz"),
        _derived_doc_ref(session_id, "playlist"),
        _derived_doc_ref(session_id, "explain"),
    ]
    derived_snaps = db.get_all(derived_refs)
    derived_map = {s.id: s.to_dict() for s in derived_snaps if s.exists}
    
    item = _get_asset_item_from_derived(session_id, asset_type, data, derived_map)
    
    current_status = item.status if item else AssetStatus.MISSING
    
    if current_status in (AssetStatus.READY, AssetStatus.PROCESSING):
        return {"status": "skipped", "current": current_status}
        
    # Trigger Logic
    if asset_type == "summary":
        enqueue_summarize_task(session_id, user_id=current_user.uid)
    elif asset_type == "quiz":
        enqueue_quiz_task(session_id, user_id=current_user.uid)
    elif asset_type == "playlist":
        enqueue_playlist_task(session_id, user_id=current_user.uid)
    elif asset_type == "explain":
        enqueue_explain_task(session_id, user_id=current_user.uid)
    else:
        raise HTTPException(400, f"Unsupported asset type for ensure: {asset_type}")
        
    return {"status": "enqueued", "type": asset_type}

# We need a Transcript Artifact endpoint to match other artifacts
@router.get("/sessions/{session_id}/artifacts/transcript", response_model=DerivedStatusResponse)
async def get_artifact_transcript(session_id: str, current_user: User = Depends(get_current_user)):
    """Bridge endpoint for Transcript as Artifact."""
    doc_ref = _session_doc_ref(session_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(404, "Session not found")
    data = doc.to_dict()
    ensure_can_view(data, current_user.uid, session_id)
    
    text = data.get("transcriptText")
    if not text:
         return DerivedStatusResponse(status=JobStatus.PENDING) # or MISSING
         
    return DerivedStatusResponse(
        status=JobStatus.COMPLETED,
        result={"transcript": text},
        updatedAt=data.get("updatedAt")
    )
