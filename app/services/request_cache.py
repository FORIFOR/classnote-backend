"""
request_cache.py - Per-request Firestore document cache

Prevents duplicate Firestore reads within a single request.
This is especially useful for /users/me where the same documents
(users/{uid}, accounts/{accountId}, uid_links/{uid}) are read multiple times.

Usage:
    cache = RequestCache()

    # First call reads from Firestore
    user_data = cache.get_doc("users", uid)

    # Second call returns cached result
    user_data_again = cache.get_doc("users", uid)

    # Parallel reads with caching
    results = await cache.get_docs_parallel([
        ("users", uid),
        ("accounts", account_id),
        ("uid_links", uid),
    ])
"""

import asyncio
from typing import Dict, List, Tuple, Optional, Any
from concurrent.futures import ThreadPoolExecutor
from google.cloud import firestore
import logging

logger = logging.getLogger("app.request_cache")


class RequestCache:
    """
    Per-request cache for Firestore documents.

    Create a new instance per request to avoid cross-request cache pollution.
    """

    def __init__(self, db: firestore.Client):
        self._db = db
        self._cache: Dict[str, Dict[str, Any]] = {}  # collection -> {doc_id -> data}
        self._executor = ThreadPoolExecutor(max_workers=5)

    def _cache_key(self, collection: str, doc_id: str) -> Tuple[str, str]:
        return (collection, doc_id)

    def get_doc(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a document, using cache if available.

        Returns:
            Document data dict, or None if document doesn't exist.
            Returns cached "__NOT_FOUND__" marker as None.
        """
        if not doc_id:
            return None

        if collection not in self._cache:
            self._cache[collection] = {}

        if doc_id in self._cache[collection]:
            cached = self._cache[collection][doc_id]
            if cached == "__NOT_FOUND__":
                return None
            return cached

        # Cache miss - read from Firestore
        doc = self._db.collection(collection).document(doc_id).get()
        if doc.exists:
            data = doc.to_dict()
            self._cache[collection][doc_id] = data
            return data
        else:
            self._cache[collection][doc_id] = "__NOT_FOUND__"
            return None

    def set_cached(self, collection: str, doc_id: str, data: Optional[Dict[str, Any]]):
        """
        Manually set a cache entry (useful when you've already read a doc).
        """
        if collection not in self._cache:
            self._cache[collection] = {}

        if data is None:
            self._cache[collection][doc_id] = "__NOT_FOUND__"
        else:
            self._cache[collection][doc_id] = data

    def is_cached(self, collection: str, doc_id: str) -> bool:
        """Check if a document is already in cache."""
        return collection in self._cache and doc_id in self._cache[collection]

    async def get_docs_parallel(
        self,
        refs: List[Tuple[str, str]]
    ) -> Dict[Tuple[str, str], Optional[Dict[str, Any]]]:
        """
        Read multiple documents in parallel, using cache where available.

        Args:
            refs: List of (collection, doc_id) tuples

        Returns:
            Dict mapping (collection, doc_id) -> document data or None
        """
        results: Dict[Tuple[str, str], Optional[Dict[str, Any]]] = {}
        to_fetch: List[Tuple[str, str]] = []

        # Check cache first
        for collection, doc_id in refs:
            if not doc_id:
                results[(collection, doc_id)] = None
                continue

            if self.is_cached(collection, doc_id):
                results[(collection, doc_id)] = self.get_doc(collection, doc_id)
            else:
                to_fetch.append((collection, doc_id))

        if not to_fetch:
            return results

        # Fetch uncached docs in parallel
        loop = asyncio.get_event_loop()

        def fetch_one(collection: str, doc_id: str) -> Tuple[Tuple[str, str], Optional[Dict[str, Any]]]:
            doc = self._db.collection(collection).document(doc_id).get()
            if doc.exists:
                data = doc.to_dict()
                return ((collection, doc_id), data)
            return ((collection, doc_id), None)

        # Run all fetches in parallel using thread pool
        tasks = [
            loop.run_in_executor(self._executor, fetch_one, col, doc_id)
            for col, doc_id in to_fetch
        ]

        fetched = await asyncio.gather(*tasks, return_exceptions=True)

        for result in fetched:
            if isinstance(result, Exception):
                logger.error(f"Parallel fetch error: {result}")
                continue
            key, data = result
            results[key] = data
            # Update cache
            self.set_cached(key[0], key[1], data)

        return results

    def clear(self):
        """Clear all cached data."""
        self._cache.clear()


def create_request_cache(db: firestore.Client) -> RequestCache:
    """Factory function to create a new request cache."""
    return RequestCache(db)
