
import firebase_admin
from firebase_admin import auth, credentials
import os

TARGET_UID = "H2oQZPuK9EhnA9NUr6QqESNP6sa2"

def set_claims():
    key_path = "classnote-api-key.json"
    if os.path.exists(key_path):
        cred = credentials.Certificate(key_path)
    else:
        cred = credentials.ApplicationDefault()

    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
        
    print(f"Setting Custom Claims for {TARGET_UID}...")
    
    # Set 'plan' claim to 'pro'
    # Also 'subscriptionStatus' to 'active' if needed
    claims = {
        "plan": "pro",
        "subscriptionStatus": "active"
    }
    
    auth.set_custom_user_claims(TARGET_UID, claims)
    print(f"Successfully set claims: {claims}")
    
    # User needs to refresh token (re-login) to see this.
    user = auth.get_user(TARGET_UID)
    print(f"User Email: {user.email}")
    print(f"Current Claims: {user.custom_claims}")

if __name__ == "__main__":
    set_claims()
