import logging
import os
from datetime import datetime, timedelta, date, timezone
from google.cloud import firestore
from app.firebase import db

logger = logging.getLogger("app.jobs.aggregate_daily_usage")

def aggregate_daily_usage(target_date_str: str = None) -> dict:
    """
    Aggregates usage data from all users for a specific date into a global system statistic.
    Result is written to `system_stats/daily_{date}`.
    
    Args:
        target_date_str: "YYYY-MM-DD". If None, defaults to yesterday (UTC).
    """
    if not target_date_str:
        # Default to yesterday
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        target_date_str = yesterday.date().isoformat()

    logger.info(f"Starting daily usage aggregation for {target_date_str}")
    
    # Query all user_daily_usage docs for this date
    # Note: Requires an index on 'date' if collection is large.
    # user_daily_usage has 'date' field.
    query = db.collection("user_daily_usage").where("date", "==", target_date_str)
    docs = list(query.stream())
    
    if not docs:
        logger.info(f"No usage data found for {target_date_str}")
        return {"status": "skipped", "reason": "no_data", "date": target_date_str}

    # Initialize Global Aggregates
    global_stats = {
        "date": target_date_str,
        "aggregated_at": datetime.now(timezone.utc),
        "total_active_users": 0,
        "total_recording_sec": 0.0,
        "total_sessions": 0,
        "total_summary_invocations": 0,
        "total_summary_success": 0,
        "total_quiz_invocations": 0,
        "total_quiz_success": 0,
        "total_llm_input_tokens": 0,
        "total_llm_output_tokens": 0,
        "usage_by_mode": {},
    }
    
    for doc in docs:
        data = doc.to_dict()
        
        global_stats["total_active_users"] += 1
        global_stats["total_recording_sec"] += data.get("total_recording_sec", 0)
        global_stats["total_sessions"] += data.get("session_count", 0)
        
        global_stats["total_summary_invocations"] += data.get("summary_invocations", 0)
        global_stats["total_summary_success"] += data.get("summary_success", 0)
        
        global_stats["total_quiz_invocations"] += data.get("quiz_invocations", 0)
        global_stats["total_quiz_success"] += data.get("quiz_success", 0)
        
        global_stats["total_llm_input_tokens"] += data.get("llm_input_tokens", 0)
        global_stats["total_llm_output_tokens"] += data.get("llm_output_tokens", 0)
        
        # Mode
        u_mode = data.get("usage_by_mode", {})
        for m, sec in u_mode.items():
            global_stats["usage_by_mode"][m] = global_stats["usage_by_mode"].get(m, 0) + sec

    # Write to system_stats
    stats_ref = db.collection("system_stats").document(f"daily_{target_date_str}")
    stats_ref.set(global_stats)
    
    logger.info(f"Aggregation complete for {target_date_str}: {global_stats}")
    return {"status": "completed", "date": target_date_str, "stats": global_stats}

if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    aggregate_daily_usage()
