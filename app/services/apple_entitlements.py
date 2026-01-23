from datetime import datetime, timezone
from typing import Optional

def parse_ms_to_dt(ms: int | float | None) -> Optional[datetime]:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (ValueError, TypeError):
        return None

def is_active_from_expires_ms(expires_ms: int | float | None) -> bool:
    """
    Checks if the subscription is active based on expiration time in milliseconds.
    If expires_ms is None, it assumes the subscription is lifetime or non-expiring (Active).
    """
    if not expires_ms:
        # No expiration date implies lifetime or valid
        return True
    
    now = datetime.now(timezone.utc)
    dt = parse_ms_to_dt(expires_ms)
    if not dt:
        return False # invalid data treated as inactive to be safe, or should allow? 
                     # Logic: if None (missing) -> True logic above handles it. 
                     # If unparseable -> False.
    
    return dt > now
