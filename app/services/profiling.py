"""
profiling.py - Request Profiling & Phase Timing

Provides utilities to measure and record timing for different phases
of request processing (auth, firestore, llm, stt, etc.)

Usage:
    from app.services.profiling import get_profiler, phase

    # In middleware - initialize profiler for request
    profiler = RequestProfiler()
    request.state.profiler = profiler

    # In services - record phases
    with phase("firestore_read"):
        doc = db.collection("users").document(uid).get()

    # Or use the convenience function
    with get_profiler().phase("llm"):
        response = await llm.generate(...)

    # At end of request - get breakdown
    breakdown = profiler.get_breakdown()
    # {"total_ms": 250, "phases": {"auth": 15, "firestore_read": 180, "llm": 0}}
"""

import os
import time
import random
import logging
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone

logger = logging.getLogger("app.profiling")

# Configuration via environment variables
# PROFILING_SAMPLE_RATE: 0.0-1.0, percentage of requests to log detailed phases (default: 0.1 = 10%)
# PROFILING_SLOW_THRESHOLD_MS: Log all requests slower than this (default: 500ms)
# PROFILING_ENABLED: Set to "false" to disable profiling entirely
PROFILING_ENABLED = os.environ.get("PROFILING_ENABLED", "true").lower() != "false"
PROFILING_SAMPLE_RATE = float(os.environ.get("PROFILING_SAMPLE_RATE", "0.1"))
PROFILING_SLOW_THRESHOLD_MS = float(os.environ.get("PROFILING_SLOW_THRESHOLD_MS", "500"))

# Context variable to access profiler from anywhere in request lifecycle
_profiler_ctx: ContextVar[Optional["RequestProfiler"]] = ContextVar("profiler", default=None)


@dataclass
class PhaseRecord:
    """Record of a single phase measurement."""
    name: str
    start_time: float
    end_time: Optional[float] = None
    duration_ms: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def finish(self):
        """Mark phase as complete and calculate duration."""
        self.end_time = time.perf_counter()
        self.duration_ms = (self.end_time - self.start_time) * 1000


class RequestProfiler:
    """
    Tracks timing for multiple phases within a single request.

    Thread-safe via context variables. Each request gets its own profiler instance.
    """

    def __init__(self, request_id: Optional[str] = None):
        self.request_id = request_id
        self.start_time = time.perf_counter()
        self.started_at = datetime.now(timezone.utc)
        self.phases: List[PhaseRecord] = []
        self._active_phases: Dict[str, PhaseRecord] = {}
        self.endpoint: Optional[str] = None
        self.method: Optional[str] = None
        self.status_code: Optional[int] = None
        self.user_id: Optional[str] = None
        self.extra: Dict[str, Any] = {}

    def set_request_info(
        self,
        endpoint: str,
        method: str,
        user_id: Optional[str] = None,
    ):
        """Set request metadata for logging."""
        self.endpoint = endpoint
        self.method = method
        self.user_id = user_id

    def start_phase(self, name: str, **metadata) -> PhaseRecord:
        """
        Start timing a phase.

        Args:
            name: Phase name (e.g., "auth", "firestore_read", "llm")
            **metadata: Additional metadata to attach

        Returns:
            PhaseRecord that can be used to finish the phase
        """
        record = PhaseRecord(
            name=name,
            start_time=time.perf_counter(),
            metadata=metadata,
        )
        self._active_phases[name] = record
        return record

    def end_phase(self, name: str, **extra_metadata):
        """
        End timing for a phase.

        Args:
            name: Phase name to end
            **extra_metadata: Additional metadata to merge
        """
        if name in self._active_phases:
            record = self._active_phases.pop(name)
            record.finish()
            record.metadata.update(extra_metadata)
            self.phases.append(record)
            return record.duration_ms
        return None

    @contextmanager
    def phase(self, name: str, **metadata):
        """
        Context manager to time a phase.

        Usage:
            with profiler.phase("firestore_read", collection="users"):
                doc = db.get(...)
        """
        self.start_phase(name, **metadata)
        try:
            yield
        finally:
            self.end_phase(name)

    def record_phase(self, name: str, duration_ms: float, **metadata):
        """
        Record a phase that was timed externally.

        Useful when integrating with external timing (e.g., from SDK metrics).
        """
        record = PhaseRecord(
            name=name,
            start_time=0,
            end_time=0,
            duration_ms=duration_ms,
            metadata=metadata,
        )
        self.phases.append(record)

    def get_total_ms(self) -> float:
        """Get total elapsed time since profiler creation."""
        return (time.perf_counter() - self.start_time) * 1000

    def get_breakdown(self) -> Dict[str, Any]:
        """
        Get timing breakdown for logging/analysis.

        Returns:
            {
                "request_id": "req_abc123",
                "endpoint": "/sessions/123",
                "method": "GET",
                "user_id": "user_456",
                "total_ms": 250.5,
                "phases": {
                    "auth": 15.2,
                    "firestore_read": 180.3,
                    "validation": 5.0,
                },
                "phase_details": [
                    {"name": "auth", "duration_ms": 15.2, "metadata": {}},
                    ...
                ],
                "unaccounted_ms": 50.0,
                "started_at": "2026-02-05T10:30:00Z",
            }
        """
        total_ms = self.get_total_ms()

        # Aggregate phases by name (sum if same name appears multiple times)
        phase_totals: Dict[str, float] = {}
        for p in self.phases:
            if p.duration_ms is not None:
                phase_totals[p.name] = phase_totals.get(p.name, 0) + p.duration_ms

        accounted_ms = sum(phase_totals.values())
        unaccounted_ms = max(0, total_ms - accounted_ms)

        return {
            "request_id": self.request_id,
            "endpoint": self.endpoint,
            "method": self.method,
            "user_id": self.user_id,
            "status_code": self.status_code,
            "total_ms": round(total_ms, 2),
            "phases": {k: round(v, 2) for k, v in phase_totals.items()},
            "phase_details": [
                {
                    "name": p.name,
                    "duration_ms": round(p.duration_ms, 2) if p.duration_ms else None,
                    "metadata": p.metadata,
                }
                for p in self.phases
            ],
            "unaccounted_ms": round(unaccounted_ms, 2),
            "started_at": self.started_at.isoformat(),
            **self.extra,
        }

    def get_phases_summary(self) -> Dict[str, float]:
        """Get simple dict of phase name -> total duration_ms."""
        totals: Dict[str, float] = {}
        for p in self.phases:
            if p.duration_ms is not None:
                totals[p.name] = totals.get(p.name, 0) + p.duration_ms
        return {k: round(v, 2) for k, v in totals.items()}

    def should_log_details(self) -> bool:
        """
        Determine if this request should have detailed phase logging.

        Returns True if:
        - Request is slow (> PROFILING_SLOW_THRESHOLD_MS)
        - Request is sampled (random < PROFILING_SAMPLE_RATE)
        - Status code indicates error (>= 500)
        """
        total_ms = self.get_total_ms()

        # Always log slow requests
        if total_ms >= PROFILING_SLOW_THRESHOLD_MS:
            return True

        # Always log server errors
        if self.status_code and self.status_code >= 500:
            return True

        # Sample other requests
        return random.random() < PROFILING_SAMPLE_RATE

    def get_log_payload(self) -> Dict[str, Any]:
        """
        Get compact log payload for Cloud Logging.

        For sampled/slow requests: includes phase breakdown
        For others: includes only summary metrics
        """
        total_ms = self.get_total_ms()
        phases = self.get_phases_summary()

        # Compact payload for all requests
        payload = {
            "type": "request_profile",
            "request_id": self.request_id,
            "endpoint": self.endpoint,
            "method": self.method,
            "status": self.status_code,
            "total_ms": round(total_ms, 1),
            "ts": self.started_at.isoformat(),
        }

        # Add user_id only if present
        if self.user_id:
            payload["uid"] = self.user_id

        # Add phases if any were recorded
        if phases:
            payload["phases"] = phases

        # Add extra metadata
        if self.extra:
            payload["extra"] = self.extra

        # For detailed logging, add phase_details
        if self.should_log_details() and self.phases:
            payload["details"] = [
                {"n": p.name, "ms": round(p.duration_ms, 1) if p.duration_ms else 0}
                for p in self.phases
            ]
            payload["sampled"] = True

        return payload


def get_profiler() -> Optional[RequestProfiler]:
    """Get the current request's profiler from context."""
    return _profiler_ctx.get()


def set_profiler(profiler: RequestProfiler) -> None:
    """Set the profiler for the current request context."""
    _profiler_ctx.set(profiler)


def reset_profiler() -> None:
    """Clear the profiler from context."""
    _profiler_ctx.set(None)


@contextmanager
def phase(name: str, **metadata):
    """
    Convenience context manager to time a phase using the current profiler.

    If no profiler is active, timing is silently skipped.

    Usage:
        from app.services.profiling import phase

        with phase("firestore_read"):
            doc = db.get(...)
    """
    profiler = get_profiler()
    if profiler:
        with profiler.phase(name, **metadata):
            yield
    else:
        yield


def record_phase(name: str, duration_ms: float, **metadata):
    """
    Record a phase that was timed externally.

    If no profiler is active, silently skipped.
    """
    profiler = get_profiler()
    if profiler:
        profiler.record_phase(name, duration_ms, **metadata)


# Standard phase names for consistency
class Phase:
    """Standard phase names for consistency across codebase."""
    AUTH = "auth"
    AUTH_VERIFY = "auth_verify"
    AUTH_DECODE = "auth_decode"

    FIRESTORE_READ = "firestore_read"
    FIRESTORE_WRITE = "firestore_write"
    FIRESTORE_QUERY = "firestore_query"
    FIRESTORE_BATCH = "firestore_batch"
    FIRESTORE_TRANSACTION = "firestore_transaction"

    STORAGE_READ = "storage_read"
    STORAGE_WRITE = "storage_write"
    STORAGE_SIGNED_URL = "storage_signed_url"

    LLM_REQUEST = "llm_request"
    LLM_STREAMING = "llm_streaming"

    STT_REQUEST = "stt_request"
    STT_STREAMING = "stt_streaming"

    VALIDATION = "validation"
    SERIALIZATION = "serialization"

    EXTERNAL_API = "external_api"
    CLOUD_TASKS = "cloud_tasks"
