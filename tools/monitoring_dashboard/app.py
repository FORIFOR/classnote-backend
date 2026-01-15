import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, timezone, timedelta
import requests
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
    .badge-7d {{ background: {COLOR_GREEN}; color: white; padding: 2px 8px; border-radius: 4px; font-size: 11px; }}
    .badge-30d {{ background: {COLOR_YELLOW}; color: white; padding: 2px 8px; border-radius: 4px; font-size: 11px; }}
    .badge-inactive {{ background: {COLOR_GRAY}; color: white; padding: 2px 8px; border-radius: 4px; font-size: 11px; }}
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
    
    api_base = st.text_input("API Base URL", value="http://localhost:8000", help="Backend API endpoint")
    
    st.divider()
    
    # Date range
    date_range = st.selectbox("Date Range", ["Today", "7 days", "30 days", "All Time"], index=1)
    
    st.divider()
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
        for doc in users_docs:
            data = doc.to_dict()
            last_seen = data.get("lastSeenAt")
            created_at = data.get("createdAt")
            
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
        
        for doc in sessions_docs:
            data = doc.to_dict()
            created_at = data.get("createdAt")
            duration = data.get("durationSec") or 0
            status = data.get("status", "")
            
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
            "jobs_failed_24h": jobs_failed_24h
        }
    except Exception as e:
        st.error(f"Overview fetch error: {e}")
        return None

@st.cache_data(ttl=30)
def fetch_users_data(project_id, limit=200):
    """Fetch users with session stats."""
    try:
        db = _init_firebase(project_id)
        from google.cloud import firestore as gcp_firestore
        
        now = datetime.now(timezone.utc)
        month_ago = now - timedelta(days=30)
        
        # Users
        users_docs = list(db.collection("users").limit(limit).stream())
        
        # Sessions for stats
        sessions_docs = list(db.collection("sessions").order_by("createdAt", direction=gcp_firestore.Query.DESCENDING).limit(3000).stream())
        
        # Aggregate sessions by user
        user_stats = {}
        for doc in sessions_docs:
            data = doc.to_dict()
            uid = data.get("userId") or data.get("ownerUserId")
            created_at = data.get("createdAt")
            duration = data.get("durationSec") or 0
            
            if not uid: continue
            if uid not in user_stats:
                user_stats[uid] = {"sessions_30d": 0, "recording_sec_30d": 0.0, "recording_sec_lifetime": 0.0}
            
            user_stats[uid]["recording_sec_lifetime"] += duration
            
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
            stats = user_stats.get(uid, {"sessions_30d": 0, "recording_sec_30d": 0.0, "recording_sec_lifetime": 0.0})
            
            last_seen = data.get("lastSeenAt")
            active_badge = "inactive"
            if last_seen and hasattr(last_seen, 'replace'):
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)
                delta = now - last_seen
                if delta < timedelta(days=7): active_badge = "7d"
                elif delta < timedelta(days=30): active_badge = "30d"
            
            users.append({
                "uid": uid,
                "displayName": data.get("displayName") or data.get("email") or uid[:8],
                "email": data.get("email") or "-",
                "plan": data.get("plan", "free"),
                "providers": ", ".join(data.get("providers", [])) or "-",
                "createdAt": data.get("createdAt"),
                "lastSeenAt": last_seen,
                "sessions_30d": stats["sessions_30d"],
                "recording_min_30d": round(stats["recording_sec_30d"] / 60, 1),
                "recording_min_lifetime": round(stats["recording_sec_lifetime"] / 60, 1),
                "active_badge": active_badge
            })
        
        return users
    except Exception as e:
        st.error(f"Users fetch error: {e}")
        return []

@st.cache_data(ttl=30)
def fetch_sessions_data(project_id, limit=200):
    """Fetch sessions."""
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
                "userId": data.get("userId") or data.get("ownerUserId") or "-",
                "status": data.get("status") or "unknown",
                "mode": data.get("mode") or "-",
                "createdAt": data.get("createdAt"),
                "durationSec": data.get("durationSec") or 0,
                "audioStatus": data.get("audioStatus") or "unknown",
                "summaryStatus": data.get("summaryStatus") or "-"
            })
        return sessions
    except Exception as e:
        st.error(f"Sessions fetch error: {e}")
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

# ---------------------------------------------------------
# 5. Main Tabs
# ---------------------------------------------------------
tab_overview, tab_users, tab_sessions, tab_costs, tab_config = st.tabs([
    "📊 Overview", "👥 Users", "📝 Sessions", "💰 Costs", "⚙️ Config"
])

# ---------------------------------------------------------
# TAB 1: Overview
# ---------------------------------------------------------
with tab_overview:
    st.markdown("## 📊 Overview")
    
    data = fetch_overview_data(selected_project)
    
    if data:
        # KPI Cards
        k1, k2, k3, k4, k5, k6 = st.columns(6)
        with k1: st.metric("Total Users", data["total_users"])
        with k2: st.metric("DAU / WAU / MAU", f"{data['dau']} / {data['wau']} / {data['mau']}")
        with k3: st.metric("New Users (7d)", data["new_users_7d"])
        with k4: st.metric("Sessions (7d)", data["sessions_7d"])
        with k5: st.metric("Recording (7d)", f"{data['recording_min_7d']} min")
        with k6: st.metric("Est. Cost (7d)", f"${data['est_cost_7d']}")
        
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
    
    col_filter, col_search = st.columns([1, 2])
    with col_filter:
        filter_active = st.selectbox("Activity Filter", ["All", "7d", "30d", "inactive"], index=0)
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
        
        # Format for display
        df["Last Seen"] = df["lastSeenAt"].apply(lambda x: x.strftime("%Y-%m-%d %H:%M") if pd.notna(x) and hasattr(x, 'strftime') else "-")
        df["Created"] = df["createdAt"].apply(lambda x: x.strftime("%Y-%m-%d") if pd.notna(x) and hasattr(x, 'strftime') else "-")
        df["Active"] = df["active_badge"].apply(lambda x: "🟢" if x == "7d" else "🟡" if x == "30d" else "⚪")
        
        display_df = df[["Active", "displayName", "email", "plan", "Last Seen", "sessions_30d", "recording_min_30d", "recording_min_lifetime"]].rename(columns={
            "displayName": "Name",
            "sessions_30d": "Sessions (30d)",
            "recording_min_30d": "Rec. Min (30d)",
            "recording_min_lifetime": "Rec. Min (All)"
        })
        
        st.caption(f"Showing {len(display_df)} users")
        st.dataframe(display_df, use_container_width=True, hide_index=True, height=500)
    else:
        st.warning("No user data available.")

# ---------------------------------------------------------
# TAB 3: Sessions
# ---------------------------------------------------------
with tab_sessions:
    st.markdown("## 📝 Sessions")
    
    col_status, col_limit = st.columns([1, 1])
    with col_status:
        filter_status = st.selectbox("Status", ["All", "recording", "completed", "failed", "transcribing"])
    with col_limit:
        session_limit = st.slider("Limit", 50, 500, 200)
    
    sessions_data = fetch_sessions_data(selected_project, limit=session_limit)
    
    if sessions_data:
        df = pd.DataFrame(sessions_data)
        
        if filter_status != "All":
            df = df[df["status"] == filter_status]
        
        df["Time"] = df["createdAt"].apply(lambda x: x.strftime("%m/%d %H:%M") if pd.notna(x) and hasattr(x, 'strftime') else "-")
        df["Duration"] = (df["durationSec"] / 60).round(1).astype(str) + " min"
        df["Title"] = df["title"].apply(lambda x: str(x)[:30] if x else "-")
        
        display_df = df[["Time", "Title", "userId", "status", "mode", "Duration", "audioStatus", "summaryStatus"]].rename(columns={
            "userId": "User",
            "audioStatus": "Audio",
            "summaryStatus": "Summary"
        })
        
        st.caption(f"Showing {len(display_df)} sessions")
        st.dataframe(display_df, use_container_width=True, hide_index=True, height=500)
    else:
        st.warning("No session data available.")

# ---------------------------------------------------------
# TAB 4: Costs
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
            # Estimate monthly based on 7d average
            monthly_est = (overview['est_cost_7d'] / 7) * 30
            st.metric("Monthly (Est.)", f"${round(monthly_est, 2)}")
        
        st.divider()
        st.markdown("### Pricing Config")
        
        for key, value in pricing.items():
            st.text(f"{key}: {value}")
    else:
        st.warning("Failed to load cost data.")

# ---------------------------------------------------------
# TAB 5: Config
# ---------------------------------------------------------
with tab_config:
    st.markdown("## ⚙️ Configuration")
    
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
    else:
        st.warning("Failed to load config.")
