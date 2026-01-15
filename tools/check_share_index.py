
import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore
import os

def check_share_links_query():
    key_path = "classnote-api-key.json"
    if os.path.exists(key_path):
        cred = credentials.Certificate(key_path)
        try:
            firebase_admin.initialize_app(cred)
        except:
            pass
    else:
        try:
            firebase_admin.initialize_app()
        except:
            pass

    db = firestore.Client()
    
    print("Attempting to query shareLinks by sessionId...")
    try:
        # Try finding *any* link (I won't know a valid ID, so I'll just check if the query executes without error)
        # using a dummy ID.
        docs = db.collection("shareLinks").where("sessionId", "==", "dummy-session-id").limit(1).stream()
        print("Query executed successfully (Result Empty is expected).")
        for d in docs:
            print("Found doc (Unexpected)")
    except Exception as e:
        print(f"Query Failed: {e}")

if __name__ == "__main__":
    check_share_links_query()
