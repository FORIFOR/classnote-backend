from __future__ import annotations

# Map Product IDs to Plan Names
# WARNING: Product IDs must match App Store Connect
PRODUCT_TO_PLAN = {
    # --- Apple ---
    "cnx.standard.monthly": "basic",
    "cnx.standard.yearly": "basic",
    "cnx.premium.monthly": "premium",
    "cnx.premium.yearly": "premium",
    
    # Legacy/Previous IDs if any
    "com.classnote.app.standard.monthly": "basic",
    "com.classnote.app.standard.yearly": "basic",
    "com.classnote.app.premium.monthly": "premium",
    "com.classnote.app.premium.yearly": "premium",

    # --- Stripe (Future) ---
    "price_basic_monthly": "basic",
    "price_premium_monthly": "premium",
}

PLAN_RANK = {
    "free": 0, 
    "basic": 1, 
    "premium": 2, 
    "pro": 2  # Alias for premium
}

def plan_from_product_id(product_id: str | None) -> str:
    if not product_id:
        return "free"
    plan = PRODUCT_TO_PLAN.get(product_id)
    if not plan:
        # Fallback heuristic if specific ID not found but follows pattern
        lowered = product_id.lower()
        if "premium" in lowered or "pro" in lowered:
            return "premium"
        if "standard" in lowered or "basic" in lowered:
            return "basic"
        return "free"
    return plan

def max_plan(plans: list[str]) -> str:
    """Returns the highest rank plan from a list of plans."""
    best = "free"
    for p in plans:
        if PLAN_RANK.get(p, 0) > PLAN_RANK.get(best, 0):
            best = p
    return best
