from fastapi import FastAPI
print("DEBUG: app/main.py starting...")
from fastapi.middleware.cors import CORSMiddleware
import os

from app.routes import sessions, tasks, websocket, auth, users, billing, share, google, search, reactions, admin, imports, universal_links, debug_appstore
from app.routes.assets import router as assets_router
# try:
#     from google.cloud import speech
#     from google.cloud import aiplatform
#     print("DEBUG: Successfully imported speech and aiplatform")
# except Exception as e:
#     print(f"DEBUG: Import failed: {e}")

# Try to import usage router safely to prevent deployment crash if dependencies fail
try:
    from app.routes import usage
    usage_router_available = True
except ImportError as e:
    print(f"WARNING: Failed to import usage router: {e}")
    usage_router_available = False
except Exception as e:
    print(f"WARNING: Unexpected error importing usage router: {e}")
    usage_router_available = False

app = FastAPI(
    title="ClassnoteX API",
    description="Backend API for ClassnoteX - AI-powered recording and transcription app.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# CORS Setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# Include Routers
app.include_router(assets_router, tags=["Assets"])
app.include_router(sessions.router, tags=["Sessions"])
app.include_router(tasks.router, tags=["Internal Tasks"], include_in_schema=False)
app.include_router(websocket.router, tags=["Streaming"])
app.include_router(auth.router, tags=["Authentication"])
app.include_router(users.router, tags=["Users"])
app.include_router(billing.router, tags=["Billing"])
app.include_router(share.router, tags=["Share"])
app.include_router(google.router, tags=["Google"])
app.include_router(search.router, tags=["Search"])
app.include_router(reactions.router, tags=["Reactions"])
app.include_router(admin.router, tags=["Admin"])
app.include_router(imports.router, tags=["Imports"])
app.include_router(universal_links.router) # Root level (/.well-known)
app.include_router(debug_appstore.router)

# [NEW] Quiz Analytics
from app.routes import quiz_analytics
app.include_router(quiz_analytics.router, tags=["Quiz Analytics"])


if usage_router_available:
    app.include_router(usage.router, tags=["Usage"])



@app.get("/health")
async def health():
    return {"status": "ok"}



@app.get("/", include_in_schema=False)
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs")
