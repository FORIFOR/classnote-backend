
import sys
import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore

if len(sys.argv) > 1:
    SESSION_ID = sys.argv[1]
else:
    SESSION_ID = "lecture-1767568426636-4e11be"

try:
    db = firestore.Client()
except:
    pass

doc = db.collection("sessions").document(SESSION_ID).get()
data = doc.to_dict()
print(f"Session: {SESSION_ID}")
print(f"SummaryStatus: {data.get('summaryStatus')}")
print(f"QuizStatus: {data.get('quizStatus')}")
print(f"QuizMarkdown Len: {len(data.get('quizMarkdown') or '')}")

# Check Jobs
jobs = db.collection("sessions").document(SESSION_ID).collection("jobs").stream()
print("Jobs:")
for j in jobs:
    jd = j.to_dict()
    print(f"[{jd.get('type')}] {jd.get('status')} (ID: {j.id})")
