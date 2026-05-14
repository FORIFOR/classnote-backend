"""Pydantic schemas for newly designed features.

This package is added in the business-license / PR1 cycle. Existing route
DTOs continue to live in ``app/util_models.py`` — the split is intentional:
new bounded contexts get their own schema module so they don't accrete
into the 1200-line util_models grab-bag.
"""
