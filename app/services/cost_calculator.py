"""Per-request cost calculator.

Given the billable inputs for a single API call / background job, returns
a CostBreakdown (USD). Callers typically construct one or more of:

  VertexUsage        — LLM tokens / grounding
  FirestoreUsage     — document reads / writes / deletes
  CloudRunUsage      — vCPU / GiB / request counts
  StorageUsage       — GCS GiB-hours + class A/B ops
  SttUsage           — STT minutes

then pass them to `calc_total_cost()`. The engine is pure: no I/O, no
async. All prices come from cost_pricing (which Phase 5 will refresh
from BigQuery billing export).

Why pure:
  - Trivially unit-testable
  - Usable both in the hot request path (per-event metering) and in
    batch aggregation jobs (aggregate_daily_usage rewrite)
  - Cost must be re-computable later if prices change (reconciliation)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.services.cost_pricing import (
    VERTEX_GROUNDED_PROMPT_PER_1K,
    CLOUD_RUN_PRICES,
    FIRESTORE_PRICES,
    STORAGE_PRICES,
    STT_PRICES,
    USDJPY_FALLBACK,
    get_vertex_price,
    get_usd_jpy_rate,
)


@dataclass
class VertexUsage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    # Number of generate_content calls that used Google Search grounding.
    # The per-project free tier is applied at reconciliation time — we
    # always bill the count; set `grounded_prompts_over_free` if you know
    # the free tier was already consumed for this project+day.
    grounded_prompts: int = 0
    grounded_prompts_over_free: int = 0


@dataclass
class FirestoreUsage:
    document_reads: int = 0
    document_writes: int = 0
    document_deletes: int = 0


@dataclass
class CloudRunUsage:
    vcpu_seconds_est: float = 0.0
    gib_seconds_est: float = 0.0
    request_count: int = 0


@dataclass
class StorageUsage:
    storage_gib_hours: float = 0.0
    class_a_ops: int = 0
    class_b_ops: int = 0
    egress_gib: float = 0.0


@dataclass
class SttUsage:
    # Cloud STT minutes charged. Pure on-device STT (Sherpa) should be 0.
    standard_minutes: float = 0.0


@dataclass
class CostBreakdown:
    vertex_usd: float = 0.0
    firestore_usd: float = 0.0
    cloud_run_usd: float = 0.0
    storage_usd: float = 0.0
    stt_usd: float = 0.0
    # Per-model detail (keyed by model name) so dashboards can split gemini-2.0 vs 2.5
    per_model_usd: dict = field(default_factory=dict)

    @property
    def total_usd(self) -> float:
        return (
            self.vertex_usd
            + self.firestore_usd
            + self.cloud_run_usd
            + self.storage_usd
            + self.stt_usd
        )

    def total_jpy(self, usd_jpy: Optional[float] = None) -> float:
        return self.total_usd * get_usd_jpy_rate(usd_jpy)

    def to_dict(self) -> dict:
        return {
            "vertexUsd":    round(self.vertex_usd, 8),
            "firestoreUsd": round(self.firestore_usd, 8),
            "cloudRunUsd":  round(self.cloud_run_usd, 8),
            "storageUsd":   round(self.storage_usd, 8),
            "sttUsd":       round(self.stt_usd, 8),
            "perModelUsd":  {k: round(v, 8) for k, v in self.per_model_usd.items()},
        }


# ---------------------------------------------------------------------------
# Per-service calculators (testable in isolation)
# ---------------------------------------------------------------------------


def calc_vertex_cost(usage: VertexUsage) -> float:
    price = get_vertex_price(usage.model)
    tokens = (
        (usage.input_tokens / 1_000_000.0) * price.input_per_1m
        + (usage.output_tokens / 1_000_000.0) * price.output_per_1m
    )
    grounding = (max(0, usage.grounded_prompts_over_free) / 1000.0) * VERTEX_GROUNDED_PROMPT_PER_1K
    return round(tokens + grounding, 8)


def calc_firestore_cost(usage: FirestoreUsage) -> float:
    return round(
        (usage.document_reads   / 100_000.0) * FIRESTORE_PRICES["document_read_per_100k"]
        + (usage.document_writes  / 100_000.0) * FIRESTORE_PRICES["document_write_per_100k"]
        + (usage.document_deletes / 100_000.0) * FIRESTORE_PRICES["document_delete_per_100k"],
        8,
    )


def calc_cloud_run_cost(usage: CloudRunUsage) -> float:
    return round(
        usage.vcpu_seconds_est * CLOUD_RUN_PRICES["vcpu_second"]
        + usage.gib_seconds_est * CLOUD_RUN_PRICES["gib_second"]
        + (usage.request_count / 1_000_000.0) * CLOUD_RUN_PRICES["request_per_million"],
        8,
    )


def calc_storage_cost(usage: StorageUsage) -> float:
    return round(
        usage.storage_gib_hours * STORAGE_PRICES["standard_gib_hour"]
        + (usage.class_a_ops / 1000.0) * STORAGE_PRICES["class_a_per_1000"]
        + (usage.class_b_ops / 1000.0) * STORAGE_PRICES["class_b_per_1000"]
        + usage.egress_gib * STORAGE_PRICES["egress_gib"],
        8,
    )


def calc_stt_cost(usage: SttUsage) -> float:
    return round(usage.standard_minutes * STT_PRICES["standard_per_minute"], 8)


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


def calc_total_cost(
    *,
    vertex: Optional[VertexUsage] = None,
    firestore: Optional[FirestoreUsage] = None,
    cloud_run: Optional[CloudRunUsage] = None,
    storage: Optional[StorageUsage] = None,
    stt: Optional[SttUsage] = None,
) -> CostBreakdown:
    breakdown = CostBreakdown(
        vertex_usd=calc_vertex_cost(vertex) if vertex else 0.0,
        firestore_usd=calc_firestore_cost(firestore) if firestore else 0.0,
        cloud_run_usd=calc_cloud_run_cost(cloud_run) if cloud_run else 0.0,
        storage_usd=calc_storage_cost(storage) if storage else 0.0,
        stt_usd=calc_stt_cost(stt) if stt else 0.0,
    )
    if vertex and vertex.model:
        breakdown.per_model_usd[vertex.model] = breakdown.vertex_usd
    return breakdown
