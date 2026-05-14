from __future__ import annotations

# Map Product IDs to Plan Names
# WARNING: Product IDs must match App Store Connect
PRODUCT_TO_PLAN = {
    # --- Apple ---
    "cnx.standard.monthly": "basic",
    "cnx.standard.yearly": "basic",

    # Legacy/Previous IDs if any
    "com.classnote.app.standard.monthly": "basic",
    "com.classnote.app.standard.yearly": "basic",

    # --- Stripe (Future) ---
    "price_basic_monthly": "basic",
}

# Centralised plan ladder used for gate / capability comparisons.
#
# Tier ladder, lowest → highest:
#   free     — anonymous / unpaid
#   basic    — paid via Apple / Stripe consumer subscription
#   standard — paid via Stripe (currently mostly equivalent to basic for
#              feature gates but kept distinct so a future split can lift
#              one without touching the other)
#   business — paid via bulk-license redeem
#              (POST /v1/licenses:redeem → Phase 1 bulk-license flow)
#
# `pro` is intentionally absent — no Pro SKU exists in the live ladder.
# Add it here first if a Pro tier is ever introduced; downstream gates
# read this map via `has_plan_at_least`.
PLAN_RANK = {
    "free": 0,
    "basic": 1,
    "standard": 2,
    "business": 3,
}

def plan_from_product_id(product_id: str | None) -> str:
    if not product_id:
        return "free"
    plan = PRODUCT_TO_PLAN.get(product_id)
    if not plan:
        # Fallback heuristic if specific ID not found but follows pattern
        lowered = product_id.lower()
        if "standard" in lowered or "basic" in lowered:
            return "basic"
        return "free"
    return plan

def plan_rank(plan: str | None) -> int:
    """Numeric rank of `plan`. Unknown / missing tiers fall back to free."""
    return PLAN_RANK.get(plan or "free", 0)


def has_plan_at_least(current: str | None, required: str) -> bool:
    """True when `current` is at least as high as `required` on the
    plan ladder. Centralised so future tier insertions don't require
    revisiting every ad-hoc `plan in (...)` check.

    Examples:
        has_plan_at_least("business", "basic")    # True
        has_plan_at_least("standard", "business") # False
        has_plan_at_least(None, "basic")          # False
    """
    return plan_rank(current) >= plan_rank(required)


def choose_higher_plan(a: str | None, b: str | None) -> str:
    """Return whichever of the two plans has the higher rank (`a` wins
    ties). Useful when reconciling multiple entitlement sources — e.g.
    an Apple subscription `basic` and a redeemed bulk-license
    `business` should resolve to `business`.
    """
    a_norm = a or "free"
    b_norm = b or "free"
    return a_norm if plan_rank(a_norm) >= plan_rank(b_norm) else b_norm


def max_plan(plans: list[str]) -> str:
    """Returns the highest rank plan from a list of plans."""
    best = "free"
    for p in plans:
        if PLAN_RANK.get(p, 0) > PLAN_RANK.get(best, 0):
            best = p
    return best
