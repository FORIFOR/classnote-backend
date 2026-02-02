from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from datetime import datetime, timezone
from app.services.ops_logger import OpsLogger, Severity, EventType
from app.services.metrics import track_api_request
from app.middleware.request_id import RequestIdMiddleware, get_request_id
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

print("DEBUG: app/main.py starting...")
from fastapi.middleware.cors import CORSMiddleware
import os

from app.routes import sessions, tasks, websocket, auth, users, billing, share, google, search, reactions, admin, imports, universal_links, debug_appstore, ads, account, account_merge, phone, app_config, jobs
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

# Rate Limiter setup
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """
    Standardizes error responses. 
    If detail is a dict, return it directly to avoid double-wrapping in {"detail": ...}.
    """
    headers = getattr(exc, "headers", None)
    if isinstance(exc.detail, dict):
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.detail,
            headers=headers
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=headers
    )

# [ENHANCED] Ops Logger & Metrics Middleware
@app.middleware("http")
async def ops_logger_middleware(request: Request, call_next):
    start_time = datetime.now(timezone.utc)
    response = await call_next(request)
    process_time = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000

    # Get request_id from middleware (set by RequestIdMiddleware)
    request_id = getattr(request.state, "request_id", None)

    # Track metrics for all requests (except health checks)
    if request.url.path not in ["/health", "/", "/docs", "/redoc", "/openapi.json"]:
        track_api_request(
            endpoint=request.url.path,
            method=request.method,
            status_code=response.status_code,
            latency_ms=process_time
        )

    # Log API Error or suspicious latency
    if response.status_code >= 500:
        OpsLogger().log(
            severity=Severity.ERROR,
            event_type=EventType.API_ERROR,
            endpoint=request.url.path,
            status_code=response.status_code,
            request_id=request_id,
            message=f"API 500 Error: {request.url.path}",
            props={"latencyMs": int(process_time), "method": request.method, "remoteIp": request.client.host if request.client else None, "email": getattr(request.state, "email", None)},
            trace_id=request.headers.get("X-Cloud-Trace-Context"),
            uid=getattr(request.state, "uid", None)
        )
    # 400系は INFO/WARN レベル (認証エラーなどは除外してもよいが、ここでは全て記録しフィルタで分ける)
    # ただし大量になるので 401/403/429/402 など重要なものに絞るのが一般的
    elif response.status_code in [402, 409, 429]:
        OpsLogger().log(
            severity=Severity.WARN,
            event_type=EventType.API_ERROR,
            endpoint=request.url.path,
            status_code=response.status_code,
            request_id=request_id,
            message=f"API Client Error: {response.status_code}",
            props={"latencyMs": int(process_time), "method": request.method, "remoteIp": request.client.host if request.client else None, "email": getattr(request.state, "email", None)},
            trace_id=request.headers.get("X-Cloud-Trace-Context"),
            uid=getattr(request.state, "uid", None)
        )

    return response

# CORS Setup - Explicit allowed origins for security
ALLOWED_ORIGINS = [
    "https://classnote.app",
    "https://www.classnote.app",
    "https://classnote-x-dev.web.app",
    "https://classnote-x.web.app",
    "http://localhost:3000",  # Local development
    "http://localhost:8080",  # Local development
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# Request ID Middleware (must be added after CORS, runs before ops_logger)
app.add_middleware(RequestIdMiddleware)

# Include Routers
app.include_router(account.router, tags=["Account"])
app.include_router(account_merge.router, tags=["Account Merge"])
app.include_router(phone.router, tags=["Phone Verification"])
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
app.include_router(ads.router, tags=["Ads"])

# [NEW] Quiz Analytics
from app.routes import quiz_analytics
app.include_router(quiz_analytics.router, tags=["Quiz Analytics"])

# [NEW] App Config (Maintenance Mode / Feature Flags)
app.include_router(app_config.router, tags=["App Config"])

# [NEW] Async Jobs API (Summary/Quiz Generation) - v2
app.include_router(jobs.router, tags=["Jobs"])

if usage_router_available:
    app.include_router(usage.router, tags=["Usage"])



@app.get("/health")
async def health():
    return {"status": "ok"}



@app.get("/", include_in_schema=False)
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs")

# Legal Pages (Static HTML)
@app.get("/terms", include_in_schema=False)
async def terms_page():
    from fastapi.responses import FileResponse
    import os
    file_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "public", "terms.html")
    return FileResponse(file_path, media_type="text/html")

@app.get("/privacy", include_in_schema=False)
async def privacy_page():
    from fastapi.responses import FileResponse
    import os
    file_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "public", "privacy.html")
    return FileResponse(file_path, media_type="text/html")

