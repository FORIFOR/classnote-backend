import hashlib

def account_id_from_phone(phone_e164: str) -> str:
    """
    Generate a deterministic account ID from an E.164 phone number.
    Uses SHA256 hash.
    """
    if not phone_e164:
        raise ValueError("Phone number cannot be empty")
    return hashlib.sha256(phone_e164.encode("utf-8")).hexdigest()
