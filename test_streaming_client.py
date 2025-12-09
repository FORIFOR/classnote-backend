#!/usr/bin/env python3
"""
End-to-end test script for Classnote API on Cloud Run.

What it does:
 1) Create a session (HTTP)
 2) Optional: streaming ASR over WebSocket (send WAV as PCM)
 3) Get signed upload URL (HTTP) and PUT the WAV file
 4) Start batch transcription (HTTP)
 5) Poll refresh_transcript until transcribed
 6) Call summarize / quiz / qa

Environment variables you can override:
  BASE_URL                default: https://classnote-api-900324644592.asia-northeast1.run.app
  FIREBASE_ID_TOKEN       Authorization bearer (required if your service enforces auth)
  USER_ID                 default: test-user
  MODE                    default: lecture
  TITLE                   default: CloudBuildテスト講義
  AUDIO_FILE              default: /Users/horioshuuhei/Downloads/audiodata_normal_080.wav
  QUESTION                default: この講義のポイントは？

Requires: requests, websockets
"""

import asyncio
import json
import os
import time
import wave
from typing import Optional

import requests
import websockets

BASE_URL = os.getenv(
    "BASE_URL", "https://classnote-api-900324644592.asia-northeast1.run.app"
)
TOKEN = os.getenv("FIREBASE_ID_TOKEN")
USER_ID = os.getenv("USER_ID", "test-user")
MODE = os.getenv("MODE", "lecture")
TITLE = os.getenv("TITLE", "CloudBuildテスト講義")
AUDIO_FILE = os.getenv(
    "AUDIO_FILE", "/Users/horioshuuhei/Downloads/audiodata_normal_080.wav"
)
QUESTION = os.getenv("QUESTION", "この講義のポイントは？")


def headers() -> dict:
    h = {"Content-Type": "application/json"}
    if not TOKEN:
        raise RuntimeError(
            "FIREBASE_ID_TOKEN is not set. Export a Firebase ID token before running."
        )
    h["Authorization"] = f"Bearer {TOKEN}"
    return h


def pick_url(obj: dict) -> Optional[str]:
    return obj.get("url") or obj.get("uploadUrl") or obj.get("upload_url")


def create_session() -> str:
    payload = {"title": TITLE, "mode": MODE, "userId": USER_ID}
    resp = requests.post(f"{BASE_URL}/sessions", headers=headers(), json=payload)
    if resp.status_code >= 300:
        print("[sessions] error:", resp.status_code, resp.text)
        resp.raise_for_status()
    data = resp.json()
    sid = data.get("id")
    print(f"[sessions] created: {data}")
    if not sid:
        raise RuntimeError("session id missing")
    return sid


async def streaming_test(session_id: str):
    """Send WAV over WebSocket and print partial/final."""
    token_q = f"?token={TOKEN}" if TOKEN else ""
    ws_url = f"{BASE_URL.replace('https://', 'wss://').replace('http://', 'ws://')}/ws/stream/{session_id}{token_q}"
    print(f"[ws] connect {ws_url}")
    async with websockets.connect(ws_url, open_timeout=30) as ws:
        msg = await ws.recv()
        print("[ws<-]", msg)

        await ws.send(
            json.dumps(
                {
                    "event": "start",
                    "config": {
                        "languageCode": "ja-JP",
                        "sampleRateHertz": 16000,
                        "enableSpeakerDiarization": True,
                        "speakerCount": 2,
                        "model": "latest_long",
                    },
                }
            )
        )
        print("[ws->] start")

        with wave.open(AUDIO_FILE, "rb") as wf:
            sr, ch, width = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
            if sr != 16000:
                print(f"[warn] sample rate {sr} != 16000")
            if ch != 1:
                print(f"[warn] channels {ch} != 1")
            if width != 2:
                print(f"[warn] sample width {width} != 2")

            chunk_frames = int(sr * 0.1)
            while True:
                frames = wf.readframes(chunk_frames)
                if not frames:
                    break
                if ch == 2:  # downmix simple L channel
                    frames = b"".join(frames[i : i + 2] for i in range(0, len(frames), 4))
                await ws.send(frames)
                await asyncio.sleep(0.1)

        await ws.send(json.dumps({"event": "stop"}))
        print("[ws->] stop")

        try:
            while True:
                resp = await ws.recv()
                print("[ws<-]", resp)
        except websockets.ConnectionClosed:
            print("[ws] closed")


def get_upload_url(session_id: str) -> str:
    payload = {"sessionId": session_id, "mode": MODE, "contentType": "audio/wav"}
    resp = requests.post(f"{BASE_URL}/upload-url", headers=headers(), json=payload)
    resp.raise_for_status()
    data = resp.json()
    url = pick_url(data)
    if not url:
        raise RuntimeError(f"upload url not found in {data}")
    print(f"[upload-url] ok, expiresAt={data.get('expiresAt')}")
    return url


def upload_audio(signed_url: str):
    with open(AUDIO_FILE, "rb") as f:
        r = requests.put(signed_url, headers={"Content-Type": "audio/wav"}, data=f)
    if r.status_code >= 300:
        raise RuntimeError(f"upload failed: {r.status_code} {r.text}")
    print("[upload] success")


def start_transcribe(session_id: str):
    payload = {"mode": MODE}
    resp = requests.post(
        f"{BASE_URL}/sessions/{session_id}/start_transcribe",
        headers=headers(),
        json=payload,
    )
    resp.raise_for_status()
    print(f"[start_transcribe] {resp.status_code} {resp.text}")


def refresh_transcript(session_id: str, max_attempts: int = 10, wait_sec: int = 5):
    for attempt in range(1, max_attempts + 1):
        resp = requests.post(
            f"{BASE_URL}/sessions/{session_id}/refresh_transcript",
            headers=headers(),
            json={},
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status") or data.get("status".lower())
        text = data.get("transcriptText") or data.get("transcript")
        print(f"[refresh {attempt}] status={status} text_len={len(text or '')}")
        if status == "transcribed" and text:
            return text
        time.sleep(wait_sec)
    raise RuntimeError("transcript not ready in time")


def summarize(session_id: str):
    resp = requests.post(
        f"{BASE_URL}/sessions/{session_id}/summarize",
        headers=headers(),
        json={},
    )
    resp.raise_for_status()
    data = resp.json()
    print("[summarize] status:", data.get("status"), "summary keys:", list(data.get("summary", {}).keys()))


def quiz(session_id: str, count: int = 5):
    resp = requests.post(
        f"{BASE_URL}/sessions/{session_id}/quiz",
        headers=headers(),
        params={"count": count},
        json={},
    )
    resp.raise_for_status()
    data = resp.json()
    qs = data.get("questions", [])
    print(f"[quiz] count={len(qs)}")
    if qs:
        print("  Q1:", qs[0].get("question"))


def qa(session_id: str):
    resp = requests.post(
        f"{BASE_URL}/sessions/{session_id}/qa",
        headers=headers(),
        json={"question": QUESTION},
    )
    resp.raise_for_status()
    data = resp.json()
    print("[qa] answer:", (data.get("answer") or "")[:80], "...")


async def main():
    print(f"[config] BASE_URL={BASE_URL}")
    session_id = create_session()

    # 1) Streaming test (non-blocking with same WAV)
    try:
        await streaming_test(session_id)
    except Exception as e:
        print("[warn] streaming test failed:", e)

    # 2) Upload + batch STT flow
    signed_url = get_upload_url(session_id)
    upload_audio(signed_url)
    start_transcribe(session_id)
    transcript = refresh_transcript(session_id)
    print("[transcript] snippet:", transcript[:120], "...")

    # 3) LLM features
    summarize(session_id)
    quiz(session_id, count=5)
    qa(session_id)


if __name__ == "__main__":
    asyncio.run(main())
