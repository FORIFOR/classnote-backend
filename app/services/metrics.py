"""
metrics.py - Metrics Service

Tracks application metrics and exports to Cloud Monitoring.
Uses Firestore for persistence and aggregation (can be replaced with Cloud Monitoring API).
"""

import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
from enum import Enum
from contextlib import contextmanager
from google.cloud import firestore

logger = logging.getLogger("app.metrics")


class MetricType(str, Enum):
    """Metric types for categorization."""
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


class MetricName(str, Enum):
    """Standard metric names."""
    # API Metrics
    API_REQUEST_COUNT = "api_request_count"
    API_REQUEST_LATENCY_MS = "api_request_latency_ms"
    API_ERROR_COUNT = "api_error_count"

    # Job Metrics
    JOB_QUEUED_COUNT = "job_queued_count"
    JOB_COMPLETED_COUNT = "job_completed_count"
    JOB_FAILED_COUNT = "job_failed_count"
    JOB_DURATION_SEC = "job_duration_sec"
    JOB_QUEUE_DEPTH = "job_queue_depth"

    # STT Metrics
    STT_REQUEST_COUNT = "stt_request_count"
    STT_DURATION_SEC = "stt_duration_sec"
    STT_ERROR_COUNT = "stt_error_count"

    # LLM Metrics
    LLM_REQUEST_COUNT = "llm_request_count"
    LLM_TOKENS_USED = "llm_tokens_used"
    LLM_ERROR_COUNT = "llm_error_count"

    # Rate Limit Metrics
    RATE_LIMIT_HIT_COUNT = "rate_limit_hit_count"
    QUOTA_EXCEEDED_COUNT = "quota_exceeded_count"


class MetricsService:
    """
    Metrics collection and aggregation service.

    Stores metrics in Firestore for persistence and dashboard queries.
    Aggregates by minute for time-series analysis.
    """

    _instance: Optional["MetricsService"] = None
    _db: Optional[firestore.Client] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def _get_db(self) -> firestore.Client:
        if self._db is None:
            project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
            self._db = firestore.Client(project=project_id)
        return self._db

    def _get_minute_key(self) -> str:
        """Get current minute as key (YYYY-MM-DD-HH-MM)."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M")

    def _get_hour_key(self) -> str:
        """Get current hour as key (YYYY-MM-DD-HH)."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d-%H")

    def increment(
        self,
        metric: MetricName,
        value: float = 1.0,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        Increment a counter metric.

        Args:
            metric: The metric name
            value: Value to increment by (default 1)
            labels: Optional labels for metric dimensions
        """
        try:
            db = self._get_db()
            minute_key = self._get_minute_key()
            metric_name = metric.value if isinstance(metric, MetricName) else metric

            # Build document ID with labels (sanitize values for Firestore)
            label_suffix = ""
            if labels:
                # Replace / with _ to avoid Firestore path issues
                safe_labels = {k: str(v).replace("/", "_").replace(".", "_") for k, v in labels.items()}
                label_suffix = "_" + "_".join(f"{k}:{v}" for k, v in sorted(safe_labels.items()))

            doc_id = f"{minute_key}_{metric_name}{label_suffix}"

            # Use increment for atomic updates
            doc_ref = db.collection("metrics_minutes").document(doc_id)
            doc_ref.set({
                "metric": metric_name,
                "type": MetricType.COUNTER.value,
                "minute": minute_key,
                "value": firestore.Increment(value),
                "labels": labels or {},
                "updatedAt": datetime.now(timezone.utc),
            }, merge=True)

        except Exception as e:
            # Don't let metrics collection break the app
            logger.warning(f"Failed to record metric {metric}: {e}")

    def record_histogram(
        self,
        metric: MetricName,
        value: float,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        Record a histogram/distribution metric (e.g., latency).

        Stores individual values in a subcollection for percentile calculation.
        Also updates aggregates (count, sum, min, max).
        """
        try:
            db = self._get_db()
            minute_key = self._get_minute_key()
            metric_name = metric.value if isinstance(metric, MetricName) else metric

            label_suffix = ""
            if labels:
                # Replace / with _ to avoid Firestore path issues
                safe_labels = {k: str(v).replace("/", "_").replace(".", "_") for k, v in labels.items()}
                label_suffix = "_" + "_".join(f"{k}:{v}" for k, v in sorted(safe_labels.items()))

            doc_id = f"{minute_key}_{metric_name}{label_suffix}"
            doc_ref = db.collection("metrics_minutes").document(doc_id)

            # Update aggregates
            doc_ref.set({
                "metric": metric_name,
                "type": MetricType.HISTOGRAM.value,
                "minute": minute_key,
                "count": firestore.Increment(1),
                "sum": firestore.Increment(value),
                "labels": labels or {},
                "updatedAt": datetime.now(timezone.utc),
            }, merge=True)

            # For more detailed analysis, store individual samples (limited)
            # Only store ~100 samples per minute to avoid excessive writes
            sample_ref = doc_ref.collection("samples").document()
            sample_ref.set({
                "value": value,
                "ts": datetime.now(timezone.utc),
            })

        except Exception as e:
            logger.warning(f"Failed to record histogram {metric}: {e}")

    def set_gauge(
        self,
        metric: MetricName,
        value: float,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        Set a gauge metric (current value, not cumulative).

        Args:
            metric: The metric name
            value: Current value
            labels: Optional labels
        """
        try:
            db = self._get_db()
            metric_name = metric.value if isinstance(metric, MetricName) else metric

            label_suffix = ""
            if labels:
                # Replace / with _ to avoid Firestore path issues
                safe_labels = {k: str(v).replace("/", "_").replace(".", "_") for k, v in labels.items()}
                label_suffix = "_" + "_".join(f"{k}:{v}" for k, v in sorted(safe_labels.items()))

            doc_id = f"gauge_{metric_name}{label_suffix}"

            db.collection("metrics_gauges").document(doc_id).set({
                "metric": metric_name,
                "type": MetricType.GAUGE.value,
                "value": value,
                "labels": labels or {},
                "updatedAt": datetime.now(timezone.utc),
            })

        except Exception as e:
            logger.warning(f"Failed to set gauge {metric}: {e}")

    @contextmanager
    def measure_time(
        self,
        metric: MetricName,
        labels: Optional[Dict[str, str]] = None,
    ):
        """
        Context manager to measure execution time.

        Usage:
            with metrics.measure_time(MetricName.JOB_DURATION_SEC, {"job_type": "summarize"}):
                do_work()
        """
        start = time.perf_counter()
        try:
            yield
        finally:
            duration = time.perf_counter() - start
            self.record_histogram(metric, duration, labels)

    def get_metrics_summary(self, hours: int = 1) -> Dict[str, Any]:
        """
        Get metrics summary for the last N hours.

        Returns aggregated metrics for monitoring dashboards.
        """
        try:
            db = self._get_db()
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            cutoff_key = cutoff.strftime("%Y-%m-%d-%H-%M")

            # Query recent metrics
            docs = list(
                db.collection("metrics_minutes")
                .where("minute", ">=", cutoff_key)
                .order_by("minute", direction=firestore.Query.DESCENDING)
                .limit(1000)
                .stream()
            )

            # Aggregate by metric name
            summary: Dict[str, Any] = {}
            for doc in docs:
                data = doc.to_dict()
                metric_name = data.get("metric")
                metric_type = data.get("type")

                if metric_name not in summary:
                    summary[metric_name] = {
                        "type": metric_type,
                        "total": 0,
                        "count": 0,
                    }

                if metric_type == MetricType.COUNTER.value:
                    summary[metric_name]["total"] += data.get("value", 0)
                elif metric_type == MetricType.HISTOGRAM.value:
                    summary[metric_name]["count"] += data.get("count", 0)
                    summary[metric_name]["total"] += data.get("sum", 0)

            # Calculate averages for histograms
            for name, stats in summary.items():
                if stats["type"] == MetricType.HISTOGRAM.value and stats["count"] > 0:
                    stats["avg"] = round(stats["total"] / stats["count"], 2)

            return summary

        except Exception as e:
            logger.error(f"Failed to get metrics summary: {e}")
            return {}


# Singleton instance
metrics = MetricsService()


# Convenience functions
def track_api_request(endpoint: str, method: str, status_code: int, latency_ms: float) -> None:
    """Track an API request with all relevant metrics."""
    labels = {"endpoint": endpoint, "method": method}

    metrics.increment(MetricName.API_REQUEST_COUNT, labels=labels)
    metrics.record_histogram(MetricName.API_REQUEST_LATENCY_MS, latency_ms, labels=labels)

    if status_code >= 500:
        metrics.increment(MetricName.API_ERROR_COUNT, labels={**labels, "status": "5xx"})
    elif status_code >= 400:
        metrics.increment(MetricName.API_ERROR_COUNT, labels={**labels, "status": "4xx"})


def track_job_queued(job_type: str) -> None:
    """Track a job being queued."""
    metrics.increment(MetricName.JOB_QUEUED_COUNT, labels={"job_type": job_type})


def track_job_completed(job_type: str, duration_sec: float) -> None:
    """Track a job completion."""
    labels = {"job_type": job_type}
    metrics.increment(MetricName.JOB_COMPLETED_COUNT, labels=labels)
    metrics.record_histogram(MetricName.JOB_DURATION_SEC, duration_sec, labels=labels)


def track_job_failed(job_type: str, error_category: str) -> None:
    """Track a job failure."""
    metrics.increment(MetricName.JOB_FAILED_COUNT, labels={"job_type": job_type, "error": error_category})


def track_rate_limit_hit(endpoint: str, user_id: Optional[str] = None) -> None:
    """Track a rate limit being hit."""
    labels = {"endpoint": endpoint}
    if user_id:
        labels["has_user"] = "true"
    metrics.increment(MetricName.RATE_LIMIT_HIT_COUNT, labels=labels)


def track_quota_exceeded(quota_type: str, plan: str) -> None:
    """Track a quota being exceeded."""
    metrics.increment(MetricName.QUOTA_EXCEEDED_COUNT, labels={"quota_type": quota_type, "plan": plan})
