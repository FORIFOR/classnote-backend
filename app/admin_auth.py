from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from firebase_admin import auth
import logging
import os

logger = logging.getLogger("app.admin_auth")
security = HTTPBearer()

# Admin UID whitelist — prefer environment variable, fallback to hardcoded owner UIDs
_ADMIN_WHITELIST_RAW = os.environ.get("ADMIN_UIDS", "eCgQGszHJZS3vHlLQ7jBorCQAK72,16PRzcKCQrSsqR2d8UnnIjnssh02,tLy5z7eWb3bQAezw9EmvQvM6HbR2")
ADMIN_WHITELIST = [uid.strip() for uid in _ADMIN_WHITELIST_RAW.split(",") if uid.strip()]

def get_current_admin_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    Validates Firebase ID Token and checks for 'admin' custom claim.
    Returns the decoded token if valid and admin.
    """
    token = credentials.credentials

    try:
        decoded_token = auth.verify_id_token(token)
        uid = decoded_token.get("uid")

        is_admin = decoded_token.get("admin", False) or uid in ADMIN_WHITELIST

        if not is_admin:
            logger.warning(f"Admin access denied for user {uid}: Missing admin claim")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin privileges required"
            )

        return decoded_token
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Admin token verification failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials"
        )
