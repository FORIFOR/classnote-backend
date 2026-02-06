"""
request_id.py - Request ID Middleware

Generates and propagates X-Request-Id for distributed tracing.
"""

import uuid
from contextvars import ContextVar
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

# Context variable for request_id (accessible from anywhere in the request lifecycle)
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")


def get_request_id() -> str:
    """Get the current request ID from context."""
    return request_id_ctx.get()


class RequestIdMiddleware(BaseHTTPMiddleware):
    """
    Middleware that ensures every request has a unique request_id.

    - If X-Request-Id header is present, use it (for distributed tracing)
    - Otherwise, generate a new UUID
    - Store in request.state and context var for easy access
    - Add to response headers
    """

    async def dispatch(self, request: Request, call_next):
        # Get or generate request_id
        request_id = request.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex[:16]}"

        # Store in request state
        request.state.request_id = request_id

        # Store in context var (for access from services/utilities)
        token = request_id_ctx.set(request_id)

        try:
            response = await call_next(request)

            # Add request_id to response headers
            response.headers["X-Request-Id"] = request_id

            return response
        finally:
            # Reset context var
            request_id_ctx.reset(token)
