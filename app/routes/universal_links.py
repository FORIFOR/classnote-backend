from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()

import os

# [FIX] Use Environment Variables for Team ID and Bundle ID
# Default to Real Team ID provided by User
TEAM_ID = os.environ.get("APPLE_TEAM_ID", "6RR7572ZLU") 
BUNDLE_ID = os.environ.get("APPLE_BUNDLE_ID", "jp.horioshuhei.deepnote")

AASA_CONTENT = {
    "applinks": {
        "apps": [],
        "details": [
            {
                "appID": f"{TEAM_ID}.{BUNDLE_ID}",
                "paths": ["/s/*"]
            }
        ]
    }
}

@router.get("/.well-known/apple-app-site-association", include_in_schema=False)
@router.get("/apple-app-site-association", include_in_schema=False)
async def apple_app_site_association():
    """
    Serve the AASA file for Universal Links.
    Must return application/json.
    """
    return JSONResponse(content=AASA_CONTENT, media_type="application/json")
