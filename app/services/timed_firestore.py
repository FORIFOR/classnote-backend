"""
timed_firestore.py - Lightweight Firestore Timing Helpers

Provides optional timing wrappers for Firestore operations.
Use these in hot paths where you want to measure latency.

Design principles:
- Zero overhead when profiling is disabled
- Minimal overhead when enabled (just time.perf_counter calls)
- Non-invasive - use explicitly where needed
- Falls back gracefully if profiler not active

Usage:
    from app.services.timed_firestore import timed_get, timed_set, timed_query

    # Instead of: doc = db.collection("users").document(uid).get()
    doc = await timed_get(db.collection("users").document(uid))

    # For sync operations (Firestore SDK is sync):
    doc = timed_get_sync(db.collection("users").document(uid))
"""

import time
from typing import Any, Optional, List
from app.services.profiling import get_profiler, Phase, PROFILING_ENABLED


def timed_get_sync(doc_ref, label: Optional[str] = None) -> Any:
    """
    Time a Firestore document get operation (synchronous).

    Args:
        doc_ref: Firestore DocumentReference
        label: Optional label for the phase (default: "firestore_read")

    Returns:
        DocumentSnapshot
    """
    if not PROFILING_ENABLED:
        return doc_ref.get()

    profiler = get_profiler()
    if not profiler:
        return doc_ref.get()

    phase_name = label or Phase.FIRESTORE_READ
    start = time.perf_counter()
    try:
        result = doc_ref.get()
        return result
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        profiler.record_phase(phase_name, duration_ms, op="get", path=doc_ref.path if hasattr(doc_ref, 'path') else None)


def timed_set_sync(doc_ref, data: dict, merge: bool = False, label: Optional[str] = None) -> Any:
    """
    Time a Firestore document set operation (synchronous).

    Args:
        doc_ref: Firestore DocumentReference
        data: Data to set
        merge: Whether to merge with existing data
        label: Optional label for the phase (default: "firestore_write")

    Returns:
        WriteResult
    """
    if not PROFILING_ENABLED:
        return doc_ref.set(data, merge=merge)

    profiler = get_profiler()
    if not profiler:
        return doc_ref.set(data, merge=merge)

    phase_name = label or Phase.FIRESTORE_WRITE
    start = time.perf_counter()
    try:
        result = doc_ref.set(data, merge=merge)
        return result
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        profiler.record_phase(phase_name, duration_ms, op="set", path=doc_ref.path if hasattr(doc_ref, 'path') else None)


def timed_update_sync(doc_ref, data: dict, label: Optional[str] = None) -> Any:
    """
    Time a Firestore document update operation (synchronous).
    """
    if not PROFILING_ENABLED:
        return doc_ref.update(data)

    profiler = get_profiler()
    if not profiler:
        return doc_ref.update(data)

    phase_name = label or Phase.FIRESTORE_WRITE
    start = time.perf_counter()
    try:
        result = doc_ref.update(data)
        return result
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        profiler.record_phase(phase_name, duration_ms, op="update", path=doc_ref.path if hasattr(doc_ref, 'path') else None)


def timed_delete_sync(doc_ref, label: Optional[str] = None) -> Any:
    """
    Time a Firestore document delete operation (synchronous).
    """
    if not PROFILING_ENABLED:
        return doc_ref.delete()

    profiler = get_profiler()
    if not profiler:
        return doc_ref.delete()

    phase_name = label or Phase.FIRESTORE_WRITE
    start = time.perf_counter()
    try:
        result = doc_ref.delete()
        return result
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        profiler.record_phase(phase_name, duration_ms, op="delete", path=doc_ref.path if hasattr(doc_ref, 'path') else None)


def timed_query_sync(query, label: Optional[str] = None) -> List[Any]:
    """
    Time a Firestore query and stream operation (synchronous).

    Args:
        query: Firestore Query object
        label: Optional label for the phase (default: "firestore_query")

    Returns:
        List of DocumentSnapshots
    """
    if not PROFILING_ENABLED:
        return list(query.stream())

    profiler = get_profiler()
    if not profiler:
        return list(query.stream())

    phase_name = label or Phase.FIRESTORE_QUERY
    start = time.perf_counter()
    try:
        results = list(query.stream())
        return results
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        profiler.record_phase(phase_name, duration_ms, op="query", count=len(results))


def timed_batch_commit_sync(batch, label: Optional[str] = None) -> Any:
    """
    Time a Firestore batch commit operation (synchronous).
    """
    if not PROFILING_ENABLED:
        return batch.commit()

    profiler = get_profiler()
    if not profiler:
        return batch.commit()

    phase_name = label or Phase.FIRESTORE_BATCH
    start = time.perf_counter()
    try:
        result = batch.commit()
        return result
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        profiler.record_phase(phase_name, duration_ms, op="batch_commit")


class TimedTransaction:
    """
    Context manager for timing Firestore transactions.

    Usage:
        with TimedTransaction(db) as transaction:
            doc = transaction.get(doc_ref)
            transaction.set(doc_ref, new_data)
    """

    def __init__(self, db, label: Optional[str] = None):
        self.db = db
        self.label = label or Phase.FIRESTORE_TRANSACTION
        self.start_time: Optional[float] = None
        self._transaction = None

    def __enter__(self):
        if PROFILING_ENABLED:
            self.start_time = time.perf_counter()
        self._transaction = self.db.transaction()
        return self._transaction

    def __exit__(self, exc_type, exc_val, exc_tb):
        if PROFILING_ENABLED and self.start_time:
            duration_ms = (time.perf_counter() - self.start_time) * 1000
            profiler = get_profiler()
            if profiler:
                profiler.record_phase(self.label, duration_ms, op="transaction")


# Async versions (for future use if SDK adds async support)
async def timed_get(doc_ref, label: Optional[str] = None) -> Any:
    """Async wrapper - currently just calls sync version."""
    return timed_get_sync(doc_ref, label)


async def timed_set(doc_ref, data: dict, merge: bool = False, label: Optional[str] = None) -> Any:
    """Async wrapper - currently just calls sync version."""
    return timed_set_sync(doc_ref, data, merge, label)


async def timed_query(query, label: Optional[str] = None) -> List[Any]:
    """Async wrapper - currently just calls sync version."""
    return timed_query_sync(query, label)
