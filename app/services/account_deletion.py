from datetime import datetime, timedelta, timezone

DELETION_HOLD_DAYS = 30
REQUESTS_COLLECTION = "account_deletion_requests"
LOCKS_COLLECTION = "account_deletion_locks"


def deletion_lock_id(email_lower: str, provider_id: str) -> str:
    safe_email = (email_lower or "").replace("/", "_")
    safe_provider = (provider_id or "").replace("/", "_")
    return f"{safe_provider}:{safe_email}"


def deletion_schedule_at(now: datetime | None = None) -> datetime:
    base = now or datetime.now(timezone.utc)
    return base + timedelta(days=DELETION_HOLD_DAYS)
