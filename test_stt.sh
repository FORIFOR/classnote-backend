#!/usr/bin/env bash
set -euo pipefail

# 環境変数 CLOUDRUN_URL にデプロイ先の URL を設定しておくこと。
# 例: export CLOUDRUN_URL="https://classnote-api-900324644592.asia-northeast1.run.app"

if [[ -z "${CLOUDRUN_URL:-}" ]]; then
  echo "CLOUDRUN_URL が未設定です" >&2
  exit 1
fi

AUDIO_PATH="${1:-${AUDIO_PATH:-}}"
if [[ -z "$AUDIO_PATH" ]]; then
  echo "音声ファイルパスを引数または AUDIO_PATH で指定してください" >&2
  exit 1
fi

if [[ ! -f "$AUDIO_PATH" ]]; then
  echo "音声ファイルが見つかりません: $AUDIO_PATH" >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "jq が必要です。インストールしてください。" >&2
  exit 1
fi

echo "1) セッション作成..."
SESSION_RESP=$(curl -s -X POST "$CLOUDRUN_URL/sessions" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "テスト講義1",
    "mode": "lecture",
    "userId": "test-user"
  }')
echo "$SESSION_RESP"
SESSION_ID=$(echo "$SESSION_RESP" | jq -r '.id')
echo "SESSION_ID=$SESSION_ID"

echo "2) 署名付きURL取得..."
UPLOAD_RESP=$(curl -s -X POST "$CLOUDRUN_URL/upload-url" \
  -H "Content-Type: application/json" \
  -d "{\"sessionId\": \"${SESSION_ID}\", \"contentType\": \"audio/wav\"}")
echo "$UPLOAD_RESP"
UPLOAD_URL=$(echo "$UPLOAD_RESP" | jq -r '.uploadUrl')

echo "3) 音声アップロード..."
curl -s -i -X PUT "$UPLOAD_URL" \
  -H "Content-Type: audio/wav" \
  --data-binary "@${AUDIO_PATH}" >/dev/null

echo "4) STT開始..."
curl -s -i -X POST "$CLOUDRUN_URL/sessions/${SESSION_ID}/start_transcribe" \
  -H "Content-Type: application/json" \
  -d "{\"sessionId\": \"${SESSION_ID}\"}" >/dev/null

echo "5) ステータス確認..."
curl -s "$CLOUDRUN_URL/sessions/${SESSION_ID}" | jq .

echo "6) Transcript 取得..."
curl -s -X POST "$CLOUDRUN_URL/sessions/${SESSION_ID}/refresh_transcript" | jq .

echo "完了"
