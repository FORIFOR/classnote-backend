from fastapi import Depends, Header, HTTPException
from app.auth.firebase import verify_firebase_id_token, current_user_from_claims, CurrentUser

def get_current_user(authorization: str = Header(...)) -> CurrentUser:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization header")
        
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
        
    token = authorization.removeprefix("Bearer ").strip()
    claims = verify_firebase_id_token(token)
    return current_user_from_claims(claims)
