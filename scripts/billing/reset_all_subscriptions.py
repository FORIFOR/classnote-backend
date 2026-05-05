
import firebase_admin
from firebase_admin import credentials, firestore
import os

# Initialize Firebase
if not firebase_admin._apps:
    cred = credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred, {
        "projectId": os.getenv("GOOGLE_CLOUD_PROJECT"),
    })

db = firestore.client()

def reset_all_subscriptions():
    print("Starting subscription reset for ALL users...")
    
    users_ref = db.collection("users")
    docs = users_ref.stream()
    
    count = 0
    batch = db.batch()
    
    for doc in docs:
        user_data = doc.to_dict()
        current_plan = user_data.get("plan", "free")
        
        # We want to reset everyone, or maybe just non-free users?
        # User said "All users".
        
        # Fields to reset
        update_data = {
            "plan": "free",
            "subscriptionPlatform": firestore.DELETE_FIELD,
            "planUpdatedAt": firestore.SERVER_TIMESTAMP
        }
        
        batch.update(doc.reference, update_data)
        count += 1
        
        if count % 400 == 0:
            batch.commit()
            batch = db.batch()
            print(f"Processed {count} users...")
            
    if count % 400 != 0:
        batch.commit()
        
    print(f"Successfully reset subscriptions for {count} users.")

if __name__ == "__main__":
    reset_all_subscriptions()
