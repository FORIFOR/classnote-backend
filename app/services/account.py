import hashlib

def account_id_from_phone(phone_e164: str) -> str:
    """
    Generate a deterministic Account ID from an E.164 phone number.
    Key: SHA256(phone)
    """
    if not phone_e164:
        raise ValueError("Phone number is required")
    return hashlib.sha256(phone_e164.strip().encode("utf-8")).hexdigest()
