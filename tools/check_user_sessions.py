
import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore
import os
import datetime
import json

TARGET_UID = "H2oQZPuK9EhnA9NUr6QqESNP6sa2"

def check_user_activity():
    key_path = "classnote-api-key.json"
    if os.path.exists(key_path):
        cred = credentials.Certificate(key_path)
    else:
        cred = credentials.ApplicationDefault()

    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    
    # Init Firestore
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "classnote-x-dev"
    try:
         db = firestore.Client(project=project_id)
    except:
         db = firestore.Client()
         
    print(f"Checking recent activity for user: {TARGET_UID}")
    
    # Get recent sessions for this user (using simple filtering, might require index if high volume, but limit small)
    sessions = db.collection("sessions").where("ownerUid", "==", TARGET_UID).limit(20).stream()
    
    all_sessions = []
    for s in sessions:
        all_sessions.append(s)
        
    # Sort by updatedAt desc
    all_sessions.sort(key=lambda x: x.to_dict().get("updatedAt", x.to_dict().get("createdAt", datetime.datetime.min.replace(tzinfo=datetime.timezone.utc))), reverse=True)
    
    sessions = all_sessions[:5]
    
    found = False
    for s in sessions:
        found = True
        d = s.to_dict()
        sid = s.id
        title = d.get("title", "No Title")
        updated = d.get("updatedAt")
        
        summary = d.get("summaryMarkdown")
        quiz = d.get("quiz")
        
        print(f"\n[Session] {sid} | Title: {title} | Updated: {updated}")
        
        if summary:
            print(f"  ✅ Summary Generated ({len(summary)} chars)")
            print(f"     Preview: {summary[:100].replace(chr(10), ' ')}...")
        else:
            print("  ❌ No Summary")
            
        if quiz:
            print(f"  ✅ Quiz Generated ({len(str(quiz))} chars)")
        else:
             print("  ❌ No Quiz")
             
        # Check Jobs for this session
        print("  [Jobs]:")
        jobs = db.collection("sessions").document(sid).collection("jobs").order_by("createdAt", direction=firestore.Query.DESCENDING).limit(5).stream()
        j_found = False
        for j in jobs:
            j_found = True
            jd = j.to_dict()
            print(f"    - {jd.get('type')}: {jd.get('status')} (Created: {jd.get('createdAt')})")
            if jd.get("errorReason"):
                print(f"      Error: {jd.get('errorReason')}")
        if not j_found:
            print("    (No recent jobs)")

    if not found:
        print("No sessions found for this user.")

if __name__ == "__main__":
    check_user_activity()
