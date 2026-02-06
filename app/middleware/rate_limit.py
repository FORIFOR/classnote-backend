"""
rate_limit.py - Rate Limiting Middleware

Provides per-user and per-endpoint rate limiting using slowapi.
"""

import logging
from typing import Optional, Callable
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.services.metrics import track_rate_limit_hit

logger = logging.getLogger("app.rate_limit")


def get_user_identifier(request: Request) -> str:
    """
    Get a unique identifier for rate limiting.

    Priority:
    1. User ID from auth (if authenticated)
    2. IP address (for unauthenticated requests)
    """
    # Try to get user ID from request state (set by auth middleware)
    uid = getattr(request.state, "uid", None)
    if uid:
        return f"user:{uid}"

    # Fallback to IP address
    return f"ip:{get_remote_address(request)}"


# Create limiter instance with in-memory storage
# For production with multiple instances, use Redis:
# limiter = Limiter(key_func=get_user_identifier, storage_uri="redis://localhost:6379")
limiter = Limiter(key_func=get_user_identifier)


# Rate limit configurations
class RateLimits:
    """Standard rate limit configurations."""

    # Default: 100 requests per minute
    DEFAULT = "100/minute"

    # Light endpoints (health, config): 200 requests per minute
    LIGHT = "200/minute"

    # Heavy endpoints (summarize, quiz, STT): 10 requests per minute
    HEAVY = "10/minute"

    # Very heavy (file upload): 5 requests per minute
    UPLOAD = "5/minute"

    # Search: 30 requests per minute
    SEARCH = "30/minute"

    # Auth endpoints: 20 requests per minute (prevent brute force)
    AUTH = "20/minute"

    # Admin: 50 requests per minute
    ADMIN = "50/minute"


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """
    Custom handler for rate limit exceeded errors.

    Returns a standardized 429 response with Retry-After header.
    """
    # Track metric
    endpoint = request.url.path
    uid = getattr(request.state, "uid", None)
    track_rate_limit_hit(endpoint, uid)

    # Log the event
    logger.warning(
        f"Rate limit exceeded: endpoint={endpoint}, "
        f"uid={uid}, ip={get_remote_address(request)}, "
        f"limit={exc.detail}"
    )

    # Calculate retry-after (extract from limit string)
    # e.g., "10/minute" -> 60 seconds
    retry_after = 60  # default
    if exc.detail:
        try:
            limit_str = str(exc.detail)
            if "/minute" in limit_str:
                retry_after = 60
            elif "/hour" in limit_str:
                retry_after = 3600
            elif "/second" in limit_str:
                retry_after = 1
        except Exception:
            pass

    return JSONResponse(
        status_code=429,
        content={
            "error": "rate_limit_exceeded",
            "message": "Too many requests. Please slow down.",
            "detail": str(exc.detail) if exc.detail else "Rate limit exceeded",
            "retryAfter": retry_after,
        },
        headers={
            "Retry-After": str(retry_after),
            "X-RateLimit-Limit": str(exc.detail) if exc.detail else "unknown",
        },
    )


# Decorator functions for common rate limits
def limit_default(func: Callable) -> Callable:
    """Apply default rate limit (100/minute)."""
    return limiter.limit(RateLimits.DEFAULT)(func)


def limit_heavy(func: Callable) -> Callable:
    """Apply heavy rate limit (10/minute) for expensive operations."""
    return limiter.limit(RateLimits.HEAVY)(func)


def limit_upload(func: Callable) -> Callable:
    """Apply upload rate limit (5/minute)."""
    return limiter.limit(RateLimits.UPLOAD)(func)


def limit_search(func: Callable) -> Callable:
    """Apply search rate limit (30/minute)."""
    return limiter.limit(RateLimits.SEARCH)(func)


def limit_auth(func: Callable) -> Callable:
    """Apply auth rate limit (20/minute)."""
    return limiter.limit(RateLimits.AUTH)(func)


# Export limiter for use in main.py
__all__ = [
    "limiter",
    "RateLimits",
    "rate_limit_exceeded_handler",
    "limit_default",
    "limit_heavy",
    "limit_upload",
    "limit_search",
    "limit_auth",
]
