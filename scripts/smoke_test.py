import argparse
import asyncio
import json
import os
import sys
import uuid
from typing import Optional

import requests
import websockets
from google.cloud import firestore

# Configuration defaults
DEFAULT_BASE_URL = "http://localhost:8080"
DEFAULT_PROJECT_ID = "classnote-x-dev"
DEFAULT_AUDIO_BUCKET = "classnote-x-audio"

def parse_args():
    parser = argparse.ArgumentParser(description="Smoke test for ClassnoteX Backend")
    parser.add_argument("--url", default=DEFAULT_BASE_URL, help="Base URL of the API")
    parser.add_argument("--project", default=DEFAULT_PROJECT_ID, help="GCP Project ID")
    parser.add_argument("--skip-ws", action="store_true", help="Skip WebSocket tests")
    parser.add_argument("--skip-gcp", action="store_true", help="Skip direct GCP checks (Firestore)")
    return parser.parse_args()

def print_pass(msg):
    print(f"✅ {msg}")

def print_fail(msg):
    print(f"❌ {msg}")

def print_info(msg):
    print(f"ℹ️ {msg}")

# --- Tests ---

def test_health(base_url):
    print_info("Testing /health ...")
    try:
        resp = requests.get(f"{base_url}/health")
        if resp.status_code == 200 and resp.json().get("status") == "ok":
            print_pass("Health check passed")
            return True
        else:
            print_fail(f"Health check failed: {resp.status_code} {resp.text}")
            return False
    except Exception as e:
        print_fail(f"Health check exception: {e}")
        return False

def test_create_session(base_url):
    print_info("Testing POST /sessions ...")
    try:
        payload = {
            "title": f"Smoke Test {uuid.uuid4().hex[:6]}",
            "mode": "lecture",
            "userId": "smoke-test-user"
        }
        resp = requests.post(f"{base_url}/sessions", json=payload)
        if resp.status_code == 200:
            data = resp.json()
            session_id = data.get("id")
            if session_id:
                print_pass(f"Session created: {session_id}")
                return session_id
            else:
                print_fail("Session ID missing in response")
        else:
            print_fail(f"Create session failed: {resp.status_code} {resp.text}")
    except Exception as e:
        print_fail(f"Create session exception: {e}")
    return None

async def test_websocket(base_url, session_id):
    print_info(f"Testing WebSocket /ws/stream/{session_id} ...")
    # Convert http/https to ws/wss
    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
    url = f"{ws_url}/ws/stream/{session_id}"
    
    try:
        async with websockets.connect(url) as ws:
            # Send start
            start_msg = {
                "event": "start",
                "config": {
                    "languageCode": "ja-JP",
                    "sampleRateHertz": 16000
                }
            }
            await ws.send(json.dumps(start_msg))
            resp = await ws.recv()
            print_info(f"WS Recv after start: {resp}")
            
            # Send dummy audio
            await ws.send(os.urandom(1024)) # 1KB dummy audio
            print_info("Sent dummy audio chunk")
            
            await ws.close()
            print_pass("WebSocket connected, sent data, and closed")
            return True
    except Exception as e:
        print_fail(f"WebSocket test failed: {e}")
        return False

def setup_transcript(project_id, session_id):
    print_info(f"Setting up dummy transcript for {session_id} in Firestore...")
    try:
        db = firestore.Client(project=project_id)
        doc_ref = db.collection("sessions").document(session_id)
        doc_ref.update({
            "transcriptText": "これはスモークテスト用の講義文字起こしです。AIは人口知能の略で、近年急速に発展しています。ディープラーニングがその中心技術です。"
        })
        print_pass("Transcript set in Firestore")
        return True
    except Exception as e:
        print_fail(f"Firestore update failed: {e}")
        return False

def test_summarize(base_url, session_id):
    print_info(f"Testing POST /sessions/{session_id}/summarize ...")
    try:
        resp = requests.post(f"{base_url}/sessions/{session_id}/summarize")
        if resp.status_code == 200:
            data = resp.json()
            if data.get("summary"):
                print_pass("Summary generated successfully")
                print_info(f"Summary preview: {data['summary'][:50]}...")
                return True
            else:
                print_fail("Summary field missing in response")
        else:
            print_fail(f"Summarize failed: {resp.status_code} {resp.text}")
    except Exception as e:
        print_fail(f"Summarize exception: {e}")
    return False

def test_quiz(base_url, session_id):
    print_info(f"Testing POST /sessions/{session_id}/quiz ...")
    try:
        resp = requests.post(f"{base_url}/sessions/{session_id}/quiz?count=3")
        if resp.status_code == 200:
            data = resp.json()
            if data.get("quizMarkdown"):
                print_pass("Quiz generated successfully")
                return True
            else:
                print_fail("quizMarkdown field missing in response")
        else:
            print_fail(f"Quiz failed: {resp.status_code} {resp.text}")
    except Exception as e:
        print_fail(f"Quiz exception: {e}")
    return False

# --- Main ---

async def main():
    args = parse_args()
    
    if not test_health(args.url):
        sys.exit(1)
        
    session_id = test_create_session(args.url)
    if not session_id:
        sys.exit(1)
        
    if not args.skip_ws:
        await test_websocket(args.url, session_id)
        
    if not args.skip_gcp:
        if setup_transcript(args.project, session_id):
            # Give it a moment? Firestore updates are usually fast, but consistency...
            test_summarize(args.url, session_id)
            test_quiz(args.url, session_id)
        else:
            print_info("Skipping summarize/quiz verification due to Firestore setup failure")
    else:
        print_info("Skipping GCP dependent tests (summarize/quiz need transcript in DB)")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
