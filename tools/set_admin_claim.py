import firebase_admin
from firebase_admin import auth, credentials
import sys
import os

# Initialize Firebase Admin SDK
try:
    if not firebase_admin._apps:
        firebase_admin.initialize_app()
except Exception as e:
    print(f"Failed to init firebase: {e}")
    sys.exit(1)

def set_admin_claim(email_or_uid):
    user = None
    try:
        if "@" in email_or_uid:
            # Assume Email
            print(f"Searching by Email: {email_or_uid}")
            user = auth.get_user_by_email(email_or_uid)
        else:
            # Assume UID
            print(f"Searching by UID: {email_or_uid}")
            user = auth.get_user(email_or_uid)
        
        print(f"Found user: {user.uid} ({user.email})")

    except auth.UserNotFoundError:
        print(f"Error: User '{email_or_uid}' not found.")
        return
    except Exception as e:
        print(f"Unexpected error finding user: {e}")
        return

    # Set custom user claims
    current_claims = user.custom_claims or {}
    if current_claims.get("admin") is True:
        print(f"User {user.email} (UID: {user.uid}) is ALREADY an admin.")
        return

    new_claims = {**current_claims, "admin": True}
    auth.set_custom_user_claims(user.uid, new_claims)
    
    print(f"SUCCESS: Set 'admin: true' claim for user {user.email} (UID: {user.uid})")
    print("NOTE: The user must sign out and sign in again (or refresh token) for changes to take effect.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python tools/set_admin_claim.py <email_or_uid>")
        sys.exit(1)
    
    target = sys.argv[1]
    set_admin_claim(target)