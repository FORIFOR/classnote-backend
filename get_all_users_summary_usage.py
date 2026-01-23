import os
import firebase_admin
from firebase_admin import credentials, firestore
from collections import defaultdict
import sys

# Initialize Firebase
if not firebase_admin._apps:
    # Try to use Application Default Credentials first
    try:
        cred = credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred)
        print("Initialized Firebase with Application Default Credentials.")
    except Exception as e:
        print(f"ADC failed: {e}. Trying local key file.")
        # Fallback to local key file
        key_path = "classnote-api-key.json"
        if os.path.exists(key_path):
            cred = credentials.Certificate(key_path)
            firebase_admin.initialize_app(cred)
            print("Initialized Firebase with local key file.")
        else:
            print("ERROR: Could not initialize Firebase. Provide ADC or a local key file.")
            sys.exit(1)


db = firestore.client()

def get_all_summary_usage():
    """
    Fetches and aggregates summary generation counts for all users across all months.
    """
    print("Fetching all users...")
    users = db.collection("users").stream()
    
    user_summary_counts = defaultdict(int)
    
    user_count = 0
    for user in users:
        user_count += 1
        user_id = user.id
        print(f"Processing user {user_id}...")
        
        monthly_usage_docs = user.reference.collection("monthly_usage").stream()
        
        for month_doc in monthly_usage_docs:
            month_data = month_doc.to_dict()
            
            # Count 'summary_generated' for free/basic plans
            summary_count = month_data.get("summary_generated", 0)
            if summary_count > 0:
                user_summary_counts[user_id] += int(summary_count)

            # Count 'llm_calls' for premium plans (which includes summaries)
            llm_calls = month_data.get("llm_calls", 0)
            if llm_calls > 0:
                user_summary_counts[user_id] += int(llm_calls)

    print("\n" + "="*60)
    print(f"Finished processing {user_count} users.")
    print("="*60 + "\n")

    if not user_summary_counts:
        print("No summary usage found for any user.")
        return

    # Sort users by summary count descending
    sorted_users = sorted(user_summary_counts.items(), key=lambda item: item[1], reverse=True)
    
    print(f"{'User ID':<40} | {'Total Summary Generations'}")
    print("-" * 60)
    
    for user_id, count in sorted_users:
        print(f"{user_id:<40} | {count}")
        
    print("-" * 60)


if __name__ == "__main__":
    get_all_summary_usage()
