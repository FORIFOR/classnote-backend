import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, timezone, timedelta
import json
import os

pd.set_option('future.no_silent_downcasting', True)

# ---------------------------------------------------------
# 1. Page Config & CSS
# ---------------------------------------------------------
st.set_page_config(
    page_title="ClassnoteX Admin",
    page_icon="⚡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

COLOR_BG = "#f8fafc"
COLOR_GLASS = "rgba(255, 255, 255, 0.95)"
COLOR_BORDER = "rgba(0, 0, 0, 0.08)"
COLOR_TEXT_MAIN = "#0f172a"
COLOR_TEXT_SUB = "#64748b"
COLOR_GREEN = "#10b981"
COLOR_YELLOW = "#f59e0b"
COLOR_GRAY = "#9ca3af"
COLOR_RED = "#ef4444"
COLOR_BLUE = "#3b82f6"

st.markdown(f"""
<style>
    .stApp {{ background: {COLOR_BG}; color: {COLOR_TEXT_MAIN}; }}
    .glass-card {{
        background: {COLOR_GLASS};
        border: 1px solid {COLOR_BORDER};
        border-radius: 12px;
        padding: 16px;
        box-shadow: 0 2px 4px rgba(0, 0, 0, 0.04);
        margin-bottom: 12px;
    }}
    .kpi-value {{ font-size: 28px; font-weight: 700; color: {COLOR_TEXT_MAIN}; }}
    .kpi-label {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: {COLOR_TEXT_SUB}; }}
    div[data-testid="stMetricValue"] {{ font-size: 24px; color: {COLOR_TEXT_MAIN}; }}
    .badge-success {{ background: {COLOR_GREEN}; color: white; padding: 2px 8px; border-radius: 4px; font-size: 11px; }}
    .badge-warning {{ background: {COLOR_YELLOW}; color: white; padding: 2px 8px; border-radius: 4px; font-size: 11px; }}
    .badge-error {{ background: {COLOR_RED}; color: white; padding: 2px 8px; border-radius: 4px; font-size: 11px; }}
    .badge-info {{ background: {COLOR_BLUE}; color: white; padding: 2px 8px; border-radius: 4px; font-size: 11px; }}
    .json-viewer {{ background: #1e293b; color: #e2e8f0; padding: 16px; border-radius: 8px; font-family: monospace; font-size: 12px; overflow-x: auto; }}
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------
# 2. Sidebar & Config
# ---------------------------------------------------------
with st.sidebar:
    st.title("⚡️ ClassnoteX Admin")
    st.divider()

    project_options = ["classnote-x-dev", "paypaybackend", "Auto (Default Credentials)"]
    selected_project = st.selectbox("GCP Project", project_options, index=0)

    st.divider()

    # Date range
    date_range = st.selectbox("Date Range", ["Today", "7 days", "30 days", "All Time"], index=1)

    st.divider()

    if st.button("🔄 Refresh Data"):
        st.cache_data.clear()
        st.rerun()

    st.caption(f"Updated: {datetime.now().strftime('%H:%M:%S')}")

# ---------------------------------------------------------
# 3. Firestore Client
# ---------------------------------------------------------
_firestore_client = None

def _init_firebase(project_id=None):
    global _firestore_client
    from google.cloud import firestore as gcp_firestore
    target = None if project_id == "Auto (Default Credentials)" else project_id
    if _firestore_client is None:
        _firestore_client = gcp_firestore.Client(project=target) if target else gcp_firestore.Client()
    return _firestore_client

def _normalize_plan(plan: str) -> str:
    """Normalize plan string."""
    if plan in ("basic", "standard"):
        return "basic"
    return "free"

JST = timezone(timedelta(hours=9))

def _format_timestamp(ts, to_jst=True):
    """Format timestamp for display in JST."""
    if ts is None:
        return "-"
    if hasattr(ts, 'strftime'):
        if to_jst:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ts = ts.astimezone(JST)
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    return str(ts)

def _format_timestamp_short(ts, to_jst=True):
    """Format timestamp for display in JST (short format)."""
    if ts is None:
        return "-"
    if hasattr(ts, 'strftime'):
        if to_jst:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ts = ts.astimezone(JST)
        return ts.strftime("%m/%d %H:%M")
    return str(ts)

def _serialize_for_json(obj):
    """Serialize Firestore objects to JSON-safe format."""
    if obj is None:
        return None
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, 'to_dict'):
        return _serialize_for_json(obj.to_dict())
    if isinstance(obj, dict):
        return {k: _serialize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize_for_json(i) for i in obj]
    if isinstance(obj, bytes):
        return f"<bytes: {len(obj)} bytes>"
    return obj

# ---------------------------------------------------------
# 4. Data Fetching
# ---------------------------------------------------------

# --- New: ops_events fetching ---
@st.cache_data(ttl=60)
def fetch_ops_events(project_id, hours=24, event_type=None, severity=None):
    """Fetch ops_events from Firestore."""
    try:
        db = _init_firebase(project_id)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        query = db.collection("ops_events").where("ts", ">=", cutoff)

        docs = list(query.order_by("ts", direction="DESCENDING").limit(1000).stream())

        events = []
        for doc in docs:
            data = doc.to_dict()
            # Apply filters in Python (Firestore doesn't support multiple inequality filters)
            if event_type and data.get("type") != event_type:
                continue
            if severity and data.get("severity") != severity:
                continue
            data["id"] = doc.id
            events.append(data)

        return events
    except Exception as e:
        st.error(f"ops_events fetch error: {e}")
        return []


@st.cache_data(ttl=300)
def fetch_apple_notifications(project_id, days=7):
    """Fetch Apple notifications from Firestore."""
    try:
        db = _init_firebase(project_id)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        query = db.collection("apple_notifications").where("receivedAt", ">=", cutoff)

        docs = list(query.order_by("receivedAt", direction="DESCENDING").limit(500).stream())

        notifications = []
        for doc in docs:
            data = doc.to_dict()
            data["id"] = doc.id
            notifications.append(data)

        return notifications
    except Exception as e:
        st.error(f"Apple notifications fetch error: {e}")
        return []


@st.cache_data(ttl=300)
def fetch_monthly_usage_all(project_id):
    """Fetch monthly_usage for all accounts."""
    try:
        db = _init_firebase(project_id)
        from datetime import datetime
        month_key = datetime.now().strftime("%Y-%m")

        results = []

        # Fetch from accounts collection
        for acc_doc in db.collection("accounts").limit(500).stream():
            usage_doc = acc_doc.reference.collection("monthly_usage").document(month_key).get()
            if usage_doc.exists:
                data = usage_doc.to_dict()
                data["accountId"] = acc_doc.id
                data["_type"] = "account"
                results.append(data)

        # Also fetch from users collection (legacy)
        for user_doc in db.collection("users").limit(500).stream():
            usage_doc = user_doc.reference.collection("monthly_usage").document(month_key).get()
            if usage_doc.exists:
                data = usage_doc.to_dict()
                data["userId"] = user_doc.id
                data["_type"] = "user"
                results.append(data)

        return results
    except Exception as e:
        st.error(f"Monthly usage fetch error: {e}")
        return []


@st.cache_data(ttl=60)
def fetch_all_jobs_enhanced(project_id, hours=24):
    """Fetch all jobs with enhanced details for stuck detection."""
    try:
        db = _init_firebase(project_id)
        from google.cloud import firestore as gcp_firestore

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        jobs = []

        # Fetch recent sessions and their jobs
        sessions_docs = list(db.collection("sessions").order_by("createdAt", direction=gcp_firestore.Query.DESCENDING).limit(200).stream())

        for session_doc in sessions_docs:
            sid = session_doc.id
            session_data = session_doc.to_dict()
            owner_uid = session_data.get("ownerUid") or session_data.get("userId") or session_data.get("ownerUserId")

            jobs_docs = list(db.collection("sessions").document(sid).collection("jobs").limit(20).stream())
            for doc in jobs_docs:
                data = doc.to_dict()
                created_at = data.get("createdAt")
                updated_at = data.get("updatedAt") or data.get("completedAt") or created_at

                # Calculate duration if completed
                duration_sec = None
                if data.get("status") == "completed" and created_at and updated_at:
                    if hasattr(created_at, 'timestamp') and hasattr(updated_at, 'timestamp'):
                        duration_sec = (updated_at.timestamp() - created_at.timestamp())

                jobs.append({
                    "jobId": doc.id,
                    "sessionId": sid,
                    "ownerUid": owner_uid,
                    "type": data.get("type") or data.get("jobType") or "-",
                    "status": data.get("status") or "-",
                    "createdAt": created_at,
                    "updatedAt": updated_at,
                    "durationSec": duration_sec,
                    "errorReason": data.get("errorReason") or data.get("error") or "-",
                    "errorCode": data.get("errorCode") or "-"
                })

        return jobs
    except Exception as e:
        st.error(f"Enhanced jobs fetch error: {e}")
        return []


def detect_stuck_jobs(jobs, threshold_minutes=10):
    """Detect jobs that are stuck (running for too long)."""
    now = datetime.now(timezone.utc)
    threshold = timedelta(minutes=threshold_minutes)
    stuck = []

    for job in jobs:
        if job.get("status") in ["running", "queued"]:
            updated_at = job.get("updatedAt") or job.get("createdAt")
            if updated_at:
                if hasattr(updated_at, 'replace') and updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                if hasattr(updated_at, 'timestamp'):
                    age = now - updated_at
                    if age > threshold:
                        job["stuckDuration"] = str(age)
                        stuck.append(job)

    return stuck


@st.cache_data(ttl=60)  # 1 minute cache
def fetch_overview_data(project_id):
    """Fetch overview KPIs directly from Firestore."""
    try:
        db = _init_firebase(project_id)
        now = datetime.now(timezone.utc)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_ago = today - timedelta(days=7)
        month_ago = today - timedelta(days=30)

        # [FIX] Pre-fetch accounts for plan resolution
        accounts_map = {}
        for acc_doc in db.collection("accounts").stream():
            acc_data = acc_doc.to_dict()
            accounts_map[acc_doc.id] = acc_data.get("plan", "free")

        # Users
        users_docs = list(db.collection("users").limit(5000).stream())
        total_users = len(users_docs)

        dau = wau = mau = new_users_today = new_users_7d = 0
        plan_counts = {"free": 0, "basic": 0}

        for doc in users_docs:
            data = doc.to_dict()
            last_seen = data.get("lastSeenAt")
            created_at = data.get("createdAt")

            # [FIX] Resolve plan from account first
            account_id = data.get("accountId")
            if account_id and account_id in accounts_map:
                plan = _normalize_plan(accounts_map[account_id])
            else:
                plan = _normalize_plan(data.get("plan", "free"))
            plan_counts[plan] = plan_counts.get(plan, 0) + 1

            if last_seen and hasattr(last_seen, 'replace'):
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)
                if last_seen >= today: dau += 1
                if last_seen >= week_ago: wau += 1
                if last_seen >= month_ago: mau += 1

            if created_at and hasattr(created_at, 'replace'):
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                if created_at >= today: new_users_today += 1
                if created_at >= week_ago: new_users_7d += 1

        # Sessions
        from google.cloud import firestore as gcp_firestore
        sessions_docs = list(db.collection("sessions").order_by("createdAt", direction=gcp_firestore.Query.DESCENDING).limit(2000).stream())

        sessions_today = sessions_7d = 0
        recording_sec_today = recording_sec_7d = 0.0
        jobs_failed_24h = 0
        cloud_sessions = device_sessions = 0

        for doc in sessions_docs:
            data = doc.to_dict()
            created_at = data.get("createdAt")
            duration = data.get("durationSec") or 0
            status = data.get("status", "")
            mode = data.get("transcriptionMode", "")

            if mode == "cloud_google":
                cloud_sessions += 1
            else:
                device_sessions += 1

            if created_at and hasattr(created_at, 'replace'):
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                if created_at >= today:
                    sessions_today += 1
                    recording_sec_today += duration
                if created_at >= week_ago:
                    sessions_7d += 1
                    recording_sec_7d += duration
                if created_at >= (now - timedelta(hours=24)) and status == "failed":
                    jobs_failed_24h += 1

        # Pricing: GCP実績ベースの全サービス合算レート
        # Cloud Speech (Chirp 2): $0.064/min
        # Cloud Run: GCP実績比率から Speech の約2.2倍
        # 全サービス合算: Speech * (1 + 2.2 + 0.08) ≈ Speech * 3.27
        speech_rate_per_min = 0.064  # Chirp 2
        total_multiplier = 3.27  # 全サービス合算/Speech比率 (GCP実績 Feb 2026)
        est_cost_today = (recording_sec_today / 60) * speech_rate_per_min * total_multiplier
        est_cost_7d = (recording_sec_7d / 60) * speech_rate_per_min * total_multiplier

        return {
            "total_users": total_users,
            "dau": dau, "wau": wau, "mau": mau,
            "new_users_today": new_users_today,
            "new_users_7d": new_users_7d,
            "sessions_today": sessions_today,
            "sessions_7d": sessions_7d,
            "recording_min_today": round(recording_sec_today / 60, 1),
            "recording_min_7d": round(recording_sec_7d / 60, 1),
            "est_cost_today": round(est_cost_today, 2),
            "est_cost_7d": round(est_cost_7d, 2),
            "jobs_failed_24h": jobs_failed_24h,
            "plan_counts": plan_counts,
            "cloud_sessions": cloud_sessions,
            "device_sessions": device_sessions
        }
    except Exception as e:
        st.error(f"Overview fetch error: {e}")
        return None

@st.cache_data(ttl=60)
def fetch_user_growth_timeseries(project_id, days=30):
    """Fetch all users with their signup timestamps for cumulative analysis."""
    try:
        db = _init_firebase(project_id)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        users_docs = list(db.collection("users").limit(5000).stream())

        all_users = []
        recent_signups = []
        for doc in users_docs:
            data = doc.to_dict()
            created_at = data.get("createdAt")
            if created_at and hasattr(created_at, 'replace'):
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                all_users.append({
                    "timestamp": created_at,
                    "uid": doc.id,
                })
                if created_at >= cutoff:
                    recent_signups.append({
                        "timestamp": created_at,
                        "uid": doc.id,
                    })

        # Sort all users by timestamp for cumulative calculation
        all_users.sort(key=lambda x: x["timestamp"])

        return {
            "all_users": all_users,
            "recent_signups": recent_signups,
            "total_users": len(all_users),
        }
    except Exception as e:
        st.error(f"User growth fetch error: {e}")
        return {"all_users": [], "recent_signups": [], "total_users": 0}


@st.cache_data(ttl=60)
def fetch_active_subscribers(project_id):
    """
    Fetch active subscribers from entitlements collection.
    A subscriber is active if:
    - status == "active" AND currentPeriodEnd > now
    - OR status in ("grace_period", "billing_retry") AND currentPeriodEnd > now
    """
    try:
        db = _init_firebase(project_id)
        now = datetime.now(timezone.utc)

        # Fetch all entitlements
        entitlements_docs = list(db.collection("entitlements").limit(1000).stream())

        active_subscribers = []
        all_entitlements = []

        for doc in entitlements_docs:
            data = doc.to_dict()
            status = data.get("status", "")
            current_period_end = data.get("currentPeriodEnd")
            created_at = data.get("createdAt") or data.get("purchaseAt")
            owner_account_id = data.get("ownerAccountId")
            owner_user_id = data.get("ownerUserId")
            product_id = data.get("productId", "")
            plan = data.get("plan", "")

            # Check if currently active
            is_active = False
            if status in ("active", "grace_period", "billing_retry"):
                if current_period_end:
                    if hasattr(current_period_end, 'timestamp'):
                        if current_period_end.timestamp() > now.timestamp():
                            is_active = True
                    elif isinstance(current_period_end, str):
                        try:
                            dt = datetime.fromisoformat(current_period_end.replace("Z", "+00:00"))
                            if dt > now:
                                is_active = True
                        except:
                            pass

            entitlement_data = {
                "id": doc.id,
                "status": status,
                "is_active": is_active,
                "owner_account_id": owner_account_id,
                "owner_user_id": owner_user_id,
                "product_id": product_id,
                "plan": plan,
                "current_period_end": current_period_end,
                "created_at": created_at,
            }

            all_entitlements.append(entitlement_data)
            if is_active:
                active_subscribers.append(entitlement_data)

        return {
            "active_subscribers": active_subscribers,
            "all_entitlements": all_entitlements,
            "active_count": len(active_subscribers),
            "total_entitlements": len(all_entitlements),
        }
    except Exception as e:
        st.error(f"Active subscribers fetch error: {e}")
        return {"active_subscribers": [], "all_entitlements": [], "active_count": 0, "total_entitlements": 0}


@st.cache_data(ttl=60)
def fetch_subscription_events(project_id, days=30):
    """Fetch subscription events (new subscriptions and cancellations) for the period."""
    try:
        db = _init_firebase(project_id)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        events = []

        # Fetch Apple notifications for subscription events
        try:
            query = db.collection("apple_notifications").where("receivedAt", ">=", cutoff)
            for doc in query.order_by("receivedAt", direction="DESCENDING").limit(1000).stream():
                data = doc.to_dict()
                received_at = data.get("receivedAt")
                notification_type = data.get("notificationType", "")

                # Classify as subscribe or cancel
                subscribe_types = ["SUBSCRIBED", "INITIAL_BUY", "DID_RENEW"]
                cancel_types = ["CANCEL", "EXPIRED", "DID_FAIL_TO_RENEW", "REFUND", "DID_REVOKE"]

                if notification_type in subscribe_types:
                    event_type = "subscribe"
                elif notification_type in cancel_types:
                    event_type = "cancel"
                else:
                    continue  # Skip other types

                if received_at:
                    events.append({
                        "timestamp": received_at,
                        "type": event_type,
                        "notification_type": notification_type,
                    })
        except Exception:
            pass

        return events
    except Exception as e:
        st.error(f"Subscription events fetch error: {e}")
        return []


@st.cache_data(ttl=60)
def fetch_usage_timeseries(project_id, days=30):
    """
    Fetch time-series data for usage metrics:
    - Sessions (with createdAt timestamps)
    - Cloud STT usage (from sessions with transcriptionMode=cloud_google)
    - Summaries (from jobs with type=summary and status=completed)
    - Quizzes (from jobs with type=quiz and status=completed)
    """
    try:
        db = _init_firebase(project_id)
        from google.cloud import firestore as gcp_firestore
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        # Fetch sessions
        sessions_docs = list(
            db.collection("sessions")
            .order_by("createdAt", direction=gcp_firestore.Query.DESCENDING)
            .limit(5000)
            .stream()
        )

        sessions = []
        cloud_stt_events = []

        for doc in sessions_docs:
            data = doc.to_dict()
            created_at = data.get("createdAt")
            if not created_at or not hasattr(created_at, 'replace'):
                continue

            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            if created_at >= cutoff:
                sessions.append({
                    "timestamp": created_at,
                    "session_id": doc.id,
                    "duration_sec": data.get("durationSec") or 0,
                })

                # Track cloud STT usage
                transcription_mode = data.get("transcriptionMode") or ""
                cloud_entitled = data.get("cloudEntitled", False)
                if transcription_mode == "cloud_google" or cloud_entitled:
                    duration_min = (data.get("durationSec") or 0) / 60
                    cloud_stt_events.append({
                        "timestamp": created_at,
                        "duration_min": duration_min,
                    })

        # Fetch jobs (summaries and quizzes)
        summaries = []
        quizzes = []

        # We need to iterate through sessions to get their jobs
        for session_doc in sessions_docs[:500]:  # Limit to recent sessions
            session_data = session_doc.to_dict()
            created_at = session_data.get("createdAt")
            if not created_at or not hasattr(created_at, 'replace'):
                continue
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if created_at < cutoff:
                continue

            try:
                jobs_docs = list(
                    db.collection("sessions")
                    .document(session_doc.id)
                    .collection("jobs")
                    .limit(20)
                    .stream()
                )
                for job_doc in jobs_docs:
                    job_data = job_doc.to_dict()
                    job_type = job_data.get("type") or job_data.get("jobType") or ""
                    job_status = job_data.get("status") or ""
                    job_created = job_data.get("createdAt") or job_data.get("completedAt") or created_at

                    if job_status == "completed":
                        if job_created and hasattr(job_created, 'replace'):
                            if job_created.tzinfo is None:
                                job_created = job_created.replace(tzinfo=timezone.utc)

                            if job_type in ("summary", "summarize"):
                                summaries.append({"timestamp": job_created})
                            elif job_type == "quiz":
                                quizzes.append({"timestamp": job_created})
            except Exception:
                continue

        return {
            "sessions": sessions,
            "cloud_stt_events": cloud_stt_events,
            "summaries": summaries,
            "quizzes": quizzes,
        }
    except Exception as e:
        st.error(f"Usage timeseries fetch error: {e}")
        return {"sessions": [], "cloud_stt_events": [], "summaries": [], "quizzes": []}


@st.cache_data(ttl=60)
def fetch_entitlements_data(project_id):
    """Fetch all entitlements with owner info."""
    try:
        db = _init_firebase(project_id)
        entitlements = []

        for doc in db.collection("entitlements").limit(500).stream():
            data = doc.to_dict()
            data["id"] = doc.id
            entitlements.append(data)

        return entitlements
    except Exception as e:
        st.error(f"Entitlements fetch error: {e}")
        return []


@st.cache_data(ttl=120)  # Increased TTL to 2 minutes to reduce Firestore reads
def fetch_users_data(project_id, limit=200):
    """Fetch users with session stats and plan limits."""
    try:
        db = _init_firebase(project_id)
        from google.cloud import firestore as gcp_firestore

        now = datetime.now(timezone.utc)
        month_ago = now - timedelta(days=30)
        month_key = now.strftime("%Y-%m")

        users_docs = list(db.collection("users").limit(limit).stream())
        sessions_docs = list(db.collection("sessions").order_by("createdAt", direction=gcp_firestore.Query.DESCENDING).limit(3000).stream())

        # [FIX] Pre-fetch all accounts to get plan info and providers
        accounts_map = {}
        for acc_doc in db.collection("accounts").stream():
            acc_data = acc_doc.to_dict()
            accounts_map[acc_doc.id] = {
                "plan": acc_data.get("plan", "free"),
                "subscriptionStatus": acc_data.get("subscriptionStatus"),
                "appleEntitlementId": acc_data.get("appleEntitlementId"),
                "providers": acc_data.get("providers", []),
            }

        # Pre-fetch all entitlements
        entitlements_map = {}
        entitlements_by_otid = {}
        for ent_doc in db.collection("entitlements").limit(500).stream():
            ent_data = ent_doc.to_dict()
            ent_data["id"] = ent_doc.id
            entitlements_map[ent_doc.id] = ent_data
            owner_acc = ent_data.get("ownerAccountId")
            if owner_acc:
                if owner_acc not in entitlements_by_otid:
                    entitlements_by_otid[owner_acc] = []
                entitlements_by_otid[owner_acc].append(ent_data)

        # [OPTIMIZATION] Pre-fetch all monthly_usage in batch to avoid N individual queries
        monthly_usage_by_account = {}
        monthly_usage_by_user = {}

        # Fetch account monthly_usage
        for acc_id in accounts_map.keys():
            try:
                usage_doc = db.collection("accounts").document(acc_id).collection("monthly_usage").document(month_key).get()
                if usage_doc.exists:
                    monthly_usage_by_account[acc_id] = usage_doc.to_dict()
            except Exception:
                pass

        # Fetch user monthly_usage (only for users not covered by accounts)
        user_ids_with_accounts = set()
        for doc in users_docs:
            data = doc.to_dict()
            acc_id = data.get("accountId")
            if acc_id and acc_id in monthly_usage_by_account:
                user_ids_with_accounts.add(doc.id)

        for doc in users_docs:
            if doc.id not in user_ids_with_accounts:
                try:
                    usage_doc = db.collection("users").document(doc.id).collection("monthly_usage").document(month_key).get()
                    if usage_doc.exists:
                        monthly_usage_by_user[doc.id] = usage_doc.to_dict()
                except Exception:
                    pass

        # Aggregate sessions by user
        user_stats = {}
        for doc in sessions_docs:
            data = doc.to_dict()
            uid = data.get("userId") or data.get("ownerUserId") or data.get("ownerUid")
            created_at = data.get("createdAt")
            duration = data.get("durationSec") or 0

            if not uid: continue
            if uid not in user_stats:
                user_stats[uid] = {"sessions_30d": 0, "recording_sec_30d": 0.0, "recording_sec_lifetime": 0.0, "total_sessions": 0}

            user_stats[uid]["recording_sec_lifetime"] += duration
            user_stats[uid]["total_sessions"] += 1

            if created_at and hasattr(created_at, 'replace'):
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                if created_at >= month_ago:
                    user_stats[uid]["sessions_30d"] += 1
                    user_stats[uid]["recording_sec_30d"] += duration

        users = []
        for doc in users_docs:
            data = doc.to_dict()
            uid = doc.id
            stats = user_stats.get(uid, {"sessions_30d": 0, "recording_sec_30d": 0.0, "recording_sec_lifetime": 0.0, "total_sessions": 0})

            # Determine activity based on multiple signals:
            # 1. lastSeenAt (if set)
            # 2. lastLoginAt (if set)
            # 3. Session activity (from user_stats)
            last_seen = data.get("lastSeenAt")
            last_login = data.get("lastLoginAt")

            # Find the most recent activity timestamp
            latest_activity = None

            # Check lastSeenAt
            if last_seen and hasattr(last_seen, 'replace'):
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)
                latest_activity = last_seen

            # Check lastLoginAt (use if more recent)
            if last_login and hasattr(last_login, 'replace'):
                if last_login.tzinfo is None:
                    last_login = last_login.replace(tzinfo=timezone.utc)
                if latest_activity is None or last_login > latest_activity:
                    latest_activity = last_login

            # Check createdAt as fallback (user is "active" if recently created)
            created_at = data.get("createdAt")
            if created_at and hasattr(created_at, 'replace'):
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                if latest_activity is None or created_at > latest_activity:
                    latest_activity = created_at

            # Determine badge based on latest activity
            active_badge = "inactive"
            if latest_activity:
                delta = now - latest_activity
                if delta < timedelta(days=7):
                    active_badge = "7d"
                elif delta < timedelta(days=30):
                    active_badge = "30d"

            # Also check session activity (if user has sessions in 30d, they're active)
            if active_badge == "inactive" and stats["sessions_30d"] > 0:
                active_badge = "30d"

            # [FIX] Resolve plan from account first, then fallback to user doc
            account_id = data.get("accountId")
            if account_id and account_id in accounts_map:
                plan = accounts_map[account_id].get("plan", "free")
            else:
                plan = data.get("plan", "free")

            # [OPTIMIZED] Use pre-fetched monthly usage data instead of individual queries
            cloud_stt_used = 0.0
            summary_used = 0
            quiz_used = 0

            # Try account monthly_usage first (from pre-fetched data)
            ai_credits_used = 0
            if account_id and account_id in monthly_usage_by_account:
                usage_data = monthly_usage_by_account[account_id]
                cloud_stt_used = usage_data.get("cloud_stt_sec", 0) or 0
                summary_used = usage_data.get("summary_generated", 0) or 0
                quiz_used = usage_data.get("quiz_generated", 0) or 0
                ai_credits_used = int(usage_data.get("ai_credits_used", 0) or 0)

            # Fallback to user monthly_usage (from pre-fetched data)
            if cloud_stt_used == 0 and uid in monthly_usage_by_user:
                usage_data = monthly_usage_by_user[uid]
                cloud_stt_used = usage_data.get("cloud_stt_sec", 0) or 0
                if summary_used == 0:
                    summary_used = usage_data.get("summary_generated", 0) or 0
                if quiz_used == 0:
                    quiz_used = usage_data.get("quiz_generated", 0) or 0
                if ai_credits_used == 0:
                    ai_credits_used = int(usage_data.get("ai_credits_used", 0) or 0)

            # Plan limits info
            server_session_count = data.get("serverSessionCount", stats["total_sessions"])
            cloud_entitled_ids = data.get("cloudEntitledSessionIds", [])

            # Get entitlement info
            entitlement_id = None
            entitlement_status = None
            entitlement_owner = None
            if account_id:
                acc_info = accounts_map.get(account_id, {})
                entitlement_id = acc_info.get("appleEntitlementId")
                if entitlement_id and entitlement_id in entitlements_map:
                    ent = entitlements_map[entitlement_id]
                    entitlement_status = ent.get("status")
                    entitlement_owner = ent.get("ownerAccountId")

            # [FIX] Get providers from account (not user doc)
            acc_providers = []
            if account_id and account_id in accounts_map:
                acc_providers = accounts_map[account_id].get("providers", [])

            users.append({
                "uid": uid,
                "accountId": account_id or "-",
                "username": data.get("username") or "-",
                "displayName": data.get("displayName") or data.get("email") or uid[:8],
                "email": data.get("email") or "-",
                "plan": _normalize_plan(plan),
                "providers": ", ".join(acc_providers) if acc_providers else "-",
                "createdAt": data.get("createdAt"),
                "lastLoginAt": last_login,
                "lastSeenAt": latest_activity,  # Use the most recent activity timestamp
                "sessions_30d": stats["sessions_30d"],
                "recording_min_30d": round(stats["recording_sec_30d"] / 60, 1),
                "recording_min_lifetime": round(stats["recording_sec_lifetime"] / 60, 1),
                "active_badge": active_badge,
                "serverSessionCount": server_session_count,
                "cloudEntitledCount": len(cloud_entitled_ids),
                "cloudSttUsedMin": round(cloud_stt_used / 60, 1),
                "aiCreditsUsed": ai_credits_used,
                "summaryUsed": summary_used,
                "quizUsed": quiz_used,
                "isBlocked": data.get("isBlocked", False),
                "securityScore": data.get("securityScore", 100),
                "entitlementId": entitlement_id or "-",
                "entitlementStatus": entitlement_status or "-",
                "entitlementOwner": entitlement_owner or "-",
            })

        return users
    except Exception as e:
        st.error(f"Users fetch error: {e}")
        return []

def _classify_stt_type(data: dict) -> str:
    """
    Classify session into STT type for dashboard display.

    Returns:
        "☁️ 高精度" - Cloud Google STT (high-precision)
        "📱 標準" - On-device SFSpeechRecognizer (standard)
        "📥 インポート" - Import mode (uploaded audio)
        "❓ 不明" - Unknown
    """
    transcription_mode = data.get("transcriptionMode") or ""
    source = data.get("source") or ""
    stt_mode = data.get("sttMode") or ""
    cloud_entitled = data.get("cloudEntitled", False)
    transcript_source = data.get("transcriptSource") or ""

    # Cloud Google STT (high-precision)
    if transcription_mode == "cloud_google" or cloud_entitled:
        return "☁️ 高精度"

    # Import mode (uploaded audio with batch transcription)
    if transcription_mode == "import" or source == "import":
        return "📥 インポート"

    # On-device (standard)
    if transcription_mode in ("on_device", "local", "") or transcript_source in ("device", "local"):
        return "📱 標準"

    # Fallback
    if transcription_mode:
        return f"❓ {transcription_mode}"

    return "❓ 不明"


@st.cache_data(ttl=60)  # 1 minute cache
def fetch_sessions_data(project_id, limit=200):
    """Fetch sessions with detailed info."""
    try:
        db = _init_firebase(project_id)
        from google.cloud import firestore as gcp_firestore

        docs = list(db.collection("sessions").order_by("createdAt", direction=gcp_firestore.Query.DESCENDING).limit(limit).stream())

        sessions = []
        for doc in docs:
            data = doc.to_dict()
            sessions.append({
                "id": doc.id,
                "title": data.get("title") or "-",
                "userId": data.get("userId") or data.get("ownerUserId") or data.get("ownerUid") or "-",
                "status": data.get("status") or "unknown",
                "mode": data.get("mode") or "-",
                "transcriptionMode": data.get("transcriptionMode") or "-",
                "sttType": _classify_stt_type(data),  # [NEW] Classified STT type
                "source": data.get("source") or "-",  # [NEW] Source (ios, import, etc.)
                "createdAt": data.get("createdAt"),
                "durationSec": data.get("durationSec") or 0,
                "audioStatus": data.get("audioStatus") or "-",
                "summaryStatus": data.get("summaryStatus") or "-",
                "quizStatus": data.get("quizStatus") or "-",
                "cloudEntitled": data.get("cloudEntitled", False),
                "hasTranscript": bool(data.get("transcriptText")),
                "transcriptLen": len(data.get("transcriptText") or ""),  # [NEW] Transcript length
                "deletedAt": data.get("deletedAt")
            })
        return sessions
    except Exception as e:
        st.error(f"Sessions fetch error: {e}")
        return []

@st.cache_data(ttl=60)  # 1 minute cache
def fetch_jobs_data(project_id, session_id=None, limit=100):
    """Fetch jobs from sessions subcollections."""
    try:
        db = _init_firebase(project_id)
        from google.cloud import firestore as gcp_firestore

        jobs = []

        if session_id:
            # Fetch jobs for specific session
            jobs_docs = list(db.collection("sessions").document(session_id).collection("jobs").limit(limit).stream())
            for doc in jobs_docs:
                data = doc.to_dict()
                jobs.append({
                    "jobId": doc.id,
                    "sessionId": session_id,
                    "type": data.get("type", "-"),
                    "status": data.get("status", "-"),
                    "createdAt": data.get("createdAt"),
                    "errorReason": data.get("errorReason") or data.get("error") or "-"
                })
        else:
            # Fetch recent sessions and their jobs
            sessions_docs = list(db.collection("sessions").order_by("createdAt", direction=gcp_firestore.Query.DESCENDING).limit(50).stream())

            for session_doc in sessions_docs:
                sid = session_doc.id
                jobs_docs = list(db.collection("sessions").document(sid).collection("jobs").limit(10).stream())
                for doc in jobs_docs:
                    data = doc.to_dict()
                    jobs.append({
                        "jobId": doc.id,
                        "sessionId": sid,
                        "type": data.get("type", "-"),
                        "status": data.get("status", "-"),
                        "createdAt": data.get("createdAt"),
                        "errorReason": data.get("errorReason") or data.get("error") or "-"
                    })

        return jobs
    except Exception as e:
        st.error(f"Jobs fetch error: {e}")
        return []

@st.cache_data(ttl=60)
def fetch_pricing_config(project_id):
    """Fetch pricing config."""
    try:
        db = _init_firebase(project_id)
        doc = db.collection("pricing_config").document("current").get()
        if doc.exists:
            return doc.to_dict()
        return {
            "llm_input_per_1k_tokens_usd": 0.0015,
            "llm_output_per_1k_tokens_usd": 0.002,
            "storage_gb_month_usd": 0.02,
            "speech_per_min_usd": 0.024,
            "cloudrun_shared_monthly_usd": 50.0
        }
    except Exception as e:
        st.warning(f"Pricing fetch error: {e}")
        return {}

def fetch_document(project_id, collection_path, doc_id):
    """Fetch a single document."""
    try:
        db = _init_firebase(project_id)
        parts = collection_path.split("/")
        ref = db.collection(parts[0])
        for i in range(1, len(parts), 2):
            if i + 1 < len(parts):
                ref = ref.document(parts[i]).collection(parts[i + 1])
        doc = ref.document(doc_id).get()
        if doc.exists:
            return _serialize_for_json(doc.to_dict())
        return None
    except Exception as e:
        st.error(f"Document fetch error: {e}")
        return None

def fetch_collection(project_id, collection_path, limit=100):
    """Fetch documents from a collection."""
    try:
        db = _init_firebase(project_id)
        parts = collection_path.split("/")
        ref = db.collection(parts[0])
        for i in range(1, len(parts), 2):
            if i + 1 < len(parts):
                ref = ref.document(parts[i]).collection(parts[i + 1])

        docs = list(ref.limit(limit).stream())
        result = []
        for doc in docs:
            data = _serialize_for_json(doc.to_dict())
            data["_id"] = doc.id
            result.append(data)
        return result
    except Exception as e:
        st.error(f"Collection fetch error: {e}")
        return []

# ---------------------------------------------------------
# 4.5 Sidebar: Subscriber Count (after function defs)
# ---------------------------------------------------------
@st.cache_data(ttl=120)
def fetch_paid_accounts_count(project_id):
    """Count accounts with plan != free (source of truth for paid users)."""
    try:
        db = _init_firebase(project_id)
        docs = list(db.collection("accounts").where("plan", "in", ["basic", "standard"]).stream())
        return len(docs)
    except Exception as e:
        st.error(f"Paid accounts fetch error: {e}")
        return 0

with st.sidebar:
    st.divider()
    _paid_count = fetch_paid_accounts_count(selected_project)
    st.metric("💳 有料ユーザー (合計)", f"{_paid_count}")

# ---------------------------------------------------------
# 5. Main Tabs
# ---------------------------------------------------------
tab_overview, tab_events, tab_limits, tab_plans, tab_users, tab_sessions, tab_jobs, tab_db, tab_costs, tab_perf, tab_config, tab_youtube = st.tabs([
    "📊 Overview", "🔔 Events", "📈 Limits", "💳 Plans", "👥 Users", "📝 Sessions", "⚙️ Tasks", "🗄️ Database", "💰 Costs", "🚀 Performance", "🔧 Config", "📺 YouTube"
])

# ---------------------------------------------------------
# TAB 1: Overview
# ---------------------------------------------------------
with tab_overview:
    st.markdown("## 📊 Overview")

    data = fetch_overview_data(selected_project)
    user_growth_data = fetch_user_growth_timeseries(selected_project, days=30)
    subscribers_data = fetch_active_subscribers(selected_project)
    subscription_events = fetch_subscription_events(selected_project, days=30)

    if data:
        # =========================================================
        # 🎯 Goal: 10,000 Active Users
        # =========================================================
        MAU_GOAL = 10000
        current_mau = data["mau"]
        progress_pct = min(100, (current_mau / MAU_GOAL) * 100)

        st.markdown("### 🎯 Goal: 10,000 Monthly Active Users")

        # Progress bar with custom styling
        col_progress, col_stats = st.columns([3, 1])
        with col_progress:
            st.progress(progress_pct / 100)
            st.caption(f"**{current_mau:,} / {MAU_GOAL:,}** ({progress_pct:.1f}%) - あと **{MAU_GOAL - current_mau:,}** ユーザー")
        with col_stats:
            # Calculate days to goal based on 7d growth rate
            new_users_7d = data["new_users_7d"]
            if new_users_7d > 0:
                daily_rate = new_users_7d / 7
                days_to_goal = (MAU_GOAL - current_mau) / daily_rate if daily_rate > 0 else float('inf')
                if days_to_goal < 365:
                    st.metric("達成予測", f"{int(days_to_goal)} 日後")
                else:
                    st.metric("達成予測", "1年以上")
            else:
                st.metric("達成予測", "-")

        st.divider()

        # =========================================================
        # KPI Cards - Using accounts collection as source of truth
        # =========================================================
        active_subs_count = fetch_paid_accounts_count(selected_project)

        # Calculate new subscriptions in 7d from events
        new_subs_7d = sum(1 for e in subscription_events if e.get("type") == "subscribe")
        canceled_7d = sum(1 for e in subscription_events if e.get("type") == "cancel")

        k1, k2, k3, k4 = st.columns(4)
        with k1:
            st.metric("総登録者数", f"{data['total_users']:,}")
        with k2:
            st.metric("DAU / WAU / MAU", f"{data['dau']} / {data['wau']} / {data['mau']}")
        with k3:
            st.metric("💳 アクティブサブスク", f"{active_subs_count:,}")
        with k4:
            # Show conversion rate
            conversion_rate = (active_subs_count / data['total_users'] * 100) if data['total_users'] > 0 else 0
            st.metric("課金率", f"{conversion_rate:.1f}%")

        st.divider()

        # KPI Row 2 - Growth metrics
        k5, k6, k7, k8 = st.columns(4)
        with k5:
            st.metric("新規登録 (7日)", f"+{data['new_users_7d']}", delta=f"{data['new_users_7d']/7:.1f}/日")
        with k6:
            st.metric("新規サブスク (30日)", f"+{new_subs_7d}")
        with k7:
            st.metric("解約 (30日)", f"-{canceled_7d}", delta_color="inverse")
        with k8:
            net_sub = new_subs_7d - canceled_7d
            st.metric("サブスク純増", f"{net_sub:+d}", delta_color="normal" if net_sub >= 0 else "inverse")

        st.divider()

        # =========================================================
        # 📈 Cumulative Charts: Users & Subscribers
        # =========================================================
        st.markdown("### 📈 推移グラフ")

        col_users, col_subs = st.columns(2)

        with col_users:
            st.markdown("#### 👥 総登録者数の推移")
            all_users = user_growth_data.get("all_users", [])
            if all_users:
                # Create cumulative user count over time
                users_df = pd.DataFrame(all_users)
                users_df["timestamp"] = pd.to_datetime(users_df["timestamp"].apply(
                    lambda x: x.isoformat() if hasattr(x, 'isoformat') else x
                ))
                users_df = users_df.sort_values("timestamp")

                # Convert to JST
                if users_df["timestamp"].dt.tz is None:
                    users_df["timestamp"] = users_df["timestamp"].dt.tz_localize("UTC")
                users_df["timestamp"] = users_df["timestamp"].dt.tz_convert(JST)

                # Add cumulative count
                users_df["cumulative"] = range(1, len(users_df) + 1)

                # Filter to last 30 days for display, but show cumulative from start
                cutoff_30d = datetime.now(timezone.utc) - timedelta(days=30)
                cutoff_30d = cutoff_30d.astimezone(JST)
                display_df = users_df[users_df["timestamp"] >= cutoff_30d].copy()

                if not display_df.empty:
                    fig = go.Figure()

                    # Cumulative line (no fill to allow dynamic Y-axis)
                    fig.add_trace(go.Scatter(
                        x=display_df["timestamp"],
                        y=display_df["cumulative"],
                        mode="lines+markers",
                        name="登録者数",
                        line=dict(color=COLOR_BLUE, width=2),
                        marker=dict(size=3)
                    ))

                    # Get min/max for dynamic range with padding
                    y_min = display_df["cumulative"].min()
                    y_max = display_df["cumulative"].max()
                    y_padding = max(10, (y_max - y_min) * 0.1)

                    fig.update_layout(
                        title="総登録者数 (過去30日)",
                        xaxis_title="",
                        yaxis_title="ユーザー数",
                        yaxis=dict(
                            range=[y_min - y_padding, y_max + y_padding],
                            autorange=False
                        ),
                        height=300,
                        margin=dict(l=20, r=20, t=40, b=20),
                        showlegend=False
                    )

                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("過去30日のデータがありません")
            else:
                st.info("ユーザーデータがありません")

        with col_subs:
            st.markdown("#### 💳 サブスク人数の推移")

            # Build historical subscriber count from entitlements
            all_entitlements = subscribers_data.get("all_entitlements", [])
            if all_entitlements:
                # Create timeline of subscriber changes based on entitlement creation dates
                # and current_period_end dates
                now = datetime.now(timezone.utc)
                cutoff_30d = now - timedelta(days=30)

                # Build a list of events: (timestamp, delta)
                # +1 when entitlement starts, -1 when it expires
                events = []
                for ent in all_entitlements:
                    created_at = ent.get("created_at")
                    period_end = ent.get("current_period_end")
                    status = ent.get("status", "")

                    # Skip revoked
                    if status == "revoked":
                        continue

                    if created_at and hasattr(created_at, 'isoformat'):
                        events.append({"timestamp": created_at, "delta": 1, "type": "start"})

                    # If expired in the past, add the end event
                    if period_end and hasattr(period_end, 'timestamp'):
                        if period_end.timestamp() < now.timestamp():
                            events.append({"timestamp": period_end, "delta": -1, "type": "end"})

                if events:
                    events_df = pd.DataFrame(events)
                    events_df["timestamp"] = pd.to_datetime(events_df["timestamp"].apply(
                        lambda x: x.isoformat() if hasattr(x, 'isoformat') else x
                    ), format='ISO8601')
                    events_df = events_df.sort_values("timestamp")

                    # Convert to JST
                    if events_df["timestamp"].dt.tz is None:
                        events_df["timestamp"] = events_df["timestamp"].dt.tz_localize("UTC")
                    events_df["timestamp"] = events_df["timestamp"].dt.tz_convert(JST)

                    # Calculate cumulative subscriber count
                    events_df["cumulative"] = events_df["delta"].cumsum()

                    # Filter to last 30 days
                    cutoff_jst = cutoff_30d.astimezone(JST)
                    display_df = events_df[events_df["timestamp"] >= cutoff_jst].copy()

                    if not display_df.empty:
                        fig = go.Figure()

                        fig.add_trace(go.Scatter(
                            x=display_df["timestamp"],
                            y=display_df["cumulative"],
                            mode="lines",
                            name="サブスク人数",
                            line=dict(color=COLOR_GREEN, width=2),
                            fill="tozeroy",
                            fillcolor="rgba(16, 185, 129, 0.1)"
                        ))

                        # Add current count marker
                        fig.add_hline(
                            y=active_subs_count,
                            line_dash="dot",
                            line_color=COLOR_YELLOW,
                            annotation_text=f"現在: {active_subs_count}",
                            annotation_position="top right"
                        )

                        fig.update_layout(
                            title="アクティブサブスク数 (過去30日)",
                            xaxis_title="",
                            yaxis_title="サブスク人数",
                            height=300,
                            margin=dict(l=20, r=20, t=40, b=20),
                            showlegend=False
                        )

                        st.plotly_chart(fig, use_container_width=True)
                    else:
                        st.metric("現在のサブスク人数", f"{active_subs_count}")
                        st.info("過去30日のサブスク履歴がありません")
                else:
                    st.metric("現在のサブスク人数", f"{active_subs_count}")
                    st.info("エンタイトルメント履歴がありません")
            else:
                st.metric("現在のサブスク人数", f"{active_subs_count}")
                st.info("サブスクリプションデータがありません")

        st.divider()

        # =========================================================
        # Active Subscribers Detail
        # =========================================================
        if active_subs_count > 0:
            with st.expander(f"💳 アクティブサブスク詳細 ({active_subs_count}件)"):
                active_list = subscribers_data.get("active_subscribers", [])
                if active_list:
                    # Build account_id to username lookup
                    users_data_for_lookup = fetch_users_data(selected_project, limit=500)
                    account_to_username = {}
                    uid_to_username = {}
                    for u in users_data_for_lookup:
                        acc_id = u.get("accountId")
                        uid = u.get("uid")
                        username = u.get("username") or u.get("displayName") or "-"
                        if acc_id and acc_id != "-":
                            account_to_username[acc_id] = username
                        if uid:
                            uid_to_username[uid] = username

                    subs_table = []
                    for s in active_list:
                        period_end = s.get("current_period_end")
                        if period_end and hasattr(period_end, 'strftime'):
                            end_str = _format_timestamp_short(period_end)
                        else:
                            end_str = "-"

                        # Lookup username
                        owner_account = s.get("owner_account_id") or ""
                        owner_user = s.get("owner_user_id") or ""
                        username = account_to_username.get(owner_account) or uid_to_username.get(owner_user) or "-"

                        subs_table.append({
                            "Username": username,
                            "Account ID": owner_account or owner_user or "-",
                            "Product": s.get("product_id", "-"),
                            "Status": s.get("status", "-"),
                            "有効期限": end_str,
                        })
                    st.dataframe(pd.DataFrame(subs_table), use_container_width=True, hide_index=True)

        st.divider()

        # Plan Distribution & Session Types
        col1, col2 = st.columns(2)

        with col1:
            st.markdown("### Plan Distribution")
            plan_df = pd.DataFrame([
                {"Plan": "Free", "Count": data["plan_counts"].get("free", 0)},
                {"Plan": "Basic (Subscribed)", "Count": active_subs_count},
            ])
            fig = px.pie(plan_df, values="Count", names="Plan", hole=0.4,
                        color_discrete_sequence=[COLOR_GRAY, COLOR_GREEN])
            fig.update_layout(margin=dict(l=20, r=20, t=30, b=20), height=250)
            st.plotly_chart(fig, use_container_width=True)

        with col2:
            st.markdown("### Session Types")
            session_type_df = pd.DataFrame([
                {"Type": "Cloud", "Count": data["cloud_sessions"]},
                {"Type": "On-Device", "Count": data["device_sessions"]},
            ])
            fig = px.pie(session_type_df, values="Count", names="Type", hole=0.4,
                        color_discrete_sequence=[COLOR_BLUE, COLOR_GREEN])
            fig.update_layout(margin=dict(l=20, r=20, t=30, b=20), height=250)
            st.plotly_chart(fig, use_container_width=True)

        st.divider()

        # Today stats
        st.markdown("### Today")
        t1, t2, t3, t4 = st.columns(4)
        with t1: st.metric("Sessions Today", data["sessions_today"])
        with t2: st.metric("Recording Today", f"{data['recording_min_today']} min")
        with t3: st.metric("Est. Cost Today", f"¥{data['est_cost_today'] * 150:.0f}")
        with t4: st.metric("⚠️ Failed (24h)", data["jobs_failed_24h"], delta_color="inverse")
    else:
        st.warning("Failed to load overview data.")

# ---------------------------------------------------------
# TAB 2: Events (ops_events monitoring)
# ---------------------------------------------------------
with tab_events:
    st.markdown("## 🔔 Events Monitor")
    st.caption("Monitor ops_events collection for real-time operational events (※ 時刻は日本時間 JST で表示)")

    # Time range selector
    col_time, col_severity, col_type = st.columns([1, 1, 2])
    with col_time:
        events_hours = st.selectbox("Time Range", [1, 6, 24, 168], index=2, format_func=lambda x: f"{x}h" if x < 48 else f"{x//24}d")
    with col_severity:
        severity_filter = st.multiselect("Severity", ["INFO", "WARN", "ERROR"], default=["WARN", "ERROR"])
    with col_type:
        event_types = ["SESSION_CREATE", "JOB_QUEUED", "JOB_STARTED", "JOB_COMPLETED", "JOB_FAILED",
                       "STT_STARTED", "STT_COMPLETED", "STT_FAILED", "LLM_STARTED", "LLM_COMPLETED", "LLM_FAILED",
                       "LIMIT_REACHED", "PAYMENT_REQUIRED", "ABUSE_DETECTED", "API_ERROR"]
        type_filter = st.multiselect("Event Type", event_types, default=[])

    events_data = fetch_ops_events(selected_project, hours=events_hours)

    if events_data:
        df = pd.DataFrame(events_data)

        # Apply filters
        if severity_filter:
            df = df[df["severity"].isin(severity_filter)]
        if type_filter:
            df = df[df["type"].isin(type_filter)]

        # KPI metrics
        k1, k2, k3, k4 = st.columns(4)
        total_events = len(df)
        error_count = len(df[df["severity"] == "ERROR"]) if "severity" in df.columns else 0
        limit_count = len(df[df["type"] == "LIMIT_REACHED"]) if "type" in df.columns else 0
        failure_rate = round(error_count / total_events * 100, 1) if total_events > 0 else 0

        with k1: st.metric("Total Events", total_events)
        with k2: st.metric("Errors", error_count, delta_color="inverse")
        with k3: st.metric("Limit Reached", limit_count, delta_color="inverse")
        with k4: st.metric("Failure Rate", f"{failure_rate}%", delta_color="inverse")

        st.divider()

        # Timeline chart
        if not df.empty and "ts" in df.columns:
            df["ts_dt"] = pd.to_datetime(df["ts"].apply(lambda x: x.isoformat() if hasattr(x, 'isoformat') else x))
            df_chart = df.copy()
            if "severity" in df_chart.columns:
                fig = px.histogram(df_chart, x="ts_dt", color="severity",
                                  color_discrete_map={"INFO": COLOR_BLUE, "WARN": COLOR_YELLOW, "ERROR": COLOR_RED},
                                  title="Events Timeline")
                fig.update_layout(height=250, margin=dict(l=20, r=20, t=40, b=20))
                st.plotly_chart(fig, use_container_width=True)

        # Events table
        if not df.empty:
            df["Time"] = df["ts"].apply(lambda x: _format_timestamp_short(x) if hasattr(x, 'strftime') else str(x))
            df["Sev"] = df["severity"].apply(lambda x: "🔴" if x == "ERROR" else "🟡" if x == "WARN" else "🔵")

            display_cols = ["Time", "Sev", "type", "uid", "message", "errorCode"]
            available_cols = [c for c in display_cols if c in df.columns]
            display_df = df[available_cols].head(200)

            st.dataframe(display_df, use_container_width=True, hide_index=True, height=400)

            # Event details expander
            with st.expander("🔍 View Event Details"):
                if "id" in df.columns:
                    selected_event_id = st.selectbox("Select Event", df["id"].tolist()[:50])
                    if selected_event_id:
                        event_doc = df[df["id"] == selected_event_id].iloc[0].to_dict()
                        st.json(_serialize_for_json(event_doc))
    else:
        st.info("No events found in the selected time range.")

# ---------------------------------------------------------
# TAB 3: Limits (Usage & Limits Monitoring)
# ---------------------------------------------------------
with tab_limits:
    st.markdown("## 📈 Usage Limits Monitor")
    st.caption("ユーザーごとのプラン制限と今月の使用量を監視")

    # Plan limits constants
    PLAN_LIMITS = {
        "free": {"cloud_stt_min": 0, "summary": 3, "quiz": 3},
        "basic": {"cloud_stt_min": 120, "summary": 100, "quiz": 100},
    }

    # Fetch users data with usage info
    users_data = fetch_users_data(selected_project, limit=500)

    # Build user lookup for username
    if users_data:
        # Create DataFrame
        df = pd.DataFrame(users_data)

        # Search filter
        col_search, col_plan_filter = st.columns([2, 1])
        with col_search:
            search_query = st.text_input("🔍 検索", placeholder="Username, Account ID...", key="limits_search")
        with col_plan_filter:
            plan_filter = st.selectbox("プラン", ["All", "free", "basic"], key="limits_plan")

        # Apply filters
        if search_query:
            mask = (
                df["username"].str.contains(search_query, case=False, na=False) |
                df["accountId"].str.contains(search_query, case=False, na=False) |
                df["displayName"].str.contains(search_query, case=False, na=False)
            )
            df = df[mask]

        if plan_filter != "All":
            df = df[df["plan"] == plan_filter]

        # Calculate remaining and format for display
        def calc_remaining_stt(row):
            plan = row["plan"]
            used = row.get("cloudSttUsedMin", 0)
            limit = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])["cloud_stt_min"]
            remaining = max(0, limit - used)
            return remaining

        def format_stt_usage(row):
            plan = row["plan"]
            used = row.get("cloudSttUsedMin", 0)
            limit = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])["cloud_stt_min"]
            if limit == 0:
                return f"{used:.1f} / 0"
            pct = (used / limit) * 100 if limit > 0 else 0
            return f"{used:.1f} / {limit} ({pct:.0f}%)"

        def format_summary_usage(row):
            plan = row["plan"]
            used = row.get("summaryUsed", 0)
            limit = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])["summary"]
            pct = (used / limit) * 100 if limit > 0 else 0
            return f"{used} / {limit} ({pct:.0f}%)"

        def format_quiz_usage(row):
            plan = row["plan"]
            used = row.get("quizUsed", 0)
            limit = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])["quiz"]
            pct = (used / limit) * 100 if limit > 0 else 0
            return f"{used} / {limit} ({pct:.0f}%)"

        df["Remaining STT (min)"] = df.apply(calc_remaining_stt, axis=1)
        df["Cloud STT"] = df.apply(format_stt_usage, axis=1)
        df["Summary"] = df.apply(format_summary_usage, axis=1)
        df["Quiz"] = df.apply(format_quiz_usage, axis=1)
        df["Total Rec (min)"] = df["recording_min_lifetime"]
        df["Plan Badge"] = df["plan"].apply(lambda x: "⭐ Basic" if x == "basic" else "🆓 Free")

        # KPI metrics
        k1, k2, k3, k4 = st.columns(4)
        total_users = len(df)
        basic_users = len(df[df["plan"] == "basic"])
        free_users = len(df[df["plan"] == "free"])
        users_near_limit = len(df[(df["plan"] == "basic") & (df["Remaining STT (min)"] < 20)])

        with k1: st.metric("Total Users", total_users)
        with k2: st.metric("Basic Users", basic_users)
        with k3: st.metric("Free Users", free_users)
        with k4: st.metric("Near Limit (<20min)", users_near_limit, delta_color="inverse" if users_near_limit > 0 else "normal")

        st.divider()

        # Main usage table
        st.markdown("### ユーザー別使用量")

        display_df = df[[
            "accountId", "username", "Plan Badge", "Total Rec (min)",
            "Cloud STT", "Summary", "Quiz", "Remaining STT (min)"
        ]].rename(columns={
            "accountId": "Account ID",
            "username": "Username",
            "Plan Badge": "Plan",
            "Total Rec (min)": "総録音時間",
            "Cloud STT": "クラウドSTT",
            "Summary": "要約",
            "Quiz": "クイズ",
            "Remaining STT (min)": "残りSTT",
        })

        # Sort by Remaining STT ascending (show users near limit first)
        display_df = display_df.sort_values("残りSTT", ascending=True)

        st.caption(f"Showing {len(display_df)} users")
        st.dataframe(display_df, use_container_width=True, hide_index=True, height=400)

    else:
        st.info("ユーザーデータが見つかりません。")

    st.divider()

    # =========================================================
    # 📅 Daily Session Stats (per-day breakdown)
    # =========================================================
    st.markdown("### 📅 日別セッション統計")

    try:
        db = _init_firebase(selected_project)
        from google.cloud import firestore as gcp_firestore
        from collections import defaultdict as _defaultdict

        _now = datetime.now(timezone.utc)
        _start = _now - timedelta(days=30)

        _sess_docs = list(
            db.collection("sessions")
            .where("createdAt", ">=", _start)
            .order_by("createdAt")
            .stream()
        )

        # Collect user display names
        _uid_set = set()
        _daily = _defaultdict(lambda: {"sessions": 0, "users": set(), "cloud": 0, "device": 0, "total_min": 0.0, "with_transcript": 0, "with_summary": 0, "user_min": _defaultdict(float)})
        for _s in _sess_docs:
            _d = _s.to_dict()
            _created = _d.get("createdAt")
            if not _created:
                continue
            _day = _created.astimezone(JST).strftime("%m/%d (%a)")
            _uid = _d.get("ownerUid") or _d.get("userId") or "unknown"
            _dur = (_d.get("durationSec") or 0) / 60.0
            _mode = _d.get("transcriptionMode", "")

            _daily[_day]["sessions"] += 1
            _daily[_day]["users"].add(_uid)
            _daily[_day]["total_min"] += _dur
            _daily[_day]["user_min"][_uid] += _dur
            if "cloud" in _mode:
                _daily[_day]["cloud"] += 1
            else:
                _daily[_day]["device"] += 1
            if len(_d.get("transcriptText", "") or "") > 0:
                _daily[_day]["with_transcript"] += 1
            if _d.get("summaryMarkdown"):
                _daily[_day]["with_summary"] += 1
            _uid_set.add(_uid)

        # Resolve display names
        _names = {}
        for _uid in _uid_set:
            try:
                _udoc = db.collection("users").document(_uid).get(["displayName", "name"])
                if _udoc.exists:
                    _ud = _udoc.to_dict()
                    _names[_uid] = _ud.get("displayName") or _ud.get("name") or _uid[:8]
                else:
                    _names[_uid] = _uid[:8]
            except:
                _names[_uid] = _uid[:8]

        # Build table rows
        _rows = []
        for _day in sorted(_daily.keys(), reverse=True):
            _v = _daily[_day]
            _user_details = []
            for _uid in sorted(_v["user_min"], key=lambda u: -_v["user_min"][u]):
                _m = round(_v["user_min"][_uid], 1)
                _n = _names.get(_uid, _uid[:8])
                _user_details.append(f"{_n}({_m}m)")
            _rows.append({
                "日付": _day,
                "Sessions": _v["sessions"],
                "Users": len(_v["users"]),
                "Cloud": _v["cloud"],
                "Device": _v["device"],
                "文字起こし": _v["with_transcript"],
                "要約": _v["with_summary"],
                "合計(分)": round(_v["total_min"], 1),
                "ユーザー内訳": ", ".join(_user_details),
            })

        if _rows:
            _daily_df = pd.DataFrame(_rows)
            st.dataframe(_daily_df, use_container_width=True, hide_index=True, height=500)
            st.caption(f"過去30日間: 合計 {len(_sess_docs)} セッション")
        else:
            st.info("セッションデータがありません")
    except Exception as _e:
        st.error(f"日別統計の取得に失敗: {_e}")

    st.divider()

    # =========================================================
    # 📊 Usage Time Series Charts
    # =========================================================
    st.markdown("### 📊 全ユーザー総合 使用量推移")

    # Fetch usage time-series data
    usage_ts_data = fetch_usage_timeseries(selected_project, days=30)

    # Time aggregation selector
    time_agg_limits = st.radio("集計単位", ["時間別", "日別"], horizontal=True, key="limits_time_agg")
    agg_format = "%Y-%m-%d %H:00" if time_agg_limits == "時間別" else "%Y-%m-%d"

    # --- Cloud STT Usage ---
    st.markdown("#### ☁️ クラウド文字起こし時間")
    cloud_stt_events = usage_ts_data.get("cloud_stt_events", [])
    if cloud_stt_events:
        stt_df = pd.DataFrame(cloud_stt_events)
        stt_df["timestamp"] = pd.to_datetime(stt_df["timestamp"].apply(
            lambda x: x.isoformat() if hasattr(x, 'isoformat') else x
        ))
        if stt_df["timestamp"].dt.tz is None:
            stt_df["timestamp"] = stt_df["timestamp"].dt.tz_localize("UTC")
        stt_df["timestamp"] = stt_df["timestamp"].dt.tz_convert(JST)
        stt_df["period"] = stt_df["timestamp"].dt.strftime(agg_format)

        # Aggregate by period
        stt_agg = stt_df.groupby("period").agg({"duration_min": "sum"}).reset_index()
        stt_agg = stt_agg.sort_values("period")
        stt_agg["cumulative"] = stt_agg["duration_min"].cumsum()

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=stt_agg["period"],
            y=stt_agg["cumulative"],
            mode="lines+markers",
            name="累積クラウドSTT (分)",
            line=dict(color=COLOR_BLUE, width=2),
            marker=dict(size=4)
        ))
        fig.update_layout(
            title=f"クラウド文字起こし累積時間 ({time_agg_limits})",
            xaxis_title="",
            yaxis_title="累積時間 (分)",
            height=280,
            margin=dict(l=20, r=20, t=40, b=60),
            xaxis_tickangle=-45
        )
        st.plotly_chart(fig, use_container_width=True)
        st.caption(f"合計: **{stt_agg['duration_min'].sum():.1f}** 分 (過去30日)")
    else:
        st.info("クラウドSTTデータがありません")

    # --- Sessions, Summaries, Quizzes Charts ---
    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown("#### 📝 総セッション数")
        sessions = usage_ts_data.get("sessions", [])
        if sessions:
            sess_df = pd.DataFrame(sessions)
            sess_df["timestamp"] = pd.to_datetime(sess_df["timestamp"].apply(
                lambda x: x.isoformat() if hasattr(x, 'isoformat') else x
            ))
            if sess_df["timestamp"].dt.tz is None:
                sess_df["timestamp"] = sess_df["timestamp"].dt.tz_localize("UTC")
            sess_df["timestamp"] = sess_df["timestamp"].dt.tz_convert(JST)
            sess_df["period"] = sess_df["timestamp"].dt.strftime(agg_format)

            sess_agg = sess_df.groupby("period").size().reset_index(name="count")
            sess_agg = sess_agg.sort_values("period")
            sess_agg["cumulative"] = sess_agg["count"].cumsum()

            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=sess_agg["period"],
                y=sess_agg["cumulative"],
                mode="lines+markers",
                name="累積セッション数",
                line=dict(color=COLOR_GREEN, width=2),
                marker=dict(size=3)
            ))
            fig.update_layout(
                title="累積セッション数",
                xaxis_title="",
                yaxis_title="セッション数",
                height=250,
                margin=dict(l=20, r=20, t=40, b=60),
                xaxis_tickangle=-45,
                showlegend=False
            )
            st.plotly_chart(fig, use_container_width=True)
            st.caption(f"合計: **{len(sessions):,}** セッション")
        else:
            st.info("セッションデータがありません")

    with col2:
        st.markdown("#### 📄 総要約数")
        summaries = usage_ts_data.get("summaries", [])
        if summaries:
            sum_df = pd.DataFrame(summaries)
            sum_df["timestamp"] = pd.to_datetime(sum_df["timestamp"].apply(
                lambda x: x.isoformat() if hasattr(x, 'isoformat') else x
            ))
            if sum_df["timestamp"].dt.tz is None:
                sum_df["timestamp"] = sum_df["timestamp"].dt.tz_localize("UTC")
            sum_df["timestamp"] = sum_df["timestamp"].dt.tz_convert(JST)
            sum_df["period"] = sum_df["timestamp"].dt.strftime(agg_format)

            sum_agg = sum_df.groupby("period").size().reset_index(name="count")
            sum_agg = sum_agg.sort_values("period")
            sum_agg["cumulative"] = sum_agg["count"].cumsum()

            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=sum_agg["period"],
                y=sum_agg["cumulative"],
                mode="lines+markers",
                name="累積要約数",
                line=dict(color=COLOR_YELLOW, width=2),
                marker=dict(size=3)
            ))
            fig.update_layout(
                title="累積要約数",
                xaxis_title="",
                yaxis_title="要約数",
                height=250,
                margin=dict(l=20, r=20, t=40, b=60),
                xaxis_tickangle=-45,
                showlegend=False
            )
            st.plotly_chart(fig, use_container_width=True)
            st.caption(f"合計: **{len(summaries):,}** 要約")
        else:
            st.info("要約データがありません")

    with col3:
        st.markdown("#### 🧠 総クイズ生成数")
        quizzes = usage_ts_data.get("quizzes", [])
        if quizzes:
            quiz_df = pd.DataFrame(quizzes)
            quiz_df["timestamp"] = pd.to_datetime(quiz_df["timestamp"].apply(
                lambda x: x.isoformat() if hasattr(x, 'isoformat') else x
            ))
            if quiz_df["timestamp"].dt.tz is None:
                quiz_df["timestamp"] = quiz_df["timestamp"].dt.tz_localize("UTC")
            quiz_df["timestamp"] = quiz_df["timestamp"].dt.tz_convert(JST)
            quiz_df["period"] = quiz_df["timestamp"].dt.strftime(agg_format)

            quiz_agg = quiz_df.groupby("period").size().reset_index(name="count")
            quiz_agg = quiz_agg.sort_values("period")
            quiz_agg["cumulative"] = quiz_agg["count"].cumsum()

            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=quiz_agg["period"],
                y=quiz_agg["cumulative"],
                mode="lines+markers",
                name="累積クイズ数",
                line=dict(color=COLOR_RED, width=2),
                marker=dict(size=3)
            ))
            fig.update_layout(
                title="累積クイズ生成数",
                xaxis_title="",
                yaxis_title="クイズ数",
                height=250,
                margin=dict(l=20, r=20, t=40, b=60),
                xaxis_tickangle=-45,
                showlegend=False
            )
            st.plotly_chart(fig, use_container_width=True)
            st.caption(f"合計: **{len(quizzes):,}** クイズ")
        else:
            st.info("クイズデータがありません")

    st.divider()

    # Limit reached events (24h)
    st.markdown("### ⚠️ Limit Reached Events (24h)")
    limit_events = fetch_ops_events(selected_project, hours=24)
    limit_events = [e for e in limit_events if e.get("type") == "LIMIT_REACHED"]

    if limit_events:
        st.caption("※ 時刻は日本時間 (JST) で表示")
        limit_df = pd.DataFrame(limit_events)
        limit_df["Time"] = limit_df["ts"].apply(lambda x: _format_timestamp_short(x) if hasattr(x, 'strftime') else str(x))
        limit_df["User"] = limit_df.get("uid", "-")

        # Extract limit type from debug or message
        def extract_limit_type(row):
            debug = row.get("debug", {})
            if isinstance(debug, dict):
                return debug.get("limitType", "-")
            msg = str(row.get("message", ""))
            if "free" in msg.lower():
                return "free"
            if "basic" in msg.lower():
                return "basic"
            return "-"

        limit_df["Limit Type"] = limit_df.apply(extract_limit_type, axis=1)

        # KPI for denials
        k1, k2, k3, k4 = st.columns(4)
        stt_denied = sum(1 for e in limit_events if "stt" in str(e.get("debug", {})).lower() or "cloud" in str(e.get("message", "")).lower())
        summary_denied = sum(1 for e in limit_events if "summary" in str(e.get("debug", {})).lower() or "summary" in str(e.get("message", "")).lower())
        quiz_denied = sum(1 for e in limit_events if "quiz" in str(e.get("debug", {})).lower() or "quiz" in str(e.get("message", "")).lower())

        with k1: st.metric("Total Denials", len(limit_events), delta_color="inverse")
        with k2: st.metric("STT Denied", stt_denied, delta_color="inverse")
        with k3: st.metric("Summary Denied", summary_denied, delta_color="inverse")
        with k4: st.metric("Quiz Denied", quiz_denied, delta_color="inverse")

        display_df = limit_df[["Time", "User", "Limit Type", "message"]].head(50) if "message" in limit_df.columns else limit_df[["Time", "User", "Limit Type"]].head(50)
        st.dataframe(display_df, use_container_width=True, hide_index=True, height=200)
    else:
        st.success("過去24時間に制限到達イベントはありません。")

# ---------------------------------------------------------
# TAB 4: Plans (Plan Changes Monitoring)
# ---------------------------------------------------------
with tab_plans:
    st.markdown("## 💳 Plan Changes Monitor")
    st.caption("Monitor Apple subscription notifications and plan transitions (※ 時刻は日本時間 JST で表示)")

    # Time range
    plans_days = st.selectbox("Time Range", [1, 7, 30], index=1, format_func=lambda x: f"{x} day{'s' if x > 1 else ''}")

    notifications = fetch_apple_notifications(selected_project, days=plans_days)

    if notifications:
        df = pd.DataFrame(notifications)

        # Count by notification type
        if "notificationType" in df.columns:
            type_counts = df["notificationType"].value_counts().to_dict()
        else:
            type_counts = {}

        # KPI metrics
        k1, k2, k3, k4 = st.columns(4)
        with k1: st.metric("Total Notifications", len(df))
        with k2: st.metric("Subscribed", type_counts.get("SUBSCRIBED", 0) + type_counts.get("DID_RENEW", 0))
        with k3: st.metric("Cancelled/Expired", type_counts.get("CANCEL", 0) + type_counts.get("EXPIRED", 0) + type_counts.get("DID_FAIL_TO_RENEW", 0), delta_color="inverse")
        with k4: st.metric("Renewals", type_counts.get("DID_RENEW", 0))

        st.divider()

        # Timeline chart
        if "receivedAt" in df.columns:
            df["ts_dt"] = pd.to_datetime(df["receivedAt"].apply(lambda x: x.isoformat() if hasattr(x, 'isoformat') else x))
            if "notificationType" in df.columns:
                fig = px.histogram(df, x="ts_dt", color="notificationType", title="Notification Timeline")
                fig.update_layout(height=250, margin=dict(l=20, r=20, t=40, b=20))
                st.plotly_chart(fig, use_container_width=True)

        # Notifications table (JST)
        df["Time"] = df["receivedAt"].apply(lambda x: _format_timestamp_short(x) if hasattr(x, 'strftime') else str(x)) if "receivedAt" in df.columns else "-"

        display_cols = ["Time", "notificationType", "subtype", "productId", "originalTransactionId"]
        available_cols = [c for c in display_cols if c in df.columns]

        st.markdown("### Recent Notifications")
        st.dataframe(df[available_cols].head(100), use_container_width=True, hide_index=True, height=300)

        # Failed notifications
        if "processedAt" in df.columns:
            failed = df[df["processedAt"].isna()]
            if not failed.empty:
                with st.expander(f"⚠️ Unprocessed Notifications ({len(failed)})"):
                    st.dataframe(failed[available_cols], use_container_width=True, hide_index=True)
    else:
        st.info("No Apple notifications found in the selected time range.")

# ---------------------------------------------------------
# TAB 5: Users
# ---------------------------------------------------------

def _format_provider_icons(providers_str: str) -> str:
    """Convert provider names to icons for display."""
    if not providers_str or providers_str == "-":
        return "❓"

    icon_map = {
        "google.com": "🔵",      # Google
        "apple.com": "🍎",       # Apple
        "line.me": "💚",         # LINE
        "custom": "💚",          # LINE (custom token)
        "line": "💚",            # LINE (alternative)
        "password": "🔑",        # Email/Password
        "phone": "📱",           # Phone
        "anonymous": "👤",       # Anonymous
        "twitter.com": "🐦",     # Twitter/X
        "facebook.com": "📘",    # Facebook
        "github.com": "🐙",      # GitHub
    }

    providers = [p.strip() for p in providers_str.split(",")]
    icons = []
    for p in providers:
        icon = icon_map.get(p, "❓")
        icons.append(icon)

    return " ".join(icons)

def _get_primary_provider(providers_str: str) -> str:
    """Get the primary provider name for filtering."""
    if not providers_str or providers_str == "-":
        return "unknown"

    providers = [p.strip() for p in providers_str.split(",")]

    # Priority order for primary provider
    priority = ["apple.com", "google.com", "line.me", "phone", "password", "anonymous"]
    for p in priority:
        if p in providers:
            return p

    return providers[0] if providers else "unknown"

with tab_users:
    st.markdown("## 👥 Users")
    st.caption("※ 時刻は日本時間 (JST) で表示 | Provider: 🔵Google 🍎Apple 💚LINE 🔑Email 📱Phone | 🆕=24h以内 🌟=7d以内")

    col_filter, col_new, col_plan, col_provider, col_search = st.columns([1, 1, 1, 1, 2])
    with col_filter:
        filter_active = st.selectbox("Activity", ["All", "7d", "30d", "inactive"], index=0)
    with col_new:
        filter_new = st.selectbox("登録日", ["All", "🆕 24h以内", "🌟 7d以内", "30d以内"], index=0)
    with col_plan:
        filter_plan = st.selectbox("Plan", ["All", "free", "basic"], index=0)
    with col_provider:
        filter_provider = st.selectbox("Provider", ["All", "🔵 Google", "🍎 Apple", "💚 LINE", "🔑 Email", "📱 Phone"], index=0)
    with col_search:
        search_query = st.text_input("🔍 Search", placeholder="Username, name, email, UID, accountId...")

    users_data = fetch_users_data(selected_project, limit=500)
    entitlements_data = fetch_entitlements_data(selected_project)

    # Build entitlement lookup
    entitlements_by_id = {e["id"]: e for e in entitlements_data}
    entitlements_by_account = {}
    for e in entitlements_data:
        owner = e.get("ownerAccountId")
        if owner:
            if owner not in entitlements_by_account:
                entitlements_by_account[owner] = []
            entitlements_by_account[owner].append(e)

    if users_data:
        df = pd.DataFrame(users_data)

        # Apply filters
        if search_query:
            mask = (
                df["displayName"].str.contains(search_query, case=False, na=False) |
                df["email"].str.contains(search_query, case=False, na=False) |
                df["uid"].str.contains(search_query, case=False, na=False) |
                df["username"].str.contains(search_query, case=False, na=False) |
                df["accountId"].str.contains(search_query, case=False, na=False)
            )
            df = df[mask]

        if filter_active != "All":
            df = df[df["active_badge"] == filter_active]

        if filter_plan != "All":
            df = df[df["plan"] == filter_plan]

        # New user filtering
        now_utc = datetime.now(timezone.utc)
        cutoff_24h = now_utc - timedelta(hours=24)
        cutoff_7d = now_utc - timedelta(days=7)
        cutoff_30d = now_utc - timedelta(days=30)

        def is_new_user(created_at, cutoff):
            if pd.isna(created_at):
                return False
            if hasattr(created_at, 'replace'):
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                return created_at >= cutoff
            return False

        if filter_new == "🆕 24h以内":
            df = df[df["createdAt"].apply(lambda x: is_new_user(x, cutoff_24h))]
        elif filter_new == "🌟 7d以内":
            df = df[df["createdAt"].apply(lambda x: is_new_user(x, cutoff_7d))]
        elif filter_new == "30d以内":
            df = df[df["createdAt"].apply(lambda x: is_new_user(x, cutoff_30d))]

        # Provider filtering
        if filter_provider != "All":
            provider_patterns = {
                "🔵 Google": "google.com",
                "🍎 Apple": "apple.com",
                "💚 LINE": "custom|line",  # LINE uses "custom" provider ID
                "🔑 Email": "password",
                "📱 Phone": "phone",
            }
            pattern = provider_patterns.get(filter_provider, "")
            if pattern:
                df = df[df["providers"].str.contains(pattern, case=False, na=False, regex=True)]

        # Format for display (JST)
        df["Last Seen"] = df["lastSeenAt"].apply(lambda x: _format_timestamp_short(x) if pd.notna(x) else "-")
        df["Last Login"] = df["lastLoginAt"].apply(lambda x: _format_timestamp_short(x) if pd.notna(x) else "-")
        df["Created"] = df["createdAt"].apply(lambda x: _format_timestamp_short(x) if pd.notna(x) else "-")
        df["Active"] = df["active_badge"].apply(lambda x: "🟢" if x == "7d" else "🟡" if x == "30d" else "⚪")
        df["Plan Badge"] = df["plan"].apply(lambda x: "⭐ Basic" if x == "basic" else "🆓 Free")

        # New user badge
        def get_new_badge(created_at):
            if pd.isna(created_at):
                return ""
            if hasattr(created_at, 'replace'):
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                if created_at >= cutoff_24h:
                    return "🆕"
                elif created_at >= cutoff_7d:
                    return "🌟"
            return ""

        df["New"] = df["createdAt"].apply(get_new_badge)

        # Entitlement status badge
        def format_entitlement(row):
            ent_id = row.get("entitlementId", "-")
            ent_status = row.get("entitlementStatus", "-")
            if ent_id == "-" or ent_status == "-":
                return "❌ None"
            if ent_status == "active":
                return f"✅ {ent_id[:20]}..."
            return f"⚠️ {ent_status}"

        df["Entitlement"] = df.apply(format_entitlement, axis=1)

        # [FIX] Show actual usage instead of limits
        def format_usage(row):
            plan = row["plan"]
            ai_cr = row.get("aiCreditsUsed", 0)
            summary = row.get("summaryUsed", 0)
            quiz = row.get("quizUsed", 0)
            if plan == "basic":
                return f"AI:{ai_cr}/400 S:{summary}/100 Q:{quiz}/100"
            else:
                return f"AI:{ai_cr}/40 S:{summary}/3 Q:{quiz}/3"

        df["Usage"] = df.apply(format_usage, axis=1)

        # Provider icons
        df["Provider"] = df["providers"].apply(_format_provider_icons)

        # Sort by created date (newest first) if filtering for new users
        if filter_new != "All":
            df = df.sort_values("createdAt", ascending=False)

        # Format usage columns for display
        df["総録音"] = df["recording_min_lifetime"].apply(lambda x: f"{x:.0f}分")
        def _format_ai_credits(row):
            used = row.get("aiCreditsUsed", 0)
            plan = row.get("plan", "free")
            limit = 400 if plan == "basic" else 40
            return f"{used}/{limit}"
        df["AIクレジット"] = df.apply(_format_ai_credits, axis=1)
        df["要約"] = df["summaryUsed"].astype(str)
        df["クイズ"] = df["quizUsed"].astype(str)

        display_df = df[[
            "Active", "Provider", "username", "displayName", "accountId", "Plan Badge",
            "総録音", "AIクレジット", "要約", "クイズ",
            "Created", "Last Seen"
        ]].rename(columns={
            "username": "Username",
            "displayName": "Name",
            "accountId": "Account ID",
            "Plan Badge": "Plan",
            "Created": "登録日時",
            "Last Seen": "最終活動",
        })

        # Count new users for metrics
        new_24h_count = len(df[df["New"] == "🆕"])
        new_7d_count = len(df[df["New"].isin(["🆕", "🌟"])])

        # Show new user stats
        col_stat1, col_stat2, col_stat3, col_stat4 = st.columns(4)
        with col_stat1:
            st.metric("表示中", f"{len(display_df)} users")
        with col_stat2:
            st.metric("🆕 24h以内", new_24h_count)
        with col_stat3:
            st.metric("🌟 7d以内", new_7d_count)
        with col_stat4:
            total_recording = df["recording_min_lifetime"].sum()
            st.metric("総録音時間", f"{total_recording:.0f}分")

        st.dataframe(display_df, use_container_width=True, hide_index=True, height=450)

        # Entitlements Summary
        st.divider()
        st.markdown("### 🔐 Entitlements Summary")

        if entitlements_data:
            ent_df = pd.DataFrame(entitlements_data)
            ent_df["Owner Account"] = ent_df["ownerAccountId"].fillna("-")
            ent_df["Status"] = ent_df["status"].fillna("-")
            ent_df["Product"] = ent_df["productId"].fillna("-")
            ent_df["Expires"] = ent_df.get("currentPeriodEnd", pd.Series([None]*len(ent_df))).apply(
                lambda x: _format_timestamp_short(x) if pd.notna(x) and hasattr(x, 'strftime') else "-"
            )

            # Find username for each owner account
            account_to_username = {}
            for u in users_data:
                acc_id = u.get("accountId")
                username = u.get("username", "-")
                if acc_id and acc_id != "-":
                    account_to_username[acc_id] = username

            ent_df["Owner Username"] = ent_df["ownerAccountId"].apply(
                lambda x: account_to_username.get(x, "-") if x else "-"
            )

            display_ent_df = ent_df[["id", "Owner Username", "Owner Account", "Status", "Product", "Expires"]].rename(columns={
                "id": "Entitlement ID",
            })
            st.dataframe(display_ent_df, use_container_width=True, hide_index=True)

            # Show accounts trying to claim same entitlement (409 candidates)
            st.markdown("#### ⚠️ Potential 409 Conflicts")
            st.caption("Accounts that might be trying to claim an entitlement owned by another account")

            # Group users by their claimed/attempted entitlement
            conflicts = []
            for ent in entitlements_data:
                ent_id = ent.get("id")
                owner_acc = ent.get("ownerAccountId")
                owner_username = account_to_username.get(owner_acc, "-")

                # Find users whose account is NOT the owner but might try to claim
                for u in users_data:
                    u_acc = u.get("accountId")
                    u_ent = u.get("entitlementId")
                    u_username = u.get("username", "-")
                    # If user's account doesn't own this entitlement but has reference to it
                    if u_acc and u_acc != owner_acc:
                        # This is a potential conflict - user with different account
                        pass  # We'll show all users grouped by entitlement below

            # For now, just show entitlement owners clearly
            st.info("上記テーブルで各 Entitlement の所有者を確認できます。同じ originalTransactionId に対して異なる Account が claim しようとすると 409 エラーになります。")
        else:
            st.info("No entitlements found.")

        # User detail expander
        with st.expander("🔍 View User Details"):
            # Create display options with username
            user_options = []
            for _, row in df.iterrows():
                username = row.get("username", "-")
                display_name = row.get("displayName", "-")
                uid = row.get("uid", "-")
                label = f"{username} ({display_name}) - {uid[:12]}..."
                user_options.append((label, uid))

            selected_option = st.selectbox("Select User", [opt[0] for opt in user_options])
            selected_uid = next((opt[1] for opt in user_options if opt[0] == selected_option), None)

            if selected_uid:
                user_row = df[df["uid"] == selected_uid].iloc[0]

                col1, col2, col3 = st.columns(3)
                with col1:
                    st.markdown("**User Info**")
                    st.text(f"UID: {user_row['uid']}")
                    st.text(f"Account ID: {user_row.get('accountId', '-')}")
                    st.text(f"Username: {user_row.get('username', '-')}")
                    st.text(f"Plan: {user_row['plan']}")
                    st.text(f"Email: {user_row['email']}")

                with col2:
                    st.markdown("**Monthly Usage**")
                    ai_cr = user_row.get('aiCreditsUsed', 0)
                    ai_limit = 400 if user_row['plan'] == 'basic' else 40
                    st.text(f"AI Credits: {ai_cr}/{ai_limit}")
                    st.text(f"Summaries: {user_row.get('summaryUsed', 0)}")
                    st.text(f"Quizzes: {user_row.get('quizUsed', 0)}")

                with col3:
                    st.markdown("**Entitlement**")
                    ent_id = user_row.get('entitlementId', '-')
                    ent_status = user_row.get('entitlementStatus', '-')
                    ent_owner = user_row.get('entitlementOwner', '-')
                    st.text(f"ID: {ent_id}")
                    st.text(f"Status: {ent_status}")
                    st.text(f"Owner Account: {ent_owner}")

                    # Check if owner matches
                    account_id = user_row.get('accountId', '-')
                    if ent_owner != "-" and ent_owner != account_id:
                        st.error(f"⚠️ Ownership mismatch! This account ({account_id}) does not own this entitlement.")
                    elif ent_owner == account_id and ent_status == "active":
                        st.success("✅ Entitlement valid and owned by this account")

                st.divider()
                st.markdown("**Raw User Document**")
                user_doc = fetch_document(selected_project, "users", selected_uid)
                if user_doc:
                    st.json(user_doc)

                # Also show account document if exists
                account_id = user_row.get('accountId')
                if account_id and account_id != "-":
                    st.markdown("**Account Document**")
                    acc_doc = fetch_document(selected_project, "accounts", account_id)
                    if acc_doc:
                        st.json(acc_doc)

                    # Show entitlement document if exists
                    ent_id = user_row.get('entitlementId')
                    if ent_id and ent_id != "-":
                        st.markdown("**Entitlement Document**")
                        ent_doc = fetch_document(selected_project, "entitlements", ent_id)
                        if ent_doc:
                            st.json(ent_doc)
    else:
        st.warning("No user data available.")

# ---------------------------------------------------------
# TAB 3: Sessions
# ---------------------------------------------------------
with tab_sessions:
    st.markdown("## 📝 Sessions")
    st.caption("※ 時刻は日本時間 (JST) で表示 | STT: ☁️高精度=Cloud STT, 📱標準=On-Device, 📥インポート=Batch")

    col_status, col_stt, col_limit = st.columns([1, 1, 1])
    with col_status:
        filter_status = st.selectbox("Status", ["All", "録音中", "録音済み", "処理中", "要約済み", "failed"])
    with col_stt:
        # [UPDATED] Filter by STT type instead of raw transcriptionMode
        filter_stt = st.selectbox("STT Type", ["All", "☁️ 高精度", "📱 標準", "📥 インポート"])
    with col_limit:
        session_limit = st.slider("Limit", 50, 500, 200)

    sessions_data = fetch_sessions_data(selected_project, limit=session_limit)

    # Fetch users for username mapping
    users_data = fetch_users_data(selected_project, limit=500)
    uid_to_username = {}
    if users_data:
        for u in users_data:
            uid = u.get("uid")
            username = u.get("username") or u.get("displayName") or "-"
            if uid:
                uid_to_username[uid] = username

    if sessions_data:
        df = pd.DataFrame(sessions_data)

        # Filter out deleted
        df = df[df["deletedAt"].isna()]

        if filter_status != "All":
            df = df[df["status"] == filter_status]

        # [UPDATED] Filter by STT type
        if filter_stt != "All":
            df = df[df["sttType"] == filter_stt]

        df["Time"] = df["createdAt"].apply(lambda x: _format_timestamp_short(x) if pd.notna(x) else "-")
        df["Duration"] = (df["durationSec"] / 60).round(1).astype(str) + " min"
        df["Title"] = df["title"].apply(lambda x: str(x)[:30] if x else "-")
        # [UPDATED] Use sttType instead of simple Cloud emoji
        df["STT"] = df["sttType"]  # Already classified: ☁️ 高精度 / 📱 標準 / 📥 インポート
        df["Transcript"] = df.apply(lambda row: f"✅ ({row['transcriptLen']:,})" if row['hasTranscript'] else "❌", axis=1)
        df["Summary"] = df["summaryStatus"].apply(lambda x: "✅" if x == "completed" else "⏳" if x in ["running", "queued"] else "❌" if x == "failed" else "-")
        df["Quiz"] = df["quizStatus"].apply(lambda x: "✅" if x == "completed" else "⏳" if x in ["running", "queued"] else "❌" if x == "failed" else "-")

        # Map userId to username
        df["Username"] = df["userId"].apply(lambda x: uid_to_username.get(x, "-") if x and x != "-" else "-")

        display_df = df[["Time", "STT", "Title", "Username", "userId", "status", "Duration", "Transcript", "Summary", "Quiz"]].rename(columns={
            "userId": "User ID"
        })

        st.caption(f"Showing {len(display_df)} sessions")
        st.dataframe(display_df, use_container_width=True, hide_index=True, height=400)

        # Session detail expander
        with st.expander("🔍 View Session Details"):
            selected_sid = st.selectbox("Select Session", df["id"].tolist())
            if selected_sid:
                session_doc = fetch_document(selected_project, "sessions", selected_sid)
                if session_doc:
                    st.json(session_doc)

                # Show jobs for this session
                st.markdown("#### Jobs")
                jobs = fetch_jobs_data(selected_project, session_id=selected_sid)
                if jobs:
                    jobs_df = pd.DataFrame(jobs)
                    st.dataframe(jobs_df, use_container_width=True, hide_index=True)
    else:
        st.warning("No session data available.")

# ---------------------------------------------------------
# TAB 7: Tasks/Jobs (Enhanced)
# ---------------------------------------------------------
with tab_jobs:
    st.markdown("## ⚙️ Tasks & Jobs Monitor")
    st.caption("Monitor job status, detect stuck tasks, and analyze processing times (※ 時刻は日本時間 JST で表示)")

    # Time range and filters
    col_hours, col_type, col_status_job = st.columns([1, 1, 1])
    with col_hours:
        jobs_hours = st.selectbox("Time Range", [6, 24, 72, 168], index=1, format_func=lambda x: f"{x}h" if x < 48 else f"{x//24}d", key="jobs_hours")
    with col_type:
        job_type_filter = st.selectbox("Job Type", ["All", "summary", "quiz", "transcribe", "qa", "summarize"])
    with col_status_job:
        job_status_filter = st.selectbox("Job Status", ["All", "queued", "running", "completed", "failed", "cancelled"])

    # Use enhanced jobs fetch
    jobs_data = fetch_all_jobs_enhanced(selected_project, hours=jobs_hours)

    if jobs_data:
        df = pd.DataFrame(jobs_data)

        # Detect stuck jobs BEFORE filtering
        stuck_jobs = detect_stuck_jobs(jobs_data, threshold_minutes=10)

        # Apply filters
        if job_type_filter != "All":
            df = df[df["type"] == job_type_filter]
        if job_status_filter != "All":
            df = df[df["status"] == job_status_filter]

        # Summary stats with stuck count
        s1, s2, s3, s4, s5 = st.columns(5)
        with s1: st.metric("Total Jobs", len(df))
        with s2: st.metric("Completed", len(df[df["status"] == "completed"]))
        with s3: st.metric("Running/Queued", len(df[df["status"].isin(["running", "queued"])]))
        with s4: st.metric("Failed", len(df[df["status"] == "failed"]), delta_color="inverse")
        with s5:
            if stuck_jobs:
                st.metric("⚠️ Stuck", len(stuck_jobs), delta_color="inverse")
            else:
                st.metric("Stuck", 0)

        # ALERT: Stuck jobs
        if stuck_jobs:
            st.error(f"🚨 ALERT: {len(stuck_jobs)} stuck task(s) detected (running > 10 min)")
            with st.expander("View Stuck Tasks", expanded=True):
                stuck_df = pd.DataFrame(stuck_jobs)
                stuck_df["Time"] = stuck_df["createdAt"].apply(lambda x: _format_timestamp_short(x) if hasattr(x, 'strftime') else "-")
                stuck_df["Session"] = stuck_df["sessionId"].apply(lambda x: x[:12] + "..." if x else "-")
                display_stuck = stuck_df[["Time", "type", "status", "Session", "stuckDuration", "ownerUid"]]
                st.dataframe(display_stuck, use_container_width=True, hide_index=True)

        st.divider()

        # Processing time analysis
        completed_jobs = df[df["status"] == "completed"].copy()
        if not completed_jobs.empty and "durationSec" in completed_jobs.columns:
            completed_jobs = completed_jobs[completed_jobs["durationSec"].notna()]

            if not completed_jobs.empty:
                st.markdown("### Processing Time Analysis")

                # Stats by job type
                if "type" in completed_jobs.columns:
                    time_stats = completed_jobs.groupby("type")["durationSec"].agg(["mean", "median", lambda x: x.quantile(0.9)]).reset_index()
                    time_stats.columns = ["Job Type", "Mean (s)", "Median (s)", "P90 (s)"]
                    time_stats = time_stats.round(1)

                    col_stats, col_chart = st.columns([1, 2])
                    with col_stats:
                        st.dataframe(time_stats, use_container_width=True, hide_index=True)
                    with col_chart:
                        fig = px.box(completed_jobs, x="type", y="durationSec", title="Processing Time by Job Type")
                        fig.update_layout(height=250, margin=dict(l=20, r=20, t=40, b=20))
                        fig.update_yaxes(title="Duration (seconds)")
                        st.plotly_chart(fig, use_container_width=True)

        st.divider()

        # Jobs table
        df["Time"] = df["createdAt"].apply(lambda x: _format_timestamp_short(x) if pd.notna(x) else "-")
        df["Status Icon"] = df["status"].apply(lambda x: "✅" if x == "completed" else "⏳" if x in ["running", "queued"] else "❌" if x == "failed" else "🚫" if x == "cancelled" else "❓")
        df["Duration"] = df["durationSec"].apply(lambda x: f"{x:.1f}s" if pd.notna(x) else "-")
        df["Session"] = df["sessionId"].apply(lambda x: x[:12] + "..." if x else "-")

        display_df = df[["Time", "Status Icon", "type", "status", "Session", "Duration", "errorReason"]].rename(columns={
            "Status Icon": "✓",
            "type": "Type",
            "status": "Status",
            "errorReason": "Error"
        })

        st.markdown("### All Jobs")
        st.dataframe(display_df.head(200), use_container_width=True, hide_index=True, height=350)

        # Failed/Cancelled jobs details
        failed_jobs = df[df["status"].isin(["failed", "cancelled"])]
        if not failed_jobs.empty:
            with st.expander(f"⚠️ Failed/Cancelled Jobs ({len(failed_jobs)})"):
                # Group by error reason
                if "errorReason" in failed_jobs.columns:
                    error_counts = failed_jobs["errorReason"].value_counts().head(10)
                    st.markdown("**Error Distribution:**")
                    for error, count in error_counts.items():
                        st.text(f"  {count}x - {error}")

                st.divider()
                for _, job in failed_jobs.head(20).iterrows():
                    st.markdown(f"**{job['type']}** - Session: `{job['sessionId'][:12]}...`")
                    st.code(job.get("errorReason", "-"))

        # Job type success rate
        if "type" in df.columns and len(df) > 0:
            st.divider()
            st.markdown("### Success Rate by Job Type")
            type_stats = df.groupby("type").apply(
                lambda x: pd.Series({
                    "Total": len(x),
                    "Success": len(x[x["status"] == "completed"]),
                    "Failed": len(x[x["status"] == "failed"]),
                    "Rate": f"{len(x[x['status'] == 'completed']) / len(x) * 100:.1f}%" if len(x) > 0 else "0%"
                })
            ).reset_index()
            st.dataframe(type_stats, use_container_width=True, hide_index=True)
    else:
        st.info("No jobs data available.")

# ---------------------------------------------------------
# TAB 5: Database Explorer
# ---------------------------------------------------------
with tab_db:
    st.markdown("## 🗄️ Database Explorer")

    st.markdown("Browse Firestore collections and documents directly.")

    # Collection selector
    collections = ["users", "sessions", "pricing_config", "share_codes", "translations", "active_streams"]

    col_coll, col_doc = st.columns([1, 2])

    with col_coll:
        selected_collection = st.selectbox("Collection", collections)
        custom_path = st.text_input("Or enter custom path", placeholder="sessions/{id}/jobs")

    with col_doc:
        doc_limit = st.slider("Document limit", 10, 200, 50)
        doc_id_input = st.text_input("Document ID (optional)", placeholder="Fetch specific document...")

    if st.button("🔍 Fetch Data"):
        path = custom_path if custom_path else selected_collection

        if doc_id_input:
            # Fetch single document
            doc = fetch_document(selected_project, path, doc_id_input)
            if doc:
                st.success(f"Document: {doc_id_input}")
                st.json(doc)
            else:
                st.warning("Document not found")
        else:
            # Fetch collection
            docs = fetch_collection(selected_project, path, limit=doc_limit)
            if docs:
                st.success(f"Found {len(docs)} documents in `{path}`")

                # Show as table if possible
                try:
                    df = pd.DataFrame(docs)
                    # Flatten nested objects for display
                    for col in df.columns:
                        if df[col].apply(lambda x: isinstance(x, (dict, list))).any():
                            df[col] = df[col].apply(lambda x: json.dumps(x, default=str) if isinstance(x, (dict, list)) else x)
                    st.dataframe(df, use_container_width=True, hide_index=True, height=400)
                except Exception:
                    # Fallback to JSON
                    for doc in docs[:20]:
                        with st.expander(f"📄 {doc.get('_id', 'unknown')}"):
                            st.json(doc)
            else:
                st.warning("No documents found")

    st.divider()

    # Quick session lookup
    st.markdown("### Quick Lookup")
    lookup_type = st.radio("Lookup by", ["Session ID", "User ID", "Email"], horizontal=True)
    lookup_value = st.text_input("Enter value...")

    if lookup_value and st.button("🔎 Search"):
        db = _init_firebase(selected_project)

        if lookup_type == "Session ID":
            doc = fetch_document(selected_project, "sessions", lookup_value)
            if doc:
                st.json(doc)
            else:
                st.warning("Session not found")

        elif lookup_type == "User ID":
            doc = fetch_document(selected_project, "users", lookup_value)
            if doc:
                st.json(doc)

                # Also show their sessions
                st.markdown("#### User's Sessions")
                try:
                    sessions = list(db.collection("sessions").where("ownerUid", "==", lookup_value).limit(20).stream())
                    for s in sessions:
                        data = s.to_dict()
                        st.text(f"📝 {s.id} - {data.get('title', '-')} ({data.get('status', '-')})")
                except Exception as e:
                    st.warning(f"Could not fetch sessions: {e}")
            else:
                st.warning("User not found")

        elif lookup_type == "Email":
            try:
                users = list(db.collection("users").where("email", "==", lookup_value).limit(1).stream())
                if users:
                    st.json(_serialize_for_json(users[0].to_dict()))
                else:
                    st.warning("User not found")
            except Exception as e:
                st.error(f"Search failed: {e}")

# ---------------------------------------------------------
# TAB 6: Costs
# ---------------------------------------------------------
with tab_costs:
    st.markdown("## 💰 Costs")

    # ── GCP実績ベースのレート (asia-northeast1, Feb 2026 verified) ──
    # Cloud Run (cpu-throttling=ON, CPU=1, Memory=2GiB)
    CR_VCPU_SEC = 0.00002400       # $/vCPU-second
    CR_MEM_GIB_SEC = 0.00000250    # $/GiB-second
    CR_CPU = 1
    CR_MEM_GIB = 2
    CR_REQ_PER_M = 0.40            # $/million requests
    CR_PER_SEC = (CR_VCPU_SEC * CR_CPU) + (CR_MEM_GIB_SEC * CR_MEM_GIB)  # $0.0000290/sec

    # Cloud Speech-to-Text V2 (Chirp 2)
    STT_RATE_CHIRP = 0.064         # $0.016/15sec = $0.064/min

    # Vertex AI (Gemini 2.0 Flash Lite)
    LLM_RATE_PER_CALL = 0.001      # ~$0.001/call (4K input + 2K output tokens)

    # Fixed overhead (Artifact Registry + Storage + Secret Manager + Build)
    FIXED_OVERHEAD_DAY_USD = 0.25  # ~$0.25/day ($7.5/month)

    JPY_RATE = 150

    # GCP実績比率 (Feb 2026: Cloud Run ≈ 2.19x Speech)
    CR_TO_SPEECH_RATIO = 2.19

    # ── 使用量データ取得 ──
    usage_data = fetch_monthly_usage_all(selected_project)

    total_stt_sec = 0
    total_summary = 0
    total_quiz = 0
    total_llm = 0
    usage_by_entity = []

    for u in usage_data:
        stt = u.get("cloud_stt_sec", 0) or 0
        summary = u.get("summary_generated", 0) or 0
        quiz = u.get("quiz_generated", 0) or 0
        llm = u.get("llm_calls", 0) or 0

        total_stt_sec += stt
        total_summary += summary
        total_quiz += quiz
        total_llm += llm

        entity_id = u.get("accountId") or u.get("userId") or "unknown"
        if stt > 0 or summary > 0 or quiz > 0:
            usage_by_entity.append({
                "Entity": entity_id[:20],
                "Type": u.get("_type", "unknown"),
                "STT (min)": round(stt / 60, 1),
                "Summaries": summary,
                "Quizzes": quiz,
                "LLM Calls": llm,
            })

    total_stt_min = total_stt_sec / 60
    total_llm_calls = total_summary + total_quiz + total_llm

    # ── コスト計算 ──
    # 月初からの日数
    now = datetime.now()
    days_elapsed = max(now.day, 1)

    # Speech cost (正確: STT分数 × レート)
    cost_speech = total_stt_min * STT_RATE_CHIRP

    # Cloud Run cost (GCP実績比率から推定)
    cost_cloud_run = cost_speech * CR_TO_SPEECH_RATIO

    # Vertex AI cost
    cost_vertex = total_llm_calls * LLM_RATE_PER_CALL

    # Fixed overhead
    cost_fixed = FIXED_OVERHEAD_DAY_USD * days_elapsed

    # Total
    cost_total = cost_speech + cost_cloud_run + cost_vertex + cost_fixed

    # ── Session count for unit cost ──
    overview_data_for_costs = fetch_overview_data(selected_project)
    sessions_month = 0
    if overview_data_for_costs:
        # Approximate monthly sessions from 7d data
        sessions_7d = overview_data_for_costs.get("sessions_7d", 0)
        sessions_month = max(int(sessions_7d * days_elapsed / 7), 1) if sessions_7d else max(days_elapsed, 1)

    # ── KPI ──
    st.markdown("### 今月の推定コスト")
    k1, k2, k3, k4 = st.columns(4)
    with k1:
        st.metric("推定合計", f"¥{cost_total * JPY_RATE:.0f}")
    with k2:
        st.metric("STT 使用量", f"{total_stt_min:.1f} 分")
    with k3:
        st.metric("LLM 呼び出し", f"{total_llm_calls} 回")
    with k4:
        daily_cost = cost_total / days_elapsed if days_elapsed > 0 else 0
        monthly_est = daily_cost * 30
        st.metric("月末予測", f"¥{monthly_est * JPY_RATE:.0f}")

    st.divider()

    # ── サービス別コスト内訳 ──
    st.markdown("### サービス別コスト内訳")

    cost_rows = []
    services = [
        ("Cloud Run", cost_cloud_run, f"STT×{CR_TO_SPEECH_RATIO:.1f}倍 (GCP実績比率)", "CPU+Memory+Requests"),
        ("Cloud Speech API", cost_speech, f"{total_stt_min:.1f} 分 × $0.064", "Chirp 2"),
        ("Vertex AI", cost_vertex, f"{total_llm_calls} 回 × $0.001", "Gemini 2.0 Flash Lite"),
        ("その他 (AR/Storage/etc)", cost_fixed, f"{days_elapsed}日 × $0.25", "Artifact Registry + Storage"),
    ]
    for name, cost_usd, usage_str, note in services:
        share = (cost_usd / cost_total * 100) if cost_total > 0 else 0
        cost_rows.append({
            "サービス": name,
            "使用量/計算根拠": usage_str,
            "コスト (USD)": f"${cost_usd:.2f}",
            "コスト (JPY)": f"¥{cost_usd * JPY_RATE:.0f}",
            "構成比": f"{share:.0f}%",
            "備考": note,
        })
    cost_rows.append({
        "サービス": "合計",
        "使用量/計算根拠": "",
        "コスト (USD)": f"${cost_total:.2f}",
        "コスト (JPY)": f"¥{cost_total * JPY_RATE:.0f}",
        "構成比": "100%",
        "備考": f"{days_elapsed}日間 (月初〜今日)",
    })

    cost_df = pd.DataFrame(cost_rows)
    st.dataframe(cost_df, use_container_width=True, hide_index=True)

    # ── 単位原価 ──
    st.divider()
    st.markdown("### 単位原価")

    u1, u2, u3 = st.columns(3)
    with u1:
        speech_per_min = cost_speech / total_stt_min if total_stt_min > 0 else 0
        total_per_min = cost_total / total_stt_min if total_stt_min > 0 else 0
        st.metric("STT 1分あたり (Speech のみ)", f"¥{speech_per_min * JPY_RATE:.1f}")
        st.caption(f"全サービス込み: ¥{total_per_min * JPY_RATE:.1f}/分")
    with u2:
        cost_per_session = cost_total / sessions_month if sessions_month > 0 else 0
        st.metric("1セッションあたり", f"¥{cost_per_session * JPY_RATE:.1f}")
        st.caption(f"推定セッション数: {sessions_month}")
    with u3:
        cost_per_day = cost_total / days_elapsed if days_elapsed > 0 else 0
        st.metric("1日あたり", f"¥{cost_per_day * JPY_RATE:.0f}")
        st.caption(f"${cost_per_day:.2f}/day")

    # ── コスト構成比チャート ──
    st.divider()
    st.markdown("### コスト構成比")
    pie_data = pd.DataFrame([
        {"サービス": name, "コスト": cost_usd}
        for name, cost_usd, _, _ in services if cost_usd > 0
    ])
    if not pie_data.empty:
        fig = px.pie(pie_data, values="コスト", names="サービス", hole=0.4)
        fig.update_layout(margin=dict(l=20, r=20, t=30, b=20), height=300)
        st.plotly_chart(fig, use_container_width=True)

    # ── エンティティ別使用量 ──
    st.divider()
    if usage_by_entity:
        st.markdown("### エンティティ別使用量")
        usage_df = pd.DataFrame(usage_by_entity)
        usage_df = usage_df.sort_values("STT (min)", ascending=False)
        st.dataframe(usage_df, use_container_width=True, hide_index=True)

    # ── Cloud Run 設定ステータス ──
    st.divider()
    st.markdown("### Cloud Run コスト最適化ステータス")
    opt1, opt2, opt3, opt4 = st.columns(4)
    with opt1:
        st.markdown("**cpu-throttling**")
        st.success("ON ✅")
    with opt2:
        st.markdown("**min-instances**")
        st.success("0 ✅")
    with opt3:
        st.markdown("**concurrency**")
        st.info("15")
    with opt4:
        st.markdown("**CPU / Memory**")
        st.info("1 vCPU / 2 GiB")

    st.markdown("""
    > **計算方法**: Cloud Run コストは GCP 請求実績の比率（Speech の約2.2倍）から推定。
    > 正確な値は [GCP Billing Console](https://console.cloud.google.com/billing) で確認。
    > Cloud Run の SKU 別内訳（CPU/Memory/Requests）は「グループ条件: SKU」で確認可能。
    """)

# ---------------------------------------------------------
# TAB: Performance Monitoring
# ---------------------------------------------------------
with tab_perf:
    st.markdown("## 🚀 Performance Monitoring")
    st.caption("API レイテンシ / Cloud Run メトリクス / Firestore 読み取り")

    # Time range selector for performance data
    perf_range = st.selectbox("時間範囲", ["1時間", "6時間", "24時間", "7日"], index=1, key="perf_range")
    perf_hours = {"1時間": 1, "6時間": 6, "24時間": 24, "7日": 168}[perf_range]

    @st.cache_data(ttl=120)
    def fetch_cloud_run_metrics(_project_id: str, hours: int):
        """Fetch Cloud Run metrics from Cloud Monitoring API."""
        try:
            from google.cloud import monitoring_v3
            from google.protobuf.timestamp_pb2 import Timestamp
            import time

            client = monitoring_v3.MetricServiceClient()
            project_name = f"projects/{_project_id}"
            now = time.time()
            start_time = now - (hours * 3600)

            interval = monitoring_v3.TimeInterval({
                "start_time": {"seconds": int(start_time)},
                "end_time": {"seconds": int(now)},
            })

            metrics = {}

            # 1. Request Latency (p50, p95, p99)
            for percentile in [50, 95, 99]:
                try:
                    request = monitoring_v3.ListTimeSeriesRequest(
                        name=project_name,
                        filter=f'metric.type="run.googleapis.com/request_latencies" AND resource.labels.service_name="deepnote-api"',
                        interval=interval,
                        view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                        aggregation=monitoring_v3.Aggregation({
                            "alignment_period": {"seconds": 300},  # 5 min
                            "per_series_aligner": monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_99 if percentile == 99 else
                                                  monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_95 if percentile == 95 else
                                                  monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_50,
                        }),
                    )
                    results = list(client.list_time_series(request=request))
                    if results:
                        points = []
                        for ts in results:
                            for point in ts.points:
                                points.append({
                                    "time": datetime.fromtimestamp(point.interval.end_time.seconds, tz=timezone.utc),
                                    f"p{percentile}": point.value.double_value / 1000  # ms to seconds
                                })
                        metrics[f"latency_p{percentile}"] = points
                except Exception as e:
                    st.warning(f"Failed to fetch p{percentile} latency: {e}")

            # 2. Request Count by path
            try:
                request = monitoring_v3.ListTimeSeriesRequest(
                    name=project_name,
                    filter=f'metric.type="run.googleapis.com/request_count" AND resource.labels.service_name="deepnote-api"',
                    interval=interval,
                    view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                    aggregation=monitoring_v3.Aggregation({
                        "alignment_period": {"seconds": 300},
                        "per_series_aligner": monitoring_v3.Aggregation.Aligner.ALIGN_RATE,
                    }),
                )
                results = list(client.list_time_series(request=request))
                request_data = []
                for ts in results:
                    response_code = ts.metric.labels.get("response_code", "unknown")
                    for point in ts.points:
                        request_data.append({
                            "time": datetime.fromtimestamp(point.interval.end_time.seconds, tz=timezone.utc),
                            "response_code": response_code,
                            "rate": point.value.double_value,
                        })
                metrics["request_count"] = request_data
            except Exception as e:
                st.warning(f"Failed to fetch request count: {e}")

            # 3. CPU Utilization
            try:
                request = monitoring_v3.ListTimeSeriesRequest(
                    name=project_name,
                    filter=f'metric.type="run.googleapis.com/container/cpu/utilizations" AND resource.labels.service_name="deepnote-api"',
                    interval=interval,
                    view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                    aggregation=monitoring_v3.Aggregation({
                        "alignment_period": {"seconds": 300},
                        "per_series_aligner": monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_99,
                        "cross_series_reducer": monitoring_v3.Aggregation.Reducer.REDUCE_MEAN,
                    }),
                )
                results = list(client.list_time_series(request=request))
                cpu_data = []
                for ts in results:
                    for point in ts.points:
                        cpu_data.append({
                            "time": datetime.fromtimestamp(point.interval.end_time.seconds, tz=timezone.utc),
                            "cpu_pct": point.value.double_value * 100,
                        })
                metrics["cpu"] = cpu_data
            except Exception as e:
                st.warning(f"Failed to fetch CPU: {e}")

            # 4. Memory Utilization
            try:
                request = monitoring_v3.ListTimeSeriesRequest(
                    name=project_name,
                    filter=f'metric.type="run.googleapis.com/container/memory/utilizations" AND resource.labels.service_name="deepnote-api"',
                    interval=interval,
                    view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                    aggregation=monitoring_v3.Aggregation({
                        "alignment_period": {"seconds": 300},
                        "per_series_aligner": monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_99,
                        "cross_series_reducer": monitoring_v3.Aggregation.Reducer.REDUCE_MEAN,
                    }),
                )
                results = list(client.list_time_series(request=request))
                mem_data = []
                for ts in results:
                    for point in ts.points:
                        mem_data.append({
                            "time": datetime.fromtimestamp(point.interval.end_time.seconds, tz=timezone.utc),
                            "memory_pct": point.value.double_value * 100,
                        })
                metrics["memory"] = mem_data
            except Exception as e:
                st.warning(f"Failed to fetch memory: {e}")

            # 5. Instance Count
            try:
                request = monitoring_v3.ListTimeSeriesRequest(
                    name=project_name,
                    filter=f'metric.type="run.googleapis.com/container/instance_count" AND resource.labels.service_name="deepnote-api"',
                    interval=interval,
                    view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                    aggregation=monitoring_v3.Aggregation({
                        "alignment_period": {"seconds": 300},
                        "per_series_aligner": monitoring_v3.Aggregation.Aligner.ALIGN_MAX,
                    }),
                )
                results = list(client.list_time_series(request=request))
                instance_data = []
                for ts in results:
                    for point in ts.points:
                        instance_data.append({
                            "time": datetime.fromtimestamp(point.interval.end_time.seconds, tz=timezone.utc),
                            "instances": point.value.int64_value,
                        })
                metrics["instances"] = instance_data
            except Exception as e:
                st.warning(f"Failed to fetch instance count: {e}")

            # 6. Concurrent Requests
            try:
                request = monitoring_v3.ListTimeSeriesRequest(
                    name=project_name,
                    filter=f'metric.type="run.googleapis.com/container/max_request_concurrencies" AND resource.labels.service_name="deepnote-api"',
                    interval=interval,
                    view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                    aggregation=monitoring_v3.Aggregation({
                        "alignment_period": {"seconds": 300},
                        "per_series_aligner": monitoring_v3.Aggregation.Aligner.ALIGN_MAX,
                        "cross_series_reducer": monitoring_v3.Aggregation.Reducer.REDUCE_SUM,
                    }),
                )
                results = list(client.list_time_series(request=request))
                concurrency_data = []
                for ts in results:
                    for point in ts.points:
                        concurrency_data.append({
                            "time": datetime.fromtimestamp(point.interval.end_time.seconds, tz=timezone.utc),
                            "concurrency": point.value.int64_value,
                        })
                metrics["concurrency"] = concurrency_data
            except Exception as e:
                st.warning(f"Failed to fetch concurrency: {e}")

            return metrics

        except ImportError:
            st.error("google-cloud-monitoring not installed. Run: pip install google-cloud-monitoring")
            return {}
        except Exception as e:
            st.error(f"Failed to fetch Cloud Run metrics: {e}")
            return {}

    @st.cache_data(ttl=120)
    def fetch_firestore_metrics(_project_id: str, hours: int):
        """Fetch Firestore read/write metrics."""
        try:
            from google.cloud import monitoring_v3
            import time

            client = monitoring_v3.MetricServiceClient()
            project_name = f"projects/{_project_id}"
            now = time.time()
            start_time = now - (hours * 3600)

            interval = monitoring_v3.TimeInterval({
                "start_time": {"seconds": int(start_time)},
                "end_time": {"seconds": int(now)},
            })

            metrics = {}

            # Document reads
            try:
                request = monitoring_v3.ListTimeSeriesRequest(
                    name=project_name,
                    filter='metric.type="firestore.googleapis.com/document/read_count"',
                    interval=interval,
                    view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                    aggregation=monitoring_v3.Aggregation({
                        "alignment_period": {"seconds": 300},
                        "per_series_aligner": monitoring_v3.Aggregation.Aligner.ALIGN_RATE,
                    }),
                )
                results = list(client.list_time_series(request=request))
                read_data = []
                for ts in results:
                    for point in ts.points:
                        read_data.append({
                            "time": datetime.fromtimestamp(point.interval.end_time.seconds, tz=timezone.utc),
                            "reads_per_sec": point.value.double_value,
                        })
                metrics["reads"] = read_data
            except Exception as e:
                st.warning(f"Failed to fetch Firestore reads: {e}")

            # Document writes
            try:
                request = monitoring_v3.ListTimeSeriesRequest(
                    name=project_name,
                    filter='metric.type="firestore.googleapis.com/document/write_count"',
                    interval=interval,
                    view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                    aggregation=monitoring_v3.Aggregation({
                        "alignment_period": {"seconds": 300},
                        "per_series_aligner": monitoring_v3.Aggregation.Aligner.ALIGN_RATE,
                    }),
                )
                results = list(client.list_time_series(request=request))
                write_data = []
                for ts in results:
                    for point in ts.points:
                        write_data.append({
                            "time": datetime.fromtimestamp(point.interval.end_time.seconds, tz=timezone.utc),
                            "writes_per_sec": point.value.double_value,
                        })
                metrics["writes"] = write_data
            except Exception as e:
                st.warning(f"Failed to fetch Firestore writes: {e}")

            return metrics

        except ImportError:
            st.error("google-cloud-monitoring not installed")
            return {}
        except Exception as e:
            st.error(f"Failed to fetch Firestore metrics: {e}")
            return {}

    @st.cache_data(ttl=120)
    def fetch_endpoint_latencies(_project_id: str, hours: int):
        """Fetch endpoint-specific latencies from Cloud Logging."""
        try:
            import subprocess
            import json

            endpoints = ["/users/me", "/sessions", "/app-config"]
            results = {}

            for endpoint in endpoints:
                cmd = f'''gcloud logging read 'resource.labels.service_name="deepnote-api" AND httpRequest.requestUrl=~"{endpoint}"' --project={_project_id} --limit=100 --freshness={hours}h --format="json(timestamp,httpRequest.latency,httpRequest.status)"'''
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)

                if result.returncode == 0 and result.stdout.strip():
                    try:
                        logs = json.loads(result.stdout)
                        latencies = []
                        for log in logs:
                            if "httpRequest" in log and "latency" in log["httpRequest"]:
                                latency_str = log["httpRequest"]["latency"]
                                # Parse latency string like "0.123456789s"
                                if latency_str.endswith("s"):
                                    latency_sec = float(latency_str[:-1])
                                    latencies.append(latency_sec)

                        if latencies:
                            latencies.sort()
                            p50_idx = int(len(latencies) * 0.5)
                            p95_idx = int(len(latencies) * 0.95)
                            p99_idx = int(len(latencies) * 0.99)
                            results[endpoint] = {
                                "count": len(latencies),
                                "p50": latencies[p50_idx] if p50_idx < len(latencies) else latencies[-1],
                                "p95": latencies[p95_idx] if p95_idx < len(latencies) else latencies[-1],
                                "p99": latencies[p99_idx] if p99_idx < len(latencies) else latencies[-1],
                                "max": max(latencies),
                            }
                    except json.JSONDecodeError:
                        pass

            return results
        except Exception as e:
            st.warning(f"Failed to fetch endpoint latencies: {e}")
            return {}

    # Fetch all metrics
    with st.spinner("Loading performance metrics..."):
        cloud_run_metrics = fetch_cloud_run_metrics(selected_project, perf_hours)
        firestore_metrics = fetch_firestore_metrics(selected_project, perf_hours)
        endpoint_latencies = fetch_endpoint_latencies(selected_project, perf_hours)

    # ---------------------------------------------------------
    # Section 1: Endpoint Latencies (p95)
    # ---------------------------------------------------------
    st.markdown("### 📊 Endpoint Latencies (p95)")
    st.caption("天井判定: p95 > 1秒 で注意")

    if endpoint_latencies:
        cols = st.columns(len(endpoint_latencies))
        for i, (endpoint, stats) in enumerate(endpoint_latencies.items()):
            with cols[i]:
                p95_ms = stats["p95"] * 1000
                status_color = COLOR_GREEN if p95_ms < 500 else COLOR_YELLOW if p95_ms < 1000 else COLOR_RED
                st.metric(
                    label=endpoint,
                    value=f"{p95_ms:.0f}ms",
                    delta=f"p99: {stats['p99']*1000:.0f}ms",
                )
                st.caption(f"p50: {stats['p50']*1000:.0f}ms | max: {stats['max']*1000:.0f}ms | n={stats['count']}")
    else:
        st.info("エンドポイントレイテンシデータがありません")

    st.divider()

    # ---------------------------------------------------------
    # Section 2: Cloud Run Metrics
    # ---------------------------------------------------------
    st.markdown("### 🖥️ Cloud Run Metrics")
    st.caption("天井判定: CPU > 80% で注意")

    col1, col2, col3, col4 = st.columns(4)

    # Current CPU
    with col1:
        if cloud_run_metrics.get("cpu"):
            latest_cpu = cloud_run_metrics["cpu"][-1]["cpu_pct"] if cloud_run_metrics["cpu"] else 0
            st.metric("CPU (p99)", f"{latest_cpu:.1f}%")
        else:
            st.metric("CPU (p99)", "N/A")

    # Current Memory
    with col2:
        if cloud_run_metrics.get("memory"):
            latest_mem = cloud_run_metrics["memory"][-1]["memory_pct"] if cloud_run_metrics["memory"] else 0
            st.metric("Memory (p99)", f"{latest_mem:.1f}%")
        else:
            st.metric("Memory (p99)", "N/A")

    # Instances
    with col3:
        if cloud_run_metrics.get("instances"):
            latest_instances = cloud_run_metrics["instances"][-1]["instances"] if cloud_run_metrics["instances"] else 0
            st.metric("Instances", latest_instances)
        else:
            st.metric("Instances", "N/A")

    # Concurrency
    with col4:
        if cloud_run_metrics.get("concurrency"):
            latest_concurrency = cloud_run_metrics["concurrency"][-1]["concurrency"] if cloud_run_metrics["concurrency"] else 0
            st.metric("Concurrency", latest_concurrency)
        else:
            st.metric("Concurrency", "N/A")

    # CPU & Memory Chart
    cpu_mem_data = []
    if cloud_run_metrics.get("cpu"):
        for point in cloud_run_metrics["cpu"]:
            cpu_mem_data.append({"time": point["time"], "metric": "CPU %", "value": point["cpu_pct"]})
    if cloud_run_metrics.get("memory"):
        for point in cloud_run_metrics["memory"]:
            cpu_mem_data.append({"time": point["time"], "metric": "Memory %", "value": point["memory_pct"]})

    if cpu_mem_data:
        df_cpu_mem = pd.DataFrame(cpu_mem_data)
        fig = px.line(df_cpu_mem, x="time", y="value", color="metric", title="CPU / Memory Utilization")
        fig.add_hline(y=80, line_dash="dash", line_color="red", annotation_text="Warning: 80%")
        fig.update_layout(height=300, yaxis_range=[0, 100])
        st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # ---------------------------------------------------------
    # Section 3: Firestore Metrics
    # ---------------------------------------------------------
    st.markdown("### 🗄️ Firestore Reads/Writes")
    st.caption("天井判定: reads/sec が CPU と比例して増加し続けたら天井")

    col1, col2 = st.columns(2)

    with col1:
        if firestore_metrics.get("reads"):
            latest_reads = firestore_metrics["reads"][-1]["reads_per_sec"] if firestore_metrics["reads"] else 0
            st.metric("Reads/sec", f"{latest_reads:.1f}")
        else:
            st.metric("Reads/sec", "N/A")

    with col2:
        if firestore_metrics.get("writes"):
            latest_writes = firestore_metrics["writes"][-1]["writes_per_sec"] if firestore_metrics["writes"] else 0
            st.metric("Writes/sec", f"{latest_writes:.1f}")
        else:
            st.metric("Writes/sec", "N/A")

    # Firestore Chart
    fs_data = []
    if firestore_metrics.get("reads"):
        for point in firestore_metrics["reads"]:
            fs_data.append({"time": point["time"], "metric": "Reads/sec", "value": point["reads_per_sec"]})
    if firestore_metrics.get("writes"):
        for point in firestore_metrics["writes"]:
            fs_data.append({"time": point["time"], "metric": "Writes/sec", "value": point["writes_per_sec"]})

    if fs_data:
        df_fs = pd.DataFrame(fs_data)
        fig = px.line(df_fs, x="time", y="value", color="metric", title="Firestore Operations")
        fig.update_layout(height=300)
        st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # ---------------------------------------------------------
    # Section 4: Capacity Analysis
    # ---------------------------------------------------------
    st.markdown("### 🎯 Capacity Analysis")

    # Calculate capacity indicators
    capacity_issues = []
    if endpoint_latencies:
        for endpoint, stats in endpoint_latencies.items():
            if stats["p95"] > 1.0:
                capacity_issues.append(f"⚠️ {endpoint} p95 > 1秒 ({stats['p95']*1000:.0f}ms)")

    if cloud_run_metrics.get("cpu"):
        latest_cpu = cloud_run_metrics["cpu"][-1]["cpu_pct"] if cloud_run_metrics["cpu"] else 0
        if latest_cpu > 80:
            capacity_issues.append(f"⚠️ CPU使用率 > 80% ({latest_cpu:.1f}%)")

    if cloud_run_metrics.get("memory"):
        latest_mem = cloud_run_metrics["memory"][-1]["memory_pct"] if cloud_run_metrics["memory"] else 0
        if latest_mem > 80:
            capacity_issues.append(f"⚠️ メモリ使用率 > 80% ({latest_mem:.1f}%)")

    if capacity_issues:
        st.warning("**天井に近づいている可能性があります:**")
        for issue in capacity_issues:
            st.markdown(issue)
    else:
        st.success("✅ 現在のキャパシティは十分です")

    # Recommendations
    st.markdown("#### 推奨アクション")
    st.markdown("""
    | 状態 | 判定基準 | 対策 |
    |------|----------|------|
    | 🟢 正常 | p95 < 500ms, CPU < 50% | 現状維持 |
    | 🟡 注意 | p95 500ms-1s, CPU 50-80% | モニタリング強化 |
    | 🔴 危険 | p95 > 1s, CPU > 80% | スケールアップ / 最適化 |
    """)

    # GCP Console Links
    st.markdown("#### GCP Console Links")
    st.markdown(f"""
    - [Cloud Run Metrics](https://console.cloud.google.com/run/detail/asia-northeast1/deepnote-api/metrics?project={selected_project})
    - [Cloud Logging](https://console.cloud.google.com/logs/query?project={selected_project})
    - [Firestore Usage](https://console.cloud.google.com/firestore/usage?project={selected_project})
    """)

# ---------------------------------------------------------
# TAB: Config
# ---------------------------------------------------------
with tab_config:
    st.markdown("## 🔧 Configuration")

    pricing = fetch_pricing_config(selected_project)

    if pricing:
        st.markdown("### Pricing Config")

        with st.form("pricing_form"):
            new_pricing = {}
            for key, value in pricing.items():
                new_pricing[key] = st.number_input(key, value=float(value), format="%.4f")

            submitted = st.form_submit_button("Save Pricing Config")
            if submitted:
                try:
                    db = _init_firebase(selected_project)
                    db.collection("pricing_config").document("current").set(new_pricing)
                    st.success("Pricing config saved!")
                    st.cache_data.clear()
                except Exception as e:
                    st.error(f"Failed to save: {e}")

    st.divider()

    # Plan Limits Config
    st.markdown("### Plan Limits")
    st.code("""
FREE_SERVER_SESSION_LIMIT = 5  # Max sessions saved on server
FREE_CLOUD_STT = 0             # Free cannot use cloud STT
BASIC_CLOUD_STT = 7200         # 120 minutes/month
BASIC_SESSIONS = 300           # Max server sessions
    """)

    st.divider()

    # Danger Zone
    st.markdown("### ⚠️ Admin Actions")

    with st.expander("Reset User Limits"):
        reset_uid = st.text_input("User ID to reset")
        if st.button("Reset Limits", type="secondary"):
            if reset_uid:
                try:
                    db = _init_firebase(selected_project)
                    db.collection("users").document(reset_uid).update({
                        "serverSessionCount": 0,
                        "cloudEntitledSessionIds": []
                    })
                    st.success(f"Reset limits for {reset_uid}")
                except Exception as e:
                    st.error(f"Failed: {e}")

    with st.expander("Set User Plan"):
        plan_uid = st.text_input("User ID")
        new_plan = st.selectbox("New Plan", ["free", "basic"])
        if st.button("Update Plan", type="secondary"):
            if plan_uid:
                try:
                    db = _init_firebase(selected_project)
                    db.collection("users").document(plan_uid).update({
                        "plan": new_plan,
                        "planUpdatedAt": datetime.now(timezone.utc)
                    })
                    st.success(f"Updated {plan_uid} to {new_plan}")
                except Exception as e:
                    st.error(f"Failed: {e}")

# ---------------------------------------------------------
# TAB 12: YouTube Health Check
# ---------------------------------------------------------
with tab_youtube:
    st.markdown("## 📺 YouTube 字幕取得ヘルスチェック")

    @st.cache_data(ttl=120)
    def fetch_youtube_health(project_id):
        db = _init_firebase(project_id)
        doc = db.collection("config").document("youtube_health").get()
        if not doc.exists:
            return None, []
        data = doc.to_dict()

        # 履歴取得（最新30件）
        history = []
        try:
            hist_docs = db.collection("config").document("youtube_health").collection("history")\
                .order_by("checkedAt", direction="DESCENDING").limit(30).stream()
            for h in hist_docs:
                hd = h.to_dict()
                hd["date"] = h.id
                history.append(hd)
        except Exception:
            pass
        return data, history

    yt_data, yt_history = fetch_youtube_health(selected_project)

    if not yt_data:
        st.warning("ヘルスチェックデータがまだありません。初回チェックを実行してください。")
    else:
        last_check = yt_data.get("lastCheck", {})
        checked_at = last_check.get("checkedAt")
        proxy_result = last_check.get("proxy", {})
        direct_result = last_check.get("direct", {})

        # 最終チェック日時（JST）
        if checked_at:
            if hasattr(checked_at, 'timestamp'):
                jst = checked_at.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=9)))
            else:
                jst = checked_at
            st.markdown(f"**最終チェック:** {jst.strftime('%Y-%m-%d %H:%M JST')}")
        else:
            st.markdown("**最終チェック:** 不明")

        # ステータスカード
        c1, c2 = st.columns(2)
        with c1:
            proxy_status = proxy_result.get("status", "unknown")
            if proxy_status == "ok":
                st.success(f"🔒 プロキシ経由: **OK** ({proxy_result.get('segments', '?')} segments, {proxy_result.get('textLength', '?')} chars)")
            else:
                st.error(f"🔒 プロキシ経由: **FAILED**")
                st.code(proxy_result.get("error", "Unknown error")[:300])
        with c2:
            direct_status = direct_result.get("status", "unknown")
            if direct_status == "ok":
                st.success(f"🌐 直接接続: **OK** ({direct_result.get('segments', '?')} segments)")
            else:
                st.warning(f"🌐 直接接続: **FAILED** (Cloud Run DC IPはブロック対象)")

    # 履歴テーブル
    if yt_history:
        st.markdown("### 📅 チェック履歴")
        hist_rows = []
        for h in yt_history:
            checked = h.get("checkedAt")
            if hasattr(checked, 'timestamp'):
                checked_jst = checked.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")
            elif checked:
                checked_jst = str(checked)[:16]
            else:
                checked_jst = h.get("date", "?")

            proxy = h.get("proxy", {})
            direct = h.get("direct", {})
            hist_rows.append({
                "日時": checked_jst,
                "プロキシ": "✅ OK" if proxy.get("status") == "ok" else "❌ FAIL",
                "直接": "✅ OK" if direct.get("status") == "ok" else "⚠️ FAIL",
                "Segments": proxy.get("segments", "-"),
                "文字数": proxy.get("textLength", "-"),
                "動画ID": proxy.get("videoId", "-"),
            })
        if hist_rows:
            st.dataframe(pd.DataFrame(hist_rows), use_container_width=True, hide_index=True)

    # 手動チェック実行ボタン
    st.markdown("---")
    st.markdown("### 🔄 手動チェック")
    if st.button("今すぐヘルスチェックを実行", type="primary"):
        import requests as http_requests
        try:
            cloud_run_url = os.environ.get("CLOUD_RUN_SERVICE_URL", "https://deepnote-api-900324644592.asia-northeast1.run.app")
            resp = http_requests.post(f"{cloud_run_url}/internal/tasks/youtube_health_check", json={}, timeout=120)
            if resp.status_code == 200:
                result = resp.json()
                st.success(f"チェック完了: Proxy={result.get('proxy', {}).get('status')}, Direct={result.get('direct', {}).get('status')}")
                st.cache_data.clear()
                st.rerun()
            else:
                st.error(f"チェック失敗: HTTP {resp.status_code}")
        except Exception as e:
            st.error(f"リクエスト失敗: {e}")
