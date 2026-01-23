from datetime import datetime, timezone
from typing import Optional
from app.firebase import db
from app.services.plans import max_plan, PLAN_RANK

def compute_effective_plan_for_user(uid: str) -> str:
    """
    Calculates the user's effective plan based on the 'entitlements' ledger.
    Logic:
    1. Query active entitlements for this user (where status == 'active').
    2. Filter out any that are actually expired (if status wasn't updated in real-time).
    3. Return the highest rank plan.
    """
    now = datetime.now(timezone.utc)

    # Query: ownerUserId == uid AND status == 'active'
    # We rely on an index for this query. If index is missing, it might error.
    # PRO-TIP: We can try-catch or assume index exists.
    try:
        docs = db.collection("entitlements")\
            .where("ownerUserId", "==", uid)\
            .where("status", "==", "active")\
            .stream()
    except Exception as e:
        # Fallback or log error
        print(f"Error querying entitlements for {uid}: {e}")
        return "free"

    plans = []
    for d in docs:
        e = d.to_dict() or {}
        
        # Double check expiration just in case 'status' is stale
        # currentPeriodEnd is a Firestore Timestamp or None
        end_val = e.get("currentPeriodEnd")
        
        is_active = True
        if end_val:
            # If it's a datetime/Timestamp object
            if hasattr(end_val, "timestamp"):
                if end_val.timestamp() < now.timestamp():
                    is_active = False
            # If it's stored as string (shouldn't be, but robust check)
            elif isinstance(end_val, str):
                try:
                    dt = datetime.fromisoformat(end_val.replace("Z", "+00:00"))
                    if dt < now:
                        is_active = False
                except:
                    pass
            
        if is_active:
            plans.append(e.get("plan", "free"))

    return max_plan(plans)
