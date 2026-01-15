
import firebase_admin
from firebase_admin import auth, credentials
from google.cloud import firestore
import requests
import json
import os
import time
import datetime

# Configuration
API_KEY = "AIzaSyDdf_xue7WNYCFUcLVJCAiG-OUFupqyoTk"
BASE_URL = "https://classnote-api-900324644592.asia-northeast1.run.app"
TEST_UID = "H2oQZPuK9EhnA9NUr6QqESNP6sa2"

DUMMY_TRANSCRIPT = """
はい、では今日は「人工知能と倫理」について話をしていきます。
まず、AI技術が急速に発展している現在、技術的な課題だけでなく、倫理的な課題も非常に重要になっています。
例えば、自動運転車が事故を避けられない状況になったとき、誰の命を優先すべきかという「トロッコ問題」のようなジレンマがあります。
また、AIによる偏見、バイアスの問題もあります。学習データに偏りがあると、AIの判断も偏ったものになり、特定の人種や性別に対して不利益を与える可能性があります。
最近では生成AI、例えばChatGPTのような大規模言語モデルが登場し、著作権の問題や、フェイクニュースの拡散といった新たな問題も浮上しています。
私たちは、単にAIを開発・利用するだけでなく、こうした倫理的な影響を常に考え、責任あるAIの社会実装を目指す必要があります。
次回の授業では、具体的なガイドラインについて見ていきましょう。
"""

def get_id_token():
    key_path = "classnote-api-key.json"
    cred = None
    if os.path.exists(key_path):
        cred = credentials.Certificate(key_path)
    else:
        cred = credentials.ApplicationDefault()

    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    
    print(f"Minting custom token for {TEST_UID}...")
    try:
        custom_token = auth.create_custom_token(TEST_UID)
        if isinstance(custom_token, bytes):
            custom_token = custom_token.decode('utf-8')
            
        exchange_url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={API_KEY}"
        payload = {"token": custom_token, "returnSecureToken": True}
        resp = requests.post(exchange_url, json=payload)
        resp.raise_for_status()
        return resp.json()["idToken"]
    except Exception as e:
        print(f"Token error: {e}")
        raise e

def inject_transcript(session_id):
    print(f"Injecting dummy transcript to {session_id} via Firestore...")
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "classnote-x-dev"
    try:
        db = firestore.Client(project=project_id)
    except:
        db = firestore.Client()
        
    db.collection("sessions").document(session_id).update({
        "transcriptText": DUMMY_TRANSCRIPT,
        "mode": "lecture",
        "status": "stored" # Set status to indicate recording done
    })
    print("Transcript injected.")

def poll_for_results(session_id, jobs_triggered):
    print("Polling for results (timeout 60s)...")
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "classnote-x-dev"
    try:
        db = firestore.Client(project=project_id)
    except:
        db = firestore.Client()
        
    start_time = time.time()
    completed_jobs = set()
    
    while time.time() - start_time < 60:
        doc = db.collection("sessions").document(session_id).get()
        data = doc.to_dict()
        
        summary = data.get("summaryMarkdown")
        quiz = data.get("quiz")
        
        if summary and "summary" not in completed_jobs:
            print(f"\n✅ Summary Generated:\n{summary[:200]}...\n")
            completed_jobs.add("summary")
            
        if quiz and "quiz" not in completed_jobs:
            print(f"\n✅ Quiz Generated:\n{json.dumps(quiz, indent=2, ensure_ascii=False)}\n")
            completed_jobs.add("quiz")
            
        if "summary" in completed_jobs and "quiz" in completed_jobs:
            print("All jobs completed!")
            return

        time.sleep(2)
        
    print("Timeout reached.")

def run_test():
    token = get_id_token()
    headers = {"Authorization": f"Bearer {token}"}
    
    # 1. Create Session
    print("Creating Session...")
    resp = requests.post(f"{BASE_URL}/sessions", json={"title": "Verification w/ Dummy Data"}, headers=headers)
    if resp.status_code not in [200, 201]:
        print(f"Create Failed: {resp.text}")
        return
    session_id = resp.json()["id"]
    print(f"Session ID: {session_id}")
    
    # 2. Inject Transcript
    inject_transcript(session_id)
    
    # 3. Trigger Jobs
    print("Triggering Summary Job...")
    resp = requests.post(f"{BASE_URL}/sessions/{session_id}/jobs", json={"type": "summary"}, headers=headers)
    if resp.status_code != 200:
        print(f"Summary Start Failed: {resp.text}")
    
    print("Triggering Quiz Job...")
    resp = requests.post(f"{BASE_URL}/sessions/{session_id}/jobs", json={"type": "quiz"}, headers=headers)
    if resp.status_code != 200:
        print(f"Quiz Start Failed: {resp.text}")
        
    # 4. Poll
    poll_for_results(session_id, ["summary", "quiz"])

if __name__ == "__main__":
    run_test()
