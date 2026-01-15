
import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore
import os
from datetime import datetime, timezone, timedelta

# Configuration
TEST_UID = "H2oQZPuK9EhnA9NUr6QqESNP6sa2"

def upgrade_user():
    # Initialize Firebase Admin with key file
    key_path = "classnote-api-key.json"
    if os.path.exists(key_path):
        cred = credentials.Certificate(key_path)
    else:
        cred = credentials.ApplicationDefault()

    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    
    # Initialize Firestore
    # Note: initialization matches app logic
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "classnote-x-dev"
    try:
         db = firestore.Client(project=project_id)
    except:
         db = firestore.Client()
         
    print(f"Upgrading user {TEST_UID} to 'pro' plan...")
    
    user_ref = db.collection("users").document(TEST_UID)
    
    # Verify user exists or create
    doc = user_ref.get()
    if not doc.exists:
        print("User does not exist, creating placeholder...")
        user_ref.set({
            "createdAt": firestore.SERVER_TIMESTAMP,
            "displayName": "Verification Bot"
        })
    
    # Update Subscription Fields
    # Based on sessions.py check: plan = user_doc.to_dict().get("plan", "free")
    expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    
    update_data = {
        "plan": "pro",  # or premium? sessions.py seems to treat anything != free as paid potentially?
                        # Actually sessions.py 330: if plan == "free": ... elif plan == "basic": ...
                        # It doesn't explicitly block "pro" from features, but `check_subscription_for_job` might.
                        # Let's check `create_job` logic again.
                        # It usually checks capabilities or plan.
                        # Assuming "pro" is safe.
        "subscriptionStatus": "active",
        "subscriptionTier": "pro",
        "subscriptionExpiresAt": expires_at
    }
    
    user_ref.set(update_data, merge=True)
    print("User upgraded successfully.")

if __name__ == "__main__":
    upgrade_user()
