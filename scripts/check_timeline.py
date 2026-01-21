
import os
import sys
from datetime import date, timedelta
from google.cloud.firestore_v1.base_query import FieldFilter

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.firebase import db

def check_timeline(uid):
    print(f"Checking timeline query for {uid}...")
    
    end_date = date.today()
    start_date = end_date - timedelta(days=30)
    
    print(f"Query: user_id=={uid}, date >= {start_date.isoformat()} <= {end_date.isoformat()}")
    
    try:
        docs = db.collection("user_daily_usage") \
                .where(filter=FieldFilter("user_id", "==", uid)) \
                .where(filter=FieldFilter("date", ">=", start_date.isoformat())) \
                .where(filter=FieldFilter("date", "<=", end_date.isoformat())) \
                .order_by("date") \
                .stream()
        
        count = 0
        for d in docs:
            print(f"Found doc: {d.id} -> {d.to_dict().get('date')}")
            count += 1
            
        print(f"Success! Query returned {count} documents.")
        return True
    except Exception as e:
        print(f"Query Failed: {e}")
        return False

if __name__ == "__main__":
    # Use the guest UID from logs if available, or just a dummy one
    target_uid = "zz7KpWgc2iNaoQwLVNYsCwewL8d2" 
    if len(sys.argv) > 1:
        target_uid = sys.argv[1]
    
    check_timeline(target_uid)
