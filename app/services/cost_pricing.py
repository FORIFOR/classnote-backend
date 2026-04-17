"""Single source of truth for unit prices used by cost_calculator.

All values are **USD** unless suffixed. Numbers are approximations of the
public Google Cloud price sheet as of 2026-04; they are deliberately
conservative (upper bound) so that our live cost estimate never
under-reports what the monthly invoice shows. Phase 5 (`cost_reconcile`)
will load the real SKU prices from the BigQuery Cloud Billing pricing
export and override these defaults.

Why the numbers live in code:
  - Fast read path for every request (no Firestore round-trip)
  - Version control / diff review of price changes
  - cost_guard / ai_credits can import directly

Prices the cost engine understands:
  - Vertex AI Gemini (per-1M-token input / output)
  - Firestore document read / write / delete (per 100k)
  - Cloud Run vCPU-second / GiB-second / requests
  - Cloud Storage standard tier GiB-hour + class A/B ops
  - Speech-to-Text per-minute (for STT sessions)
  - USD/JPY fallback for display
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class VertexModelPrice:
    """Per-1-million-token prices."""
    input_per_1m: float
    output_per_1m: float


# ---------------------------------------------------------------------------
# Vertex AI — Gemini family  (USD per 1M tokens)
# ---------------------------------------------------------------------------

VERTEX_PRICES: Dict[str, VertexModelPrice] = {
    # Gemini 2.x Flash Lite — current default chat model
    "gemini-2.0-flash-lite": VertexModelPrice(input_per_1m=0.075, output_per_1m=0.30),
    "gemini-2.5-flash-lite": VertexModelPrice(input_per_1m=0.10,  output_per_1m=0.40),
    # Gemini 2.x Flash — summary / quiz / heavier tasks
    "gemini-2.5-flash":      VertexModelPrice(input_per_1m=0.15,  output_per_1m=0.60),
    # Gemini 2.x Pro (reserved for future premium tier)
    "gemini-2.5-pro":        VertexModelPrice(input_per_1m=1.25,  output_per_1m=5.00),
    # Fallback for unknown / legacy names
    "_unknown":              VertexModelPrice(input_per_1m=0.20,  output_per_1m=0.80),
}

# Grounded (Google Search) — per 1k grounded prompts after the free tier.
# Each `generate_content` call with google_search tool counts as 1 grounded prompt.
VERTEX_GROUNDED_PROMPT_PER_1K = 35.0
VERTEX_GROUNDED_FREE_TIER_PER_PROJECT_PER_DAY = 1500  # not enforced here; reconciliation will


def get_vertex_price(model: str) -> VertexModelPrice:
    """Lookup a model price; falls back to `_unknown` for safety."""
    return VERTEX_PRICES.get(model) or VERTEX_PRICES["_unknown"]


# ---------------------------------------------------------------------------
# Firestore (USD per 100k operations)
# ---------------------------------------------------------------------------

FIRESTORE_PRICES = {
    "document_read_per_100k":   0.03,
    "document_write_per_100k":  0.09,
    "document_delete_per_100k": 0.01,
    # Network egress (not counted in Phase 1; Phase 5 reconciliation only)
    "egress_gib":               0.12,
}

# ---------------------------------------------------------------------------
# Cloud Run 2nd-gen (asia-northeast1)
# ---------------------------------------------------------------------------

CLOUD_RUN_PRICES = {
    "vcpu_second":              0.000024,   # USD
    "gib_second":               0.0000025,
    "request_per_million":      0.40,
}

# ---------------------------------------------------------------------------
# Cloud Storage standard tier
# ---------------------------------------------------------------------------

STORAGE_PRICES = {
    "standard_gib_hour":        0.000030137,   # (monthly price / 30 / 24)
    "class_a_per_1000":         0.005,
    "class_b_per_1000":         0.0004,
    "egress_gib":               0.12,
}

# ---------------------------------------------------------------------------
# Speech-to-Text (Chirp / telephony — session grounded STT)
# ---------------------------------------------------------------------------

STT_PRICES = {
    "standard_per_minute":      0.024,
    # Per the current plan, on-device STT (Sherpa) has no Google Cloud cost.
}

# ---------------------------------------------------------------------------
# FX fallback  (used only when no live rate is stored in Firestore)
# ---------------------------------------------------------------------------

USDJPY_FALLBACK = 150.0


def get_usd_jpy_rate(stored: float | None) -> float:
    """Prefer stored rate; fall back to USDJPY_FALLBACK if missing or bogus."""
    if isinstance(stored, (int, float)) and 50.0 < stored < 500.0:
        return float(stored)
    return USDJPY_FALLBACK
