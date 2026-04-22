"""Cost-aware write-side for /usage_events.

Wraps cost_calculator.calc_total_cost() and persists an enriched event
row to Firestore. Existing `usage_logger.log()` (app/services/usage.py)
remains the primary quota/inflight path; this module is additive and
only writes to `/usage_events` with a `costBreakdown` / `estimatedCostUsd`
payload so the cost dashboard can query per-request granularity.

Design choices:

- **Additive**: doesn't remove or change anything in usage.py. Phase 2
  will insert call sites in gemini_chat / assist / summarize workers
  that call BOTH the legacy logger and this metering helper.

- **Never raises**: cost metering failing (e.g. Firestore transient)
  must never break the user-visible request. All errors swallowed
  after a warn log.

- **Best-effort per-model split**: LLM calls get the model name so the
  dashboard can split gemini-2.0 vs 2.5 spend.

- **Clock**: createdAt uses server-side ISO; callers don't have to
  fight timezone conversion.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from google.cloud import firestore

from app.firebase import db
from app.services.cost_calculator import (
    CloudRunUsage,
    CostBreakdown,
    FirestoreUsage,
    SttUsage,
    StorageUsage,
    VertexUsage,
    calc_total_cost,
)
from app.services.cost_pricing import get_usd_jpy_rate


logger = logging.getLogger(__name__)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_usage_event(
    *,
    user_id: str,
    account_id: Optional[str],
    feature: str,
    service: str,
    request_id: Optional[str] = None,
    session_id: Optional[str] = None,
    region: Optional[str] = None,
    model: Optional[str] = None,
    status: str = "success",
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    duration_ms: Optional[int] = None,
    vertex_usage: Optional[VertexUsage] = None,
    firestore_usage: Optional[FirestoreUsage] = None,
    cloud_run_usage: Optional[CloudRunUsage] = None,
    storage_usage: Optional[StorageUsage] = None,
    stt_usage: Optional[SttUsage] = None,
    usd_jpy_rate: Optional[float] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Persist a cost-enriched `/usage_events/{eventId}` doc.

    Returns the written payload (for tests / callers that want to log
    the estimated cost), or None if the write failed.
    """
    try:
        breakdown: CostBreakdown = calc_total_cost(
            vertex=vertex_usage,
            firestore=firestore_usage,
            cloud_run=cloud_run_usage,
            storage=storage_usage,
            stt=stt_usage,
        )

        now_iso = _iso_now()
        event_id = str(uuid.uuid4())

        payload: Dict[str, Any] = {
            "eventId": event_id,
            "userId": user_id,
            "accountId": account_id,
            "sessionId": session_id,
            "requestId": request_id,
            "feature": feature,
            "service": service,
            "provider": "google_cloud",
            "region": region,
            "model": model,
            "status": status,
            "startedAt": started_at or now_iso,
            "finishedAt": finished_at or now_iso,
            "durationMs": duration_ms,
            "billable": {
                "inputTokens":      vertex_usage.input_tokens if vertex_usage else 0,
                "outputTokens":     vertex_usage.output_tokens if vertex_usage else 0,
                "groundedPrompts":  vertex_usage.grounded_prompts if vertex_usage else 0,
                "groundedPromptsOverFree": vertex_usage.grounded_prompts_over_free if vertex_usage else 0,
                "documentReads":    firestore_usage.document_reads if firestore_usage else 0,
                "documentWrites":   firestore_usage.document_writes if firestore_usage else 0,
                "documentDeletes":  firestore_usage.document_deletes if firestore_usage else 0,
                "vcpuSecondsEst":   cloud_run_usage.vcpu_seconds_est if cloud_run_usage else 0.0,
                "gibSecondsEst":    cloud_run_usage.gib_seconds_est if cloud_run_usage else 0.0,
                "requestCount":     cloud_run_usage.request_count if cloud_run_usage else 0,
                "storageGiBHours":  storage_usage.storage_gib_hours if storage_usage else 0.0,
                "classAOps":        storage_usage.class_a_ops if storage_usage else 0,
                "classBOps":        storage_usage.class_b_ops if storage_usage else 0,
                "egressGiB":        storage_usage.egress_gib if storage_usage else 0.0,
                "sttMinutes":       stt_usage.standard_minutes if stt_usage else 0.0,
            },
            "estimatedCostUsd": round(breakdown.total_usd, 8),
            "estimatedCostJpy": round(breakdown.total_jpy(usd_jpy=usd_jpy_rate), 4),
            "usdJpyRate":       get_usd_jpy_rate(usd_jpy_rate),
            "costBreakdown":    breakdown.to_dict(),
            "createdAt":        firestore.SERVER_TIMESTAMP,
            "dateKey":          now_iso[:10],   # "2026-04-17" — useful for daily aggregation queries
        }
        if extra:
            payload["extra"] = extra

        db.collection("usage_events").document(event_id).set(payload)
        return payload
    except Exception as e:
        logger.warning(f"[usage_metering] record_usage_event failed: {e}")
        return None
