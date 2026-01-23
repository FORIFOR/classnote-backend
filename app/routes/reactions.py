from fastapi import APIRouter, Depends, HTTPException
from google.cloud import firestore
from datetime import datetime, timezone
from app.firebase import db
from app.dependencies import get_current_user, CurrentUser, CurrentUser, ensure_can_view
from app.util_models import SetReactionRequest, ReactionStateResponse
from app.services.session_event_bus import publish_session_event

router = APIRouter()

ALLOWED = {"🔥", "👏", "😇", "🤯", "🫶"}

def _inc_counts(counts: dict, emoji: str, delta: int):
    counts = dict(counts or {})
    current_count = int(counts.get(emoji, 0))
    new_count = max(0, current_count + delta)
    counts[emoji] = new_count
    # Remove key if 0? Or keep it? Keeping it is safer for UI updates.
    return counts

@router.get("/sessions/{session_id}/reaction", response_model=ReactionStateResponse)
async def get_reaction_state(session_id: str, current_user: CurrentUser = Depends(get_current_user)):
    uid = current_user.uid

    sess_ref = db.collection("sessions").document(session_id)
    sess = sess_ref.get()
    if not sess.exists:
        raise HTTPException(status_code=404, detail="session not found")
    ensure_can_view(sess.to_dict() or {}, current_user, session_id)

    counts = (sess.to_dict().get("reactionCounts") or {})
    my_ref = sess_ref.collection("reactions").document(uid)
    my_doc = my_ref.get()
    my_emoji = my_doc.to_dict().get("emoji") if my_doc.exists else None

    # Fetch all reactions to build user-emoji map
    users_map = {}
    reacts = sess_ref.collection("reactions").stream()
    for r in reacts:
        r_data = r.to_dict()
        if r_data.get("emoji"):
            users_map[r.id] = r_data["emoji"]

    return ReactionStateResponse(myEmoji=my_emoji, counts=counts, users=users_map)

@router.put("/sessions/{session_id}/reaction", response_model=ReactionStateResponse)
async def set_reaction(
    session_id: str, 
    req: SetReactionRequest, 
    current_user: CurrentUser = Depends(get_current_user)
):
    uid = current_user.uid
    new_emoji = req.emoji

    if new_emoji is not None and new_emoji not in ALLOWED:
        raise HTTPException(status_code=400, detail="invalid emoji")

    sess_ref = db.collection("sessions").document(session_id)
    sess = sess_ref.get()
    if not sess.exists:
        raise HTTPException(status_code=404, detail="session not found")
    ensure_can_view(sess.to_dict() or {}, current_user, session_id)
    react_ref = sess_ref.collection("reactions").document(uid)

    @firestore.transactional
    def txn_update(txn):
        sess_snap = sess_ref.get(transaction=txn)
        if not sess_snap.exists:
            raise HTTPException(status_code=404, detail="session not found")

        sess_data = sess_snap.to_dict() or {}
        counts = sess_data.get("reactionCounts") or {}

        prev_snap = react_ref.get(transaction=txn)
        prev_emoji = prev_snap.to_dict().get("emoji") if prev_snap.exists else None

        # No change
        if prev_emoji == new_emoji:
            return prev_emoji, counts

        # Decrement previous
        if prev_emoji:
            counts = _inc_counts(counts, prev_emoji, -1)

        # Increment new or delete
        now = datetime.now(timezone.utc)
        if new_emoji:
            counts = _inc_counts(counts, new_emoji, +1)
            txn.set(react_ref, {"emoji": new_emoji, "updatedAt": now}, merge=True)
        else:
            # Delete reaction
            txn.delete(react_ref)

        txn.set(sess_ref, {"reactionCounts": counts, "reactionUpdatedAt": now}, merge=True)
        return new_emoji, counts

    transaction = db.transaction()
    try:
        my_emoji, counts = txn_update(transaction)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Transaction failed: {e}")

    await publish_session_event(session_id, "reactions.updated", {"userId": uid, "emoji": my_emoji})
        
    return ReactionStateResponse(myEmoji=my_emoji, counts=counts)
