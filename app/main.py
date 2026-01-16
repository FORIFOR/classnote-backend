from fastapi import FastAPI, Request
from datetime import datetime, timezone
from app.services.ops_logger import OpsLogger, Severity, EventType

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

# [NEW] Ops Logger Middleware
@app.middleware("http")
async def ops_logger_middleware(request: Request, call_next):
    start_time = datetime.now(timezone.utc)
    response = await call_next(request)
    process_time = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000

    # Log API Error or suspicious latency
    if response.status_code >= 500:
        OpsLogger().log(
            severity=Severity.ERROR,
            event_type=EventType.API_ERROR,
            endpoint=request.url.path,
            status_code=response.status_code,
            message=f"API 500 Error: {request.url.path}",
            props={"latencyMs": int(process_time), "method": request.method, "remoteIp": request.client.host},
            trace_id=request.headers.get("X-Cloud-Trace-Context")
        )
    # 400系は INFO/WARN レベル (認証エラーなどは除外してもよいが、ここでは全て記録しフィルタで分ける)
    # ただし大量になるので 401/403/429/402 など重要なものに絞るのが一般的
    elif response.status_code in [402, 409, 429]:
         OpsLogger().log(
            severity=Severity.WARN,
            event_type=EventType.API_ERROR,
            endpoint=request.url.path,
            status_code=response.status_code,
            message=f"API Client Error: {response.status_code}",
            props={"latencyMs": int(process_time), "method": request.method, "remoteIp": request.client.host},
             trace_id=request.headers.get("X-Cloud-Trace-Context")
        )

    return response

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
