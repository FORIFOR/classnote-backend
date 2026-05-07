"""Phase 2: surface DeepNote export URLs to LINE / Slack chat replies.

Phase 2 design choice:
  We DO NOT regenerate PDF/DOCX/PPTX inside the webhook context. The existing
  POST /sessions/{id}/export endpoint already handles rendering + GCS signing
  + cost guard, but it requires an authenticated client. Re-doing that work
  synchronously in a webhook would (1) couple chat latency to renderer cost,
  (2) double-spend AI credits, and (3) require us to re-implement the cost
  guard inline.

  Instead Phase 2 returns deep links into the DeepNote web app so the user
  opens the export page (which already calls /sessions/{id}/export and
  handles signing). That fulfils the user's stated requirement: "PDF / DOCX
  / PPTX は URL または LIFF / Web で開ける".

  On-demand server-side rendering is tracked as Phase 3 (`feat/server-side-
  asset-rendering`) when needed.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional
from urllib.parse import urlencode

from app.services import line_briefing

logger = logging.getLogger("app.services.asset_delivery")


def _app_base_url() -> str:
    """Where the export bridge / web view lives.

    Priority:
      1. DEEPNOTE_APP_BASE_URL (frontend, when it exists)
      2. CLOUD_RUN_SERVICE_URL (backend self-host bridge — Phase 6)
      3. hard-coded prod backend URL (last resort)
    """
    return (
        os.environ.get("DEEPNOTE_APP_BASE_URL")
        or os.environ.get("CLOUD_RUN_SERVICE_URL")
        or "https://deepnote-api-mur5rvqgga-an.a.run.app"
    ).rstrip("/")


def get_latest_export_links(account_id: str) -> Optional[Dict[str, Any]]:
    """Return {sessionId, title, links: {pdf, docx, pptx, web}} for the
    user's most recent session, or None if no session exists."""
    latest = line_briefing.get_latest_session(account_id)
    if not latest:
        return None
    base = _app_base_url()
    sid = latest["id"]
    return {
        "sessionId": sid,
        "title": latest.get("title") or "(無題の会議)",
        "links": {
            "web":  f"{base}/sessions/{sid}",
            "pdf":  f"{base}/sessions/{sid}/export?{urlencode({'format': 'pdf'})}",
            "docx": f"{base}/sessions/{sid}/export?{urlencode({'format': 'docx'})}",
            "pptx": f"{base}/sessions/{sid}/export?{urlencode({'format': 'pptx'})}",
        },
    }
