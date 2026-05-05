"""Internal endpoint hit by Cloud Scheduler to run scheduled digests.

Authenticated via DIGEST_INTERNAL_TOKEN (shared bearer). When the env is
unset, the endpoint returns 503 to make misconfiguration loudly visible.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from app.services import digest_scheduler

logger = logging.getLogger("app.routes.digests")
router = APIRouter(prefix="/internal/tasks", tags=["Internal Tasks"])


@router.post("/run_morning_digests", include_in_schema=False)
async def run_morning_digests(
    authorization: Optional[str] = Header(None),
):
    expected = os.environ.get("DIGEST_INTERNAL_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="digest_token_not_configured")
    provided = ""
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization.split(" ", 1)[1].strip()
    if provided != expected:
        raise HTTPException(status_code=401, detail="invalid_token")
    summary = digest_scheduler.run_morning_digests()
    return {"status": "ok", **summary}
