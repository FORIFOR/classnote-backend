from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from firebase_admin import auth
import logging

# Ensure firebase app is initialized (it usually is in app.firebase)
# but for dependencies we might need to be careful about circular imports if app.firebase imports dependencies.
# Assuming app.firebase is safe or initialized at startup.
# Just need firebase_admin which is global.

logger = logging.getLogger("app.dependencies")
security = HTTPBearer()

def get_current_admin_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    Validates Firebase ID Token and checks for 'admin' custom claim.
    Returns the decoded token if valid and admin.
    """
    token = credentials.credentials

    # [SECURITY] Admin bypass removed - use proper Firebase custom claims for admin access

    try:
        decoded_token = auth.verify_id_token(token)
        
        # Check for admin claim
        uid = decoded_token.get("uid")
        
        # [DEV] Whitelist fallback for owner
        ADMIN_WHITELIST = ["eCgQGszHJZS3vHlLQ7jBorCQAK72", "16PRzcKCQrSsqR2d8UnnIjnssh02"]
        is_in_whitelist = uid in ADMIN_WHITELIST
        
        # DEBUG LOG
        print(f"DEBUG AUTH: uid={uid}, whitelist={ADMIN_WHITELIST}, match={is_in_whitelist}, claim={decoded_token.get('admin')}")
        
        is_admin = decoded_token.get("admin", False) or is_in_whitelist
        
        if not is_admin:
            logger.warning(f"Admin access denied for user {decoded_token.get('uid')}: Missing admin claim")
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
