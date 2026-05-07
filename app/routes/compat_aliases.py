"""
compat_aliases.py — iOS / legacy client compatibility shim.

Reasons for each alias:
- iOS APIClient.swift uses hyphenated paths (`summary-v2:generate`,
  `summary-v2:feedback`, `quiz-attempts`) while the canonical handlers in
  this branch use underscored paths. Restore the hyphen variants by
  delegating to the canonical handlers.
- iOS expects `transcript_segments` under `/artifacts/` prefix; the canonical
  handler is registered at `/sessions/{id}/transcript_segments` only.
- iOS expects a `POST /sessions/{id}/playlist:generate` trigger that this
  branch lacks (the canonical generation path is internal). Implemented as
  a thin wrapper around `enqueue_playlist_task`.

These aliases are pure pass-through and do not introduce new business logic.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel

from app.dependencies import CurrentUser, get_current_user
from app.firebase import db

logger = logging.getLogger("app.routes.compat_aliases")

router = APIRouter(tags=["Compat Aliases"], include_in_schema=False)


# ---------------------------------------------------------------------------
# Summary v2 — hyphen aliases
# ---------------------------------------------------------------------------

@router.post("/sessions/{session_id}/artifacts/summary-v2:generate")
async def alias_summary_v2_generate(
    session_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Hyphen alias of POST /sessions/{id}/artifacts/summary_v2:generate (iOS compat)."""
    from app.routes.sessions import generate_summary_v2_endpoint
    from app.util_models import SummaryV2GenerateRequest

    raw = await request.body()
    body_dict = {} if not raw else (await request.json())
    body = SummaryV2GenerateRequest(**body_dict)
    return await generate_summary_v2_endpoint(
        session_id=session_id,
        body=body,
        background_tasks=background_tasks,
        current_user=current_user,
    )


@router.post("/sessions/{session_id}/artifacts/summary-v2:feedback")
async def alias_summary_v2_feedback(
    session_id: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Hyphen alias of POST /sessions/{id}/artifacts/summary_v2:feedback (iOS compat)."""
    from app.routes.sessions import submit_summary_v2_feedback
    from app.util_models import SummaryV2FeedbackRequest

    body_dict = await request.json()
    body = SummaryV2FeedbackRequest(**body_dict)
    return await submit_summary_v2_feedback(
        session_id=session_id,
        body=body,
        current_user=current_user,
    )


# ---------------------------------------------------------------------------
# Quiz attempts — hyphen alias
# ---------------------------------------------------------------------------

@router.post("/sessions/{session_id}/quiz-attempts")
async def alias_quiz_attempts(
    session_id: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Hyphen alias of POST /sessions/{id}/quiz_attempts (iOS compat)."""
    from app.routes.quiz_analytics import create_quiz_attempt
    from app.util_models import QuizAttemptCreate

    body_dict = await request.json()
    attempt = QuizAttemptCreate(**body_dict)
    return await create_quiz_attempt(
        session_id=session_id,
        attempt=attempt,
        current_user=current_user,
    )


# ---------------------------------------------------------------------------
# Transcript segments — artifacts/ prefix alias
# ---------------------------------------------------------------------------

@router.get("/sessions/{session_id}/artifacts/transcript_segments")
async def alias_artifacts_transcript_segments(
    session_id: str,
    fromMs: Optional[int] = None,
    toMs: Optional[int] = None,
    limit: int = 100,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Alias for /sessions/{id}/transcript_segments under /artifacts/ prefix (iOS compat)."""
    from app.routes.sessions import get_transcript_segments
    return await get_transcript_segments(
        session_id=session_id,
        fromMs=fromMs,
        toMs=toMs,
        limit=limit,
        current_user=current_user,
    )


# ---------------------------------------------------------------------------
# Playlist generate — new thin wrapper
# ---------------------------------------------------------------------------

class PlaylistGenerateRequest(BaseModel):
    force: bool = False
    idempotencyKey: Optional[str] = None


class PlaylistGenerateResponse(BaseModel):
    status: str
    jobId: str
    statusUrl: str


@router.post("/sessions/{session_id}/playlist:generate", response_model=PlaylistGenerateResponse)
async def alias_playlist_generate(
    session_id: str,
    body: PlaylistGenerateRequest = PlaylistGenerateRequest(),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Trigger playlist (aiMarkers) generation as an async Cloud Tasks job."""
    from app.task_queue import enqueue_playlist_task

    # Resolve + ownership check
    doc_ref = db.collection("sessions").document(session_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Session not found")
    data = doc.to_dict() or {}
    owner_uid = data.get("ownerUid") or data.get("ownerUserId") or data.get("userId")
    owner_account = data.get("ownerAccountId")
    if owner_uid != current_user.uid and owner_account != current_user.account_id:
        raise HTTPException(status_code=403, detail="Not authorized")

    # Idempotency: if a playlist generation is already running and force is False, reuse
    derived_ref = doc_ref.collection("derived").document("playlist")
    derived_snap = derived_ref.get()
    if derived_snap.exists and not body.force:
        derived_data = derived_snap.to_dict() or {}
        running_status = derived_data.get("status")
        if running_status in ("running", "queued", "succeeded", "completed"):
            existing_job = derived_data.get("jobId") or ""
            return PlaylistGenerateResponse(
                status=("already_completed" if running_status in ("succeeded", "completed") else "already_running"),
                jobId=existing_job,
                statusUrl=f"/jobs/{existing_job}" if existing_job else "",
            )

    job_id = f"playlist_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)

    derived_ref.set({
        "status": "queued",
        "jobId": job_id,
        "updatedAt": now,
    }, merge=True)

    try:
        enqueue_playlist_task(session_id, user_id=current_user.uid, job_id=job_id)
        logger.info(f"[playlist:generate] enqueued job {job_id} for session {session_id}")
    except Exception as e:
        logger.error(f"[playlist:generate] enqueue failed: {e}")
        derived_ref.set({"status": "failed", "errorReason": str(e), "updatedAt": now}, merge=True)
        raise HTTPException(status_code=500, detail="Failed to enqueue playlist task")

    return PlaylistGenerateResponse(
        status="queued",
        jobId=job_id,
        statusUrl=f"/jobs/{job_id}",
    )


# ---------------------------------------------------------------------------
# [HOTFIX 2026-05-05] iOS endpoint alignment — these are the paths the
# current ClassnoteX iOS build calls but that aren't registered server-side.
# Each alias either delegates to an existing canonical handler or returns a
# minimal no-op shape so the client's session-detail flow doesn't error out
# (which the user reported as "セッションの同期もうまく行ってない").
# ---------------------------------------------------------------------------

def _tombstone_response(session_id: str, extra: Optional[dict] = None) -> dict:
    """Return-shape used when a session-scoped GET is hit for a session
    that no longer exists server-side. iOS used to receive 404 here and
    spin on its sync coordinator — returning 200 with this body lets the
    client drop the local-DB ghost id once it ships a tombstone-aware
    build, while gracefully no-op'ing for current builds."""
    out = {"sessionId": session_id, "tombstone": True, "sessionExists": False}
    if extra:
        out.update(extra)
    return out


# /sessions/{id}/entity-review — iOS omits the /v1 prefix. Delegate to the
# canonical handler in app/routes/entity_review.py.
@router.get("/sessions/{session_id}/entity-review", include_in_schema=False)
async def alias_entity_review_get(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    from app.routes.entity_review import get_entity_review
    try:
        return await get_entity_review(session_id, current_user)
    except HTTPException as e:
        if e.status_code == 404:
            return _tombstone_response(session_id, {"review": None, "candidates": []})
        raise


# /sessions/{id}/reactions (plural) — canonical is /reaction (singular).
@router.get("/sessions/{session_id}/reactions", include_in_schema=False)
async def alias_reactions_get(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    from app.routes.reactions import get_reaction_state
    try:
        return await get_reaction_state(session_id, current_user)
    except HTTPException as e:
        if e.status_code == 404:
            return _tombstone_response(session_id, {"reactions": []})
        raise
    except Exception:
        return {"sessionId": session_id, "reactions": []}


@router.put("/sessions/{session_id}/reactions", include_in_schema=False)
async def alias_reactions_put(
    session_id: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    """iOS APIClient.swift writes ``PUT /sessions/{id}/reactions`` (plural).
    Canonical handler is ``PUT /sessions/{id}/reaction`` (singular). Forward
    the JSON body so the user's emoji actually persists instead of 404'ing.
    """
    from app.routes.reactions import set_reaction, SetReactionRequest
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    try:
        req = SetReactionRequest(**(payload if isinstance(payload, dict) else {}))
    except Exception:
        req = SetReactionRequest(emoji=None)
    return await set_reaction(session_id, req, current_user)


# /accounts/merges (REST plural) → /accounts/merge:start (RPC colon).
# iOS APIClient.swift posts to "accounts/merges" / "accounts/merges/{id}:commit".
@router.post("/accounts/merges", include_in_schema=False)
async def alias_accounts_merges_start(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
):
    from app.routes.account import start_merge, MergeStartRequest
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        req = MergeStartRequest(**payload)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid MergeStartRequest body")
    return start_merge(req, user=user)


@router.post("/accounts/merges/{merge_id}:commit", include_in_schema=False)
async def alias_accounts_merges_commit(
    merge_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    """iOS embeds the mergeJobId in the path; canonical endpoint expects
    ``{"mergeJobId": ...}`` in the body. Re-shape and delegate."""
    from app.routes.account import commit_merge, MergeCommitRequest
    return commit_merge(MergeCommitRequest(mergeJobId=merge_id), user=user)


# /accounts/migrations (REST plural) → /account/migrate (RPC singular).
@router.post("/accounts/migrations", include_in_schema=False)
async def alias_accounts_migrations(
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    """The canonical handler declares ``new_uid: str = Depends(...)`` but
    that dependency actually yields a ``CurrentUser`` — passing the bare
    object into Firestore ``.document()`` would fail at runtime. We
    extract ``.uid`` here so the migration query keys correctly while
    leaving the canonical signature alone.
    """
    from app.routes.account import migrate, MigrateReq
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        req = MigrateReq(**payload)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid MigrateReq body")
    return migrate(req, new_uid=current_user.uid)


# /sessions/search → /search/sessions. iOS appends q/mode/tag/from_date/to_date/limit
# query parameters; we forward them by re-invoking the canonical handler with
# the same query string.
@router.get("/sessions/search", include_in_schema=False)
async def alias_sessions_search(
    request: Request,
    q: Optional[str] = None,
    mode: Optional[str] = None,
    tag: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    limit: int = 20,
    current_user: CurrentUser = Depends(get_current_user),
):
    from app.routes.search import search_sessions
    return await search_sessions(
        request=request,
        q=q,
        mode=mode,
        tag=tag,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        current_user=current_user,
    )


# /sessions/{id}/organization — not implemented server-side. Return an
# empty-but-valid shape so iOS treats it as "no organization linked" and
# proceeds with the rest of the sync.
@router.get("/sessions/{session_id}/organization", include_in_schema=False)
async def alias_organization_get(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    return {"sessionId": session_id, "organization": None, "members": []}


@router.put("/sessions/{session_id}/organization", include_in_schema=False)
async def alias_organization_put(
    session_id: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    """iOS' ``updateSessionOrganization`` writes ``{folderId: "..."}`` here
    to attach a session to a folder. The previous stub returned
    ``{ok: true, stored: false}`` without persisting anything, so every
    folder assignment from iOS silently disappeared and the folder
    appeared empty.

    This now delegates to ``_move_session_impl`` which writes
    ``users/{uid}/sessionMeta/{session_id}.folderId`` — the same path
    ``GET /folders/{id}/sessions`` reads from.
    """
    # Parse the body manually — using ``body: Any = None`` does not actually
    # bind the JSON payload in FastAPI for this endpoint shape, so a previous
    # version always saw body=None and treated every PUT as "clear folder".
    folder_id: Optional[str] = None
    try:
        payload = await request.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        raw_fid = payload.get("folderId")
        if isinstance(raw_fid, str) and raw_fid:
            folder_id = raw_fid
        # Explicit ``{folderId: null}`` is the unset signal — folder_id stays None.
    from app.routes.folders import _move_session_impl
    try:
        result = _move_session_impl(current_user.uid, session_id, folder_id)
        return {
            "sessionId": session_id,
            "ok": True,
            "stored": True,
            "folderId": result.get("folderId") if isinstance(result, dict) else folder_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        return {"sessionId": session_id, "ok": False, "stored": False, "error": str(e)[:200]}


# /sessions/{id}/audio?purpose=playback — iOS expects a GET to receive a
# playback URL. Canonical handler is GET /sessions/{id}/audio_url.
@router.get("/sessions/{session_id}/audio", include_in_schema=False)
async def alias_audio_get(
    session_id: str,
    purpose: str = "playback",
    current_user: CurrentUser = Depends(get_current_user),
):
    from app.routes.sessions import get_audio_url
    try:
        return await get_audio_url(session_id, purpose, current_user)
    except HTTPException as e:
        if e.status_code == 404:
            return _tombstone_response(session_id, {"audioUrl": None})
        raise


# /sessions/{id}/participants_users — iOS calls this; canonical handler may
# raise 404 for ghost ids. Soft-tombstone same as above.
@router.get("/sessions/{session_id}/participants_users", include_in_schema=False)
async def alias_participants_users_get(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        from app.routes.sessions import get_participants_users
        return await get_participants_users(session_id, current_user)
    except (HTTPException,) as e:
        if e.status_code == 404:
            return _tombstone_response(session_id, {"participants": []})
        raise
    except Exception:
        return {"sessionId": session_id, "participants": []}


# /v1/sessions:reconcile-cache — iOS posts a list of local session IDs and
# gets back which are still valid vs deleted/missing. Lets a future iOS
# build scrub its ghost cache in one round-trip without hammering 404
# polls. Server returns deleted/missing IDs grouped so the client can
# decide whether to rehydrate or drop.
class _ReconcileCacheRequest(BaseModel):
    sessionIds: list[str]


@router.post("/v1/sessions:reconcile-cache", include_in_schema=False)
async def reconcile_session_cache(
    body: _ReconcileCacheRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    valid: list[str] = []
    deleted: list[str] = []
    missing: list[str] = []
    for sid in (body.sessionIds or [])[:500]:
        try:
            ref = db.collection("sessions").document(sid)
            snap = ref.get()
            if not snap.exists:
                missing.append(sid)
                continue
            data = snap.to_dict() or {}
            if data.get("deletedAt"):
                deleted.append(sid)
                continue
            owner_account = data.get("ownerAccountId")
            owner_uid = data.get("ownerUid") or data.get("ownerUserId")
            if owner_account and owner_account != getattr(current_user, "account_id", None):
                missing.append(sid)
                continue
            if not owner_account and owner_uid and owner_uid != current_user.uid:
                missing.append(sid)
                continue
            valid.append(sid)
        except Exception:
            missing.append(sid)
    return {
        "valid": valid,
        "deleted": deleted,
        "missing": missing,
        "tombstones": deleted + missing,
        "checkedAt": datetime.now(timezone.utc).isoformat(),
    }


# /system/* — iOS app config endpoints. Provide minimal stubs so the app
# can boot without 404 popups; real values come from /v1/app_config later.
@router.get("/system/config", include_in_schema=False)
async def alias_system_config(platform: Optional[str] = None):
    return {
        "platform": platform or "ios",
        "maintenance": False,
        "minSupportedVersion": "1.0",
        "features": {},
    }


@router.get("/system/status", include_in_schema=False)
async def alias_system_status(platform: Optional[str] = None):
    """Lightweight system status probe used by iOS AppConfigStore.

    Shape MUST match ``ClassnoteX/Core/AppConfig.swift::SystemStatus``.
    The previous "{status,platform}" stub failed iOS Decodable with
    ``key 'mode' not found`` on every cold start, leaving the app on
    cached SystemStatus indefinitely.

    Mode source of truth is the ``SYSTEM_STATUS_MODE`` env var so SRE
    can flip maintenance / force_update / degraded without redeploying
    code (`gcloud run services update --update-env-vars`). Optional
    detail fields are also env-driven; unset → null in the response.
    """
    import os
    valid_modes = {"normal", "notice", "degraded", "maintenance", "force_update"}
    mode = (os.environ.get("SYSTEM_STATUS_MODE") or "normal").strip().lower()
    if mode not in valid_modes:
        mode = "normal"

    def _opt(key: str) -> Optional[str]:
        v = os.environ.get(key)
        return v if v else None

    def _int(key: str) -> Optional[int]:
        v = os.environ.get(key)
        try:
            return int(v) if v else None
        except ValueError:
            return None

    affected_raw = os.environ.get("SYSTEM_STATUS_AFFECTED")
    affected = (
        [a.strip() for a in affected_raw.split(",") if a.strip()]
        if affected_raw else None
    )

    cta_label = os.environ.get("SYSTEM_STATUS_CTA_LABEL")
    cta_url = os.environ.get("SYSTEM_STATUS_CTA_URL")
    cta = {"label": cta_label, "url": cta_url} if cta_label and cta_url else None

    return {
        "mode": mode,
        "title": _opt("SYSTEM_STATUS_TITLE"),
        "message": _opt("SYSTEM_STATUS_MESSAGE"),
        "startsAt": _opt("SYSTEM_STATUS_STARTS_AT"),
        "endsAt": _opt("SYSTEM_STATUS_ENDS_AT"),
        "etaMinutes": _int("SYSTEM_STATUS_ETA_MINUTES"),
        "affected": affected,
        "minAppVersion": _opt("SYSTEM_STATUS_MIN_APP_VERSION"),
        "cta": cta,
        "retryAfterSec": _int("SYSTEM_STATUS_RETRY_AFTER_SEC"),
    }


@router.get("/system/orb-theme", include_in_schema=False)
async def alias_system_orb_theme():
    from app.routes.orb_theme import get_orb_theme
    return await get_orb_theme(uid=None, plan=None, current_user=None)


# /users/bootstrap — iOS app init call. Response shape MUST match
# ``ClassnoteX/SessionModels.swift::BootstrapResponse`` so AuthCoordinator's
# Decodable succeeds. The previous "{ok,user,account,...}" stub failed
# with ``key 'plan' not found`` on every cold start and put the endpoint
# on a 2s cooldown via the iOS APIClient failure tracker.
@router.post("/users/bootstrap", include_in_schema=False)
async def alias_users_bootstrap(
    body: Any = None,
    current_user: CurrentUser = Depends(get_current_user),
):
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    uid = current_user.uid
    account_id = getattr(current_user, "account_id", None) or uid
    account_data: dict = {}
    user_data: dict = {}
    try:
        acc_snap = db.collection("accounts").document(account_id).get()
        if acc_snap.exists:
            account_data = acc_snap.to_dict() or {}
    except Exception:
        pass
    try:
        u_snap = db.collection("users").document(uid).get()
        if u_snap.exists:
            user_data = u_snap.to_dict() or {}
    except Exception:
        pass

    raw_plan = account_data.get("plan") or user_data.get("plan") or "free"
    if raw_plan in ("basic", "standard"):
        plan = "basic"
    elif raw_plan == "premium":
        plan = "premium"
    else:
        plan = "free"

    suspended = bool(account_data.get("suspended", False))

    token_provider = getattr(current_user, "provider", None)
    token_phone = getattr(current_user, "phone_number", None)
    phone_in_db = user_data.get("phoneE164")
    verified_sns_providers = {"google.com", "apple.com", "custom", "line"}
    is_sns_verified = token_provider in verified_sns_providers

    needs_phone = (not token_phone and not phone_in_db and not is_sns_verified)
    needs_sns = False
    if token_provider == "phone":
        providers_in_db = set(user_data.get("providers", []))
        if not any(p in providers_in_db for p in verified_sns_providers):
            needs_sns = True

    cache_valid_until = (_dt.now(_tz.utc) + _td(minutes=5)).isoformat()

    return {
        # Identity
        "uid": uid,
        "accountId": account_id,
        "plan": plan,
        # Profile
        "displayName": user_data.get("displayName")
            or account_data.get("displayName")
            or getattr(current_user, "display_name", None)
            or "User",
        "username": user_data.get("username"),
        "hasUsername": bool(user_data.get("hasUsername", False)),
        "photoUrl": user_data.get("photoURL")
            or getattr(current_user, "photo_url", None),
        "provider": token_provider,
        "providers": user_data.get("providers", []),
        # Feature gates (plan-based; usage gating happens at request time)
        "featureGates": {
            "cloudStt": True,
            "summarization": True,
            "quiz": True,
            "cloudSync": True,
            "export": True,
            "share": True,
        },
        # Onboarding flags
        "needsPhoneVerification": needs_phone,
        "needsSnsLogin": needs_sns,
        "suspended": suspended,
        # Canonicalize result — bootstrap reports current state only;
        # actual merges still go through the dedicated /auth/canonicalize
        # endpoint so this never silently changes account membership.
        "canonicalized": False,
        "previousAccountId": None,
        # Custom claims
        "claimsRefreshRequired": False,
        # Async repair flags (bootstrap doesn't trigger repairs itself)
        "repairNeeded": False,
        "repairTasks": [],
        # Cache hint
        "cacheValidUntil": cache_valid_until,
    }
