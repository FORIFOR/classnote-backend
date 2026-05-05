"""
Download redirect endpoints — fixed URLs that redirect to the latest version.

Studio/website links point here; actual files live on Cloud Storage.
Version info is stored in Firestore (downloads/config) so updates
don't require a redeploy.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from app.firebase import db

logger = logging.getLogger("app.routes.download")

router = APIRouter(prefix="/download", tags=["Download"])


def _get_download_config() -> dict:
    """Fetch download config from Firestore."""
    doc = db.collection("config").document("downloads").get()
    if not doc.exists:
        return {}
    return doc.to_dict() or {}


@router.get("/mac")
async def download_mac():
    """Redirect to the latest macOS installer."""
    config = _get_download_config()
    mac = config.get("mac")
    if not mac or not mac.get("url"):
        raise HTTPException(status_code=404, detail="Mac download not available")

    logger.info(f"[Download] mac redirect → {mac.get('version', '?')}")
    return RedirectResponse(url=mac["url"], status_code=302)


@router.get("/windows")
async def download_windows():
    """Redirect to the latest Windows installer."""
    config = _get_download_config()
    win = config.get("windows")
    if not win or not win.get("url"):
        raise HTTPException(status_code=404, detail="Windows download not available")

    logger.info(f"[Download] windows redirect → {win.get('version', '?')}")
    return RedirectResponse(url=win["url"], status_code=302)


@router.get("/latest")
async def download_latest_info():
    """Return current version info (for Studio display or update checks)."""
    config = _get_download_config()
    mac = config.get("mac", {})
    win = config.get("windows", {})
    return {
        "mac": {
            "version": mac.get("version"),
            "url": f"/download/mac",
            "note": mac.get("note", "Apple Silicon / Intel 対応"),
        },
        "windows": {
            "version": win.get("version"),
            "url": f"/download/windows",
            "note": win.get("note", "Windows 10/11 対応"),
        },
    }
