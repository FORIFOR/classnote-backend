
import sys
import os
import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore

# Add project root to path
sys.path.append("/Users/horioshuuhei/Projects/classnote-api")

# Configuration
SESSION_ID = "lecture-1767485056140-1d2fc3" # Known session

# Initialize Firestore
try:
    db = firestore.Client()
except Exception as e:
    print(f"Failed to init Firestore: {e}")
    sys.exit(1)

doc = db.collection("sessions").document(SESSION_ID).get()
if not doc.exists:
    print("Session not found in DB")
    sys.exit(1)

data = doc.to_dict()
owner_id = data.get("ownerUserId") or data.get("userId")
print(f"Owner UID: {owner_id}")

print("-" * 20)
print(f"Session Data (DB):")
print(f"summaryStatus: {data.get('summaryStatus')}")
print(f"summaryMarkdown (len): {len(data.get('summaryMarkdown') or '')}")
print(f"playlistStatus: {data.get('playlistStatus')}")
print(f"quizStatus: {data.get('quizStatus')}")
print(f"quizMarkdown (len): {len(data.get('quizMarkdown') or '')}")
print("-" * 20)

# Derived Docs
for kind in ["summary", "quiz"]:
    dd = db.collection("sessions").document(SESSION_ID).collection("derived").document(kind).get()
    if dd.exists:
        ddata = dd.to_dict()
        print(f"Derived[{kind}] status: {ddata.get('status')}")
    else:
        print(f"Derived[{kind}] NOT FOUND")
print("-" * 20)

# Generic Jobs
print("Generic Jobs History:")
jobs = db.collection("sessions").document(SESSION_ID).collection("jobs").stream()
for job in jobs:
    jd = job.to_dict()
    print(f"Job[{job.id}]: type={jd.get('type')}, status={jd.get('status')}, err={jd.get('errorReason')}")
print("-" * 20)
