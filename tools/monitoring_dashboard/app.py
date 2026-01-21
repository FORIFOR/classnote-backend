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
    if plan in ("pro", "premium"):
        return "premium"
    if plan in ("basic", "standard"):
        return "basic"
    return "free"

def _format_timestamp(ts):
    """Format timestamp for display."""
    if ts is None:
        return "-"
    if hasattr(ts, 'strftime'):
        return ts.strftime("%Y-%m-%d %H:%M:%S")
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
@st.cache_data(ttl=30)
def fetch_overview_data(project_id):
    """Fetch overview KPIs directly from Firestore."""
    try:
        db = _init_firebase(project_id)
        now = datetime.now(timezone.utc)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_ago = today - timedelta(days=7)
        month_ago = today - timedelta(days=30)

        # Users
        users_docs = list(db.collection("users").limit(5000).stream())
        total_users = len(users_docs)

        dau = wau = mau = new_users_today = new_users_7d = 0
        plan_counts = {"free": 0, "basic": 0, "premium": 0}

        for doc in users_docs:
            data = doc.to_dict()
            last_seen = data.get("lastSeenAt")
            created_at = data.get("createdAt")
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

        # Pricing config
        pricing_doc = db.collection("pricing_config").document("current").get()
        speech_rate = 0.024
        if pricing_doc.exists:
            speech_rate = pricing_doc.to_dict().get("speech_per_min_usd", 0.024)

        est_cost_today = (recording_sec_today / 60) * speech_rate
        est_cost_7d = (recording_sec_7d / 60) * speech_rate

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

@st.cache_data(ttl=30)
def fetch_users_data(project_id, limit=200):
    """Fetch users with session stats and plan limits."""
    try:
        db = _init_firebase(project_id)
        from google.cloud import firestore as gcp_firestore

        now = datetime.now(timezone.utc)
        month_ago = now - timedelta(days=30)

        users_docs = list(db.collection("users").limit(limit).stream())
        sessions_docs = list(db.collection("sessions").order_by("createdAt", direction=gcp_firestore.Query.DESCENDING).limit(3000).stream())

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

            last_seen = data.get("lastSeenAt")
            active_badge = "inactive"
            if last_seen and hasattr(last_seen, 'replace'):
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)
                delta = now - last_seen
                if delta < timedelta(days=7): active_badge = "7d"
                elif delta < timedelta(days=30): active_badge = "30d"

            # Plan limits info
            server_session_count = data.get("serverSessionCount", stats["total_sessions"])
            cloud_entitled_ids = data.get("cloudEntitledSessionIds", [])

            users.append({
                "uid": uid,
                "displayName": data.get("displayName") or data.get("email") or uid[:8],
                "email": data.get("email") or "-",
                "plan": _normalize_plan(data.get("plan", "free")),
                "providers": ", ".join(data.get("providers", [])) or "-",
                "createdAt": data.get("createdAt"),
                "lastSeenAt": last_seen,
                "sessions_30d": stats["sessions_30d"],
                "recording_min_30d": round(stats["recording_sec_30d"] / 60, 1),
                "recording_min_lifetime": round(stats["recording_sec_lifetime"] / 60, 1),
                "active_badge": active_badge,
                "serverSessionCount": server_session_count,
                "cloudEntitledCount": len(cloud_entitled_ids),
                "isBlocked": data.get("isBlocked", False),
                "securityScore": data.get("securityScore", 100)
            })

        return users
    except Exception as e:
        st.error(f"Users fetch error: {e}")
        return []

@st.cache_data(ttl=30)
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
                "createdAt": data.get("createdAt"),
                "durationSec": data.get("durationSec") or 0,
                "audioStatus": data.get("audioStatus") or "-",
                "summaryStatus": data.get("summaryStatus") or "-",
                "quizStatus": data.get("quizStatus") or "-",
                "cloudEntitled": data.get("cloudEntitled", False),
                "hasTranscript": bool(data.get("transcriptText")),
                "deletedAt": data.get("deletedAt")
            })
        return sessions
    except Exception as e:
        st.error(f"Sessions fetch error: {e}")
        return []

@st.cache_data(ttl=30)
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
# 5. Main Tabs
# ---------------------------------------------------------
tab_overview, tab_users, tab_sessions, tab_jobs, tab_db, tab_costs, tab_config = st.tabs([
    "📊 Overview", "👥 Users", "📝 Sessions", "⚙️ Jobs", "🗄️ Database", "💰 Costs", "🔧 Config"
])

# ---------------------------------------------------------
# TAB 1: Overview
# ---------------------------------------------------------
with tab_overview:
    st.markdown("## 📊 Overview")

    data = fetch_overview_data(selected_project)

    if data:
        # KPI Cards Row 1
        k1, k2, k3, k4, k5, k6 = st.columns(6)
        with k1: st.metric("Total Users", data["total_users"])
        with k2: st.metric("DAU / WAU / MAU", f"{data['dau']} / {data['wau']} / {data['mau']}")
        with k3: st.metric("New Users (7d)", data["new_users_7d"])
        with k4: st.metric("Sessions (7d)", data["sessions_7d"])
        with k5: st.metric("Recording (7d)", f"{data['recording_min_7d']} min")
        with k6: st.metric("Est. Cost (7d)", f"${data['est_cost_7d']}")

        st.divider()

        # Plan Distribution & Session Types
        col1, col2 = st.columns(2)

        with col1:
            st.markdown("### Plan Distribution")
            plan_df = pd.DataFrame([
                {"Plan": "Free", "Count": data["plan_counts"].get("free", 0)},
                {"Plan": "Basic", "Count": data["plan_counts"].get("basic", 0)},
                {"Plan": "Premium", "Count": data["plan_counts"].get("premium", 0)},
            ])
            fig = px.pie(plan_df, values="Count", names="Plan", hole=0.4,
                        color_discrete_sequence=[COLOR_GRAY, COLOR_YELLOW, COLOR_GREEN])
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
        with t3: st.metric("Est. Cost Today", f"${data['est_cost_today']}")
        with t4: st.metric("⚠️ Failed (24h)", data["jobs_failed_24h"], delta_color="inverse")
    else:
        st.warning("Failed to load overview data.")

# ---------------------------------------------------------
# TAB 2: Users
# ---------------------------------------------------------
with tab_users:
    st.markdown("## 👥 Users")

    col_filter, col_plan, col_search = st.columns([1, 1, 2])
    with col_filter:
        filter_active = st.selectbox("Activity", ["All", "7d", "30d", "inactive"], index=0)
    with col_plan:
        filter_plan = st.selectbox("Plan", ["All", "free", "basic", "premium"], index=0)
    with col_search:
        search_query = st.text_input("🔍 Search", placeholder="Name, email, UID...")

    users_data = fetch_users_data(selected_project, limit=500)

    if users_data:
        df = pd.DataFrame(users_data)

        # Apply filters
        if search_query:
            mask = (
                df["displayName"].str.contains(search_query, case=False, na=False) |
                df["email"].str.contains(search_query, case=False, na=False) |
                df["uid"].str.contains(search_query, case=False, na=False)
            )
            df = df[mask]

        if filter_active != "All":
            df = df[df["active_badge"] == filter_active]

        if filter_plan != "All":
            df = df[df["plan"] == filter_plan]

        # Format for display
        df["Last Seen"] = df["lastSeenAt"].apply(lambda x: x.strftime("%Y-%m-%d %H:%M") if pd.notna(x) and hasattr(x, 'strftime') else "-")
        df["Created"] = df["createdAt"].apply(lambda x: x.strftime("%Y-%m-%d") if pd.notna(x) and hasattr(x, 'strftime') else "-")
        df["Active"] = df["active_badge"].apply(lambda x: "🟢" if x == "7d" else "🟡" if x == "30d" else "⚪")
        df["Plan"] = df["plan"].apply(lambda x: "💎" if x == "premium" else "⭐" if x == "basic" else "🆓")
        df["Limits"] = df.apply(lambda r: f"S:{r['serverSessionCount']}/5 C:{r['cloudEntitledCount']}/3" if r["plan"] == "free" else "∞", axis=1)

        display_df = df[["Active", "displayName", "email", "Plan", "Limits", "Last Seen", "sessions_30d", "recording_min_lifetime"]].rename(columns={
            "displayName": "Name",
            "sessions_30d": "Sessions (30d)",
            "recording_min_lifetime": "Total Min"
        })

        st.caption(f"Showing {len(display_df)} users")
        st.dataframe(display_df, use_container_width=True, hide_index=True, height=500)

        # User detail expander
        with st.expander("🔍 View User Details"):
            selected_uid = st.selectbox("Select User", df["uid"].tolist())
            if selected_uid:
                user_doc = fetch_document(selected_project, "users", selected_uid)
                if user_doc:
                    st.json(user_doc)
    else:
        st.warning("No user data available.")

# ---------------------------------------------------------
# TAB 3: Sessions
# ---------------------------------------------------------
with tab_sessions:
    st.markdown("## 📝 Sessions")

    col_status, col_mode, col_limit = st.columns([1, 1, 1])
    with col_status:
        filter_status = st.selectbox("Status", ["All", "録音中", "録音済み", "処理中", "要約済み", "failed"])
    with col_mode:
        filter_mode = st.selectbox("Mode", ["All", "cloud_google", "device_sherpa", "device_sfspeech"])
    with col_limit:
        session_limit = st.slider("Limit", 50, 500, 200)

    sessions_data = fetch_sessions_data(selected_project, limit=session_limit)

    if sessions_data:
        df = pd.DataFrame(sessions_data)

        # Filter out deleted
        df = df[df["deletedAt"].isna()]

        if filter_status != "All":
            df = df[df["status"] == filter_status]

        if filter_mode != "All":
            df = df[df["transcriptionMode"] == filter_mode]

        df["Time"] = df["createdAt"].apply(lambda x: x.strftime("%m/%d %H:%M") if pd.notna(x) and hasattr(x, 'strftime') else "-")
        df["Duration"] = (df["durationSec"] / 60).round(1).astype(str) + " min"
        df["Title"] = df["title"].apply(lambda x: str(x)[:30] if x else "-")
        df["Cloud"] = df["transcriptionMode"].apply(lambda x: "☁️" if x == "cloud_google" else "📱")
        df["Transcript"] = df["hasTranscript"].apply(lambda x: "✅" if x else "❌")
        df["Summary"] = df["summaryStatus"].apply(lambda x: "✅" if x == "completed" else "⏳" if x in ["running", "queued"] else "❌" if x == "failed" else "-")
        df["Quiz"] = df["quizStatus"].apply(lambda x: "✅" if x == "completed" else "⏳" if x in ["running", "queued"] else "❌" if x == "failed" else "-")

        display_df = df[["Time", "Cloud", "Title", "userId", "status", "Duration", "Transcript", "Summary", "Quiz"]].rename(columns={
            "userId": "User"
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
# TAB 4: Jobs
# ---------------------------------------------------------
with tab_jobs:
    st.markdown("## ⚙️ Jobs Monitor")

    col_type, col_status_job = st.columns(2)
    with col_type:
        job_type_filter = st.selectbox("Job Type", ["All", "summary", "quiz", "transcribe", "qa"])
    with col_status_job:
        job_status_filter = st.selectbox("Job Status", ["All", "queued", "running", "completed", "failed"])

    jobs_data = fetch_jobs_data(selected_project, limit=200)

    if jobs_data:
        df = pd.DataFrame(jobs_data)

        if job_type_filter != "All":
            df = df[df["type"] == job_type_filter]

        if job_status_filter != "All":
            df = df[df["status"] == job_status_filter]

        df["Time"] = df["createdAt"].apply(lambda x: x.strftime("%m/%d %H:%M") if pd.notna(x) and hasattr(x, 'strftime') else "-")
        df["Status Icon"] = df["status"].apply(lambda x: "✅" if x == "completed" else "⏳" if x in ["running", "queued"] else "❌" if x == "failed" else "❓")

        display_df = df[["Time", "Status Icon", "type", "status", "sessionId", "errorReason"]].rename(columns={
            "Status Icon": "✓",
            "type": "Type",
            "status": "Status",
            "sessionId": "Session",
            "errorReason": "Error"
        })

        # Summary stats
        s1, s2, s3, s4 = st.columns(4)
        with s1: st.metric("Total Jobs", len(df))
        with s2: st.metric("Completed", len(df[df["status"] == "completed"]))
        with s3: st.metric("Running", len(df[df["status"].isin(["running", "queued"])]))
        with s4: st.metric("Failed", len(df[df["status"] == "failed"]))

        st.divider()
        st.dataframe(display_df, use_container_width=True, hide_index=True, height=400)

        # Failed jobs details
        failed_jobs = df[df["status"] == "failed"]
        if not failed_jobs.empty:
            with st.expander(f"⚠️ Failed Jobs ({len(failed_jobs)})"):
                for _, job in failed_jobs.iterrows():
                    st.markdown(f"**{job['type']}** - Session: `{job['sessionId'][:12]}...`")
                    st.code(job["errorReason"])
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

    st.info("Cost estimation is based on recording duration × pricing config. For actual billing, enable BigQuery Billing Export.")

    overview = fetch_overview_data(selected_project)
    pricing = fetch_pricing_config(selected_project)

    if overview and pricing:
        st.markdown("### Estimated Costs (Based on Usage)")

        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("Today", f"${overview['est_cost_today']}")
        with c2:
            st.metric("7 Days", f"${overview['est_cost_7d']}")
        with c3:
            monthly_est = (overview['est_cost_7d'] / 7) * 30
            st.metric("Monthly (Est.)", f"${round(monthly_est, 2)}")

        st.divider()

        # Cost breakdown
        st.markdown("### Cost Breakdown (Estimated)")

        speech_cost = overview['recording_min_7d'] * pricing.get("speech_per_min_usd", 0.024)
        llm_cost = overview['sessions_7d'] * 0.05  # Rough estimate

        cost_df = pd.DataFrame([
            {"Category": "Speech-to-Text", "Cost (7d)": f"${round(speech_cost, 2)}"},
            {"Category": "LLM (Summary/Quiz)", "Cost (7d)": f"${round(llm_cost, 2)}"},
            {"Category": "Cloud Run (Shared)", "Cost (7d)": f"${round(pricing.get('cloudrun_shared_monthly_usd', 50) / 4, 2)}"},
        ])
        st.dataframe(cost_df, use_container_width=True, hide_index=True)

        st.divider()
        st.markdown("### Pricing Config")

        for key, value in pricing.items():
            st.text(f"{key}: {value}")
    else:
        st.warning("Failed to load cost data.")

# ---------------------------------------------------------
# TAB 7: Config
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
FREE_CLOUD_SESSION_LIMIT = 3   # Max cloud-enabled sessions
BASIC_MONTHLY_SESSION_LIMIT = 20
PREMIUM = Unlimited
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
        new_plan = st.selectbox("New Plan", ["free", "basic", "premium"])
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
