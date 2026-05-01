from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from datetime import datetime, timezone
import json
import logging
import os
from app.services.ops_logger import OpsLogger, Severity, EventType
from app.services.metrics import track_api_request
from app.services.profiling import (
    RequestProfiler, set_profiler, reset_profiler, PROFILING_ENABLED
)
from app.middleware.request_id import RequestIdMiddleware, get_request_id
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), force=True)
profile_logger = logging.getLogger("app.profile")

from fastapi.middleware.cors import CORSMiddleware

# [STARTUP CHECK] Validate required environment variables
# Missing env vars will cause startup failure, making issues visible at deploy time
REQUIRED_ENV_VARS = [
    # "LINE_CHANNEL_ID",  # Required for LINE login - uncomment when configured
]
RECOMMENDED_ENV_VARS = [
    "LINE_CHANNEL_ID",  # LINE login (optional but logged if missing)
    "GCP_PROJECT",
    "VERTEX_LOCATION",
]

def _check_env_vars():
    missing_required = [k for k in REQUIRED_ENV_VARS if not os.getenv(k)]
    if missing_required:
        raise RuntimeError(f"[STARTUP ERROR] Missing required env vars: {', '.join(missing_required)}")

    missing_recommended = [k for k in RECOMMENDED_ENV_VARS if not os.getenv(k)]
    if missing_recommended:
        print(f"[STARTUP WARNING] Missing recommended env vars: {', '.join(missing_recommended)}")

    # Warn if security-sensitive secrets are using default dev values
    watch_secret = os.getenv("WATCH_TOKEN_SECRET", "")
    if not watch_secret or watch_secret == "dev-watch-secret-do-not-use-in-prod":
        print("[STARTUP WARNING] WATCH_TOKEN_SECRET is missing or using insecure default value")

_check_env_vars()

from app.routes import sessions, tasks, websocket, auth, users, billing, share, google, search, reactions, admin, imports, universal_links, debug_appstore, ads, account, account_merge, phone, app_config, jobs, todos, ops, watch, translate, chat
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
    title="DeepNote API",
    description="Backend API for DeepNote - AI-powered recording and transcription app.",
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

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """
    Catch-all for unhandled exceptions.
    Returns JSON 500 instead of default HTML, ensuring iOS can always parse the response.
    """
    logger = logging.getLogger("app.main")
    logger.exception(f"Unhandled exception on {request.method} {request.url.path}: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )

# [ENHANCED] Ops Logger & Metrics Middleware with Phase Profiling
@app.middleware("http")
async def ops_logger_middleware(request: Request, call_next):
    # Skip profiling for health/static endpoints
    skip_profiling = request.url.path in ["/health", "/", "/docs", "/redoc", "/openapi.json", "/favicon.ico"]

    # Initialize profiler for this request (lightweight - just stores start time)
    profiler = None
    if PROFILING_ENABLED and not skip_profiling:
        request_id = getattr(request.state, "request_id", None)
        profiler = RequestProfiler(request_id=request_id)
        profiler.set_request_info(
            endpoint=request.url.path,
            method=request.method,
        )
        set_profiler(profiler)
        request.state.profiler = profiler

    start_time = datetime.now(timezone.utc)
    try:
        response = await call_next(request)
    finally:
        # Always reset profiler context
        if profiler:
            reset_profiler()

    process_time = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000

    # Get request_id from middleware (set by RequestIdMiddleware)
    request_id = getattr(request.state, "request_id", None)

    # Update profiler with response info and log
    if profiler:
        profiler.status_code = response.status_code
        profiler.user_id = getattr(request.state, "uid", None)

        # Log profile data (structured JSON for Cloud Logging analysis)
        # Only logs details for sampled/slow/error requests
        try:
            log_payload = profiler.get_log_payload()
            # Use structured logging for Cloud Logging compatibility
            profile_logger.info(json.dumps(log_payload, ensure_ascii=False))
        except Exception:
            pass  # Never let profiling break the request

    # Track metrics for all requests (except health checks)
    if not skip_profiling:
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
            props={
                "latencyMs": int(process_time),
                "method": request.method,
                "remoteIp": request.client.host if request.client else None,
                "email": getattr(request.state, "email", None),
                "phases": profiler.get_phases_summary() if profiler else None,
            },
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
            props={
                "latencyMs": int(process_time),
                "method": request.method,
                "remoteIp": request.client.host if request.client else None,
                "email": getattr(request.state, "email", None),
            },
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
    # Cloud Run service URLs (both old and new during migration)
    "https://classnote-api-900324644592.asia-northeast1.run.app",
    "https://deepnote-api-900324644592.asia-northeast1.run.app",
    "http://localhost:3000",  # Local development
    "http://localhost:8080",  # Local development
    "http://localhost:1420",  # Tauri dev
    "tauri://localhost",      # Tauri production
    "https://tauri.localhost", # Tauri production (WebKit)
    "https://deepnote.app",   # Billing Web UI
    "https://www.deepnote.app",
    "https://deepnote-billing-ui.vercel.app",  # Vercel deployment
    "https://classnote-dashboard-900324644592.asia-northeast1.run.app",  # Ops Dashboard
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

# Apple Sign-In relay for desktop apps
from app.routes import auth_apple_desktop
app.include_router(auth_apple_desktop.router, tags=["Authentication"])
# LINE Login relay for desktop apps
from app.routes import auth_line_desktop
app.include_router(auth_line_desktop.router, tags=["Authentication"])
# LINE Login relay for web apps (Billing UI)
from app.routes import auth_line_web
app.include_router(auth_line_web.router, tags=["Authentication"])
app.include_router(users.router, tags=["Users"])
# Overlay Assist (real-time AI during recording)
from app.routes import assist
app.include_router(assist.router, tags=["Assist"])
app.include_router(billing.router, tags=["Billing"])
app.include_router(share.router, tags=["Share"])
# [DEPRECATED 2026-05-01] Legacy Google OAuth routes (replaced by integrations_google).
# Kept import to avoid breaking any latent reference, but not registered:
# app.include_router(google.router, tags=["Google"])
# app.include_router(google.integrations_router)
from app.routes import integrations_google, integrations_microsoft
app.include_router(integrations_google.router)
app.include_router(integrations_google.oauth_router)
app.include_router(integrations_google.auth_alias_router)
app.include_router(integrations_microsoft.router)
app.include_router(integrations_microsoft.oauth_router)
# Startup soft-check: warn if token_crypto / OAuth not configured
try:
    from app.services import token_crypto as _token_crypto
    if not _token_crypto.is_configured():
        print("WARNING: TOKEN_ENCRYPTION_KEY not set — /integrations/* will return 503")
except Exception as _e:
    print(f"WARNING: token_crypto preload failed: {_e}")
app.include_router(search.router, tags=["Search"])
app.include_router(reactions.router, tags=["Reactions"])
app.include_router(admin.router, tags=["Admin"])
from app.routes import dashboard_public
app.include_router(dashboard_public.router, tags=["Dashboard Public"])
app.include_router(ops.router, tags=["Ops"])  # Deployment safety & presence
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

# [NEW] TODO Management API
app.include_router(todos.router, tags=["TODOs"])

# [NEW] Watch Authentication
app.include_router(watch.router, tags=["Watch"])

if usage_router_available:
    app.include_router(usage.router, tags=["Usage"])

# [NEW] Invite/Referral System
from app.routes import invites
app.include_router(invites.router, tags=["Invites"])

# [NEW] AI Chat (session-aware conversational AI)
app.include_router(chat.router, tags=["AI Chat"])

# [NEW] Translation API (Cloud Translation v2)
app.include_router(translate.router, tags=["Translation"])

# [NEW] Export (DOCX / PPTX)
try:
    from app.routes import export as export_route
    app.include_router(export_route.router, tags=["Export"])
except Exception as e:
    print(f"WARNING: Failed to import export router: {e}")

# [NEW] Orb Theme (server-driven orb appearance)
from app.routes import orb_theme
app.include_router(orb_theme.router, tags=["Orb Theme"])

# [NEW] Download redirects (fixed URLs for Studio/website)
from app.routes import download
app.include_router(download.router, tags=["Download"])

# [NEW] Stripe Billing (Web checkout / portal / webhook)
try:
    from app.routes import billing_stripe
    app.include_router(billing_stripe.router, tags=["Billing Stripe"])
except ImportError as e:
    print(f"WARNING: Failed to import billing_stripe router: {e}")



@app.get("/health")
async def health():
    from app.services.stt_circuit_breaker import stt_circuit_breaker
    from app.services.app_config import is_feature_enabled

    cloud_stt_enabled = False
    try:
        cloud_stt_enabled = is_feature_enabled("cloudStt")
    except Exception:
        pass

    cb_status = stt_circuit_breaker.get_status()

    return {
        "status": "ok",
        "cloudStt": {
            "available": cloud_stt_enabled and cb_status["available"],
            "featureEnabled": cloud_stt_enabled,
            "circuitBreaker": cb_status["state"],
        },
    }



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

@app.get("/logo.png", include_in_schema=False)
async def logo():
    from fastapi.responses import FileResponse
    import os
    file_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "public", "logo.png")
    return FileResponse(file_path, media_type="image/png")

