import firebase_admin
from firebase_admin import credentials, firestore
import os
import sys

# Add app directory to path to allow imports if needed, though we use direct firestore here
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

def clean_zombie_claims(dry_run=True):
    """
    Scans username_claims collection.
    If the claimed 'uid' does not exist in 'users' collection, delete the claim.
    """
    
    # Initialize Firebase (Assume GOOGLE_APPLICATION_CREDENTIALS is set)
    if not firebase_admin._apps:
        cred = credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred)
    
    db = firestore.client()
    
    print(f"--- Starting Zombie Username Cleanup (Dry Run: {dry_run}) ---")
    
    claims_ref = db.collection("username_claims")
    users_ref = db.collection("users")
    
    claims = list(claims_ref.stream())
    print(f"Total claims found: {len(claims)}")
    
    zombies = []
    
    for claim in claims:
        data = claim.to_dict()
        uid = data.get("uid")
        username = data.get("username") or claim.id
        
        if not uid:
            print(f"[WARN] Invalid claim {claim.id}: No UID")
            zombies.append(claim)
            continue
            
        # Check if user exists
        user_doc = users_ref.document(uid).get()
        if not user_doc.exists:
            print(f"[ZOMBIE] Username '{claim.id}' claimed by missing uid='{uid}'")
            zombies.append(claim)
        else:
            # User exists, check if consistency (optional)
            user_data = user_doc.to_dict()
            current_username = user_data.get("username")
            if current_username != username:
                 print(f"[MISMATCH] Username '{claim.id}' claimed by uid='{uid}', but user has '{current_username}'")
                 # This is tricky: user might have changed username (not implemented yet) or claim is stale.
                 # For safety, only delete if user treats this as NOT their username?
                 # Considering claim_username implementation, we assume immutable for now.
                 pass

    print(f"--- Scan Complete. Found {len(zombies)} zombie claims. ---")
    
    if not dry_run and zombies:
        print("Proceeding with DELETION...")
        batch = db.batch()
        count = 0
        for z in zombies:
            batch.delete(z.reference)
            count += 1
            if count >= 400: # Batch limit safe buffer
                batch.commit()
                batch = db.batch()
                count = 0
        if count > 0:
            batch.commit()
        print("Deletion Complete.")
    elif zombies:
        print("Dry run finished. Run with dry_run=False to execute.")
    else:
        print("No zombies found. Clean.")

if __name__ == "__main__":
    # Check for --execute flag
    execute = "--execute" in sys.argv
    clean_zombie_claims(dry_run=not execute)
