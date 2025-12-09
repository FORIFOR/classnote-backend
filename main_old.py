import json
import os
import re
import asyncio
import queue
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, WebSocket, status, Depends, Header
from pydantic import BaseModel, Field
import logging
from google.api_core.exceptions import NotFound as GcpNotFound
from google.api_core.operation import Operation
from google.cloud import firestore, storage
from google.cloud import speech_v2 as cloud_speech
from google.cloud import speech_v1 as speech
from google.longrunning import operations_pb2
from google.protobuf.json_format import MessageToDict
from vertexai.generative_models import GenerativeModel
import vertexai
import firebase_admin
from firebase_admin import auth as firebase_auth, credentials as firebase_credentials

logger = logging.getLogger("uvicorn.error")

# ===== Speech v2 共通設定 =====
try:
    import google.auth
except Exception:  # pragma: no cover - fallback if google.auth is unavailable
    google = None

_raw_project = os.getenv("GCP_PROJECT")
if _raw_project:
    GCP_PROJECT = _raw_project
elif google and hasattr(google, "auth"):
    try:
        _, GCP_PROJECT = google.auth.default()
    except Exception:
        GCP_PROJECT = None
else:
    GCP_PROJECT = None

if not GCP_PROJECT:
    raise RuntimeError("環境変数 GCP_PROJECT が設定されていません (projectId を指定してください)")
SPEECH_LOCATION = os.getenv("SPEECH_LOCATION", "global")
DEFAULT_MODEL = os.getenv("SPEECH_MODEL", "latest_long")

SPEECH_RECOGNIZER_ID = os.getenv(
    "SPEECH_RECOGNIZER_ID",
    f"projects/{GCP_PROJECT}/locations/{SPEECH_LOCATION}/recognizers/_",
)
SPEECH_STREAMING_RECOGNIZER_ID = os.getenv(
    "SPEECH_STREAMING_RECOGNIZER_ID",
    SPEECH_RECOGNIZER_ID,
)

def normalize_model_name(raw: str | None) -> str:
    """
    フロント/環境変数などから来た model 名を v2 用に正規化する。
    default / None / 空文字 → DEFAULT_MODEL にそろえる。
    """
    if raw is None or raw == "" or raw == "default":
        return DEFAULT_MODEL
    return raw


app = FastAPI()

# 環境変数
_raw_bucket = os.getenv("AUDIO_BUCKET")
if not _raw_bucket:
    raise RuntimeError("環境変数 AUDIO_BUCKET が設定されていません")
# gs:// が付いていても動くように防御的に剥がす
AUDIO_BUCKET = _raw_bucket.replace("gs://", "").rstrip("/")
SIGNED_URL_EXPIRATION_SECONDS = int(os.getenv("SIGNED_URL_EXPIRATION_SECONDS", "900"))
RECOGNIZER_ID = SPEECH_RECOGNIZER_ID
STREAM_RECOGNIZER_ID = SPEECH_STREAMING_RECOGNIZER_ID
VERTEX_REGION = os.getenv("VERTEX_REGION", "asia-northeast1")
# asia-northeast1 で広く利用可能な安定版。必要に応じて環境変数で上書き。
GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL_NAME", "gemini-1.5-flash")

if not AUDIO_BUCKET:
    raise RuntimeError("環境変数 AUDIO_BUCKET が設定されていません")
if not RECOGNIZER_ID:
    raise RuntimeError("環境変数 SPEECH_RECOGNIZER_ID が未設定です")
if not STREAM_RECOGNIZER_ID:
    raise RuntimeError(
        "SPEECH_STREAMING_RECOGNIZER_ID が未設定です。GCP_PROJECT を指定すると global のデフォルト recognizer (_) を利用します。"
    )


async def run_streaming_recognize(
    websocket: WebSocket,
    session_id: str,
    audio_queue: "queue.Queue[bytes | None]",
    streaming_config: speech.StreamingRecognitionConfig,
):
    logger.error(f"[/ws/stream] run_streaming_recognize START session_id={session_id}")

    def request_generator():
        logger.error(f"[/ws/stream] request_generator START session_id={session_id}")

        while True:
            chunk = audio_queue.get()

            if chunk is None:
                logger.error(f"[/ws/stream] request_generator GOT None session_id={session_id}")
                break

            if not chunk:
                continue

            logger.error(
                f"[/ws/stream] request_generator SEND chunk len={len(chunk)} "
                f"session_id={session_id}"
            )
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

        logger.error(f"[/ws/stream] request_generator END session_id={session_id}")

    loop = asyncio.get_running_loop()

    def recognize_task():
        try:
            cfg = streaming_config.config
            logger.error(
                f"[/ws/stream] streaming_recognize CALL sample_rate={cfg.sample_rate_hertz} "
                f"lang={cfg.language_code} session_id={session_id}"
            )
            for response in stream_speech_client.streaming_recognize(
                config=streaming_config,
                requests=request_generator(),
            ):
                if not response.results:
                    logger.error(f"[/ws/stream] GOT response results=0 session_id={session_id}")
                    continue

                logger.error(
                    f"[/ws/stream] GOT response results={len(response.results)} session_id={session_id}"
                )

                for result in response.results:
                    if not result.alternatives:
                        continue

                    transcript = result.alternatives[0].transcript
                    is_final = result.is_final

                    logger.error(
                        f"[/ws/stream] transcript='{transcript}' is_final={is_final} "
                        f"session_id={session_id}"
                    )
                    logger.error(
                        f"[/ws/stream] send transcript to client is_final={is_final} "
                        f"len={len(transcript)} session_id={session_id}"
                    )

                    fut = asyncio.run_coroutine_threadsafe(
                        websocket.send_json(
                            {
                                "type": "transcript",
                                "event": "transcript",
                                "sessionId": session_id,
                                "text": transcript,
                                "isFinal": is_final,
                            }
                        ),
                        loop,
                    )
                    fut.result()
        except Exception:
            logger.exception(f"[/ws/stream] streaming_recognize: error session_id={session_id}")

    await loop.run_in_executor(None, recognize_task)

    logger.error(f"[/ws/stream] run_streaming_recognize END session_id={session_id}")


def _extract_recognizer_location(recognizer_id: str) -> str:
    m = re.match(r"projects/[^/]+/locations/([^/]+)/recognizers/[^/]+", recognizer_id)
    return m.group(1) if m else ""


def _get_session_doc_or_404(session_id: str) -> Dict[str, Any]:
    doc_ref = db.collection(SESSIONS_COLLECTION).document(session_id)
    snapshot = doc_ref.get()
    if not snapshot.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    data = snapshot.to_dict() or {}
    data["_ref"] = doc_ref
    return data


def _get_transcript_text(data: Dict[str, Any]) -> Optional[str]:
    """transcript のフィールド名ゆれを吸収して取得する。"""
    return data.get("transcriptText") or data.get("transcript") or data.get("transcript_text")


def verify_firebase_token(authorization: Optional[str] = Header(default=None)) -> str:
    """Authorization: Bearer ... から Firebase UID を取り出す。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Authorization")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        decoded = firebase_auth.verify_id_token(token)
        return decoded.get("uid") or ""
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc


speech_location = os.getenv("SPEECH_LOCATION", "global")
speech_api_endpoint = "speech.googleapis.com" if speech_location == "global" else f"{speech_location}-speech.googleapis.com"

# GCP クライアントは起動時に一度だけ生成
storage_client = storage.Client(project=GCP_PROJECT)
db = firestore.Client(project=GCP_PROJECT)
speech_client = cloud_speech.SpeechClient(
    client_options={"api_endpoint": speech_api_endpoint}
)
stream_speech_client = speech.SpeechClient()
STREAM_LANGUAGE_CODE = "ja-JP"
# Streaming は LINEAR16 固定。サンプルレート/言語はクライアントの start イベントに合わせるが、
# デフォルトは iOS の送信に合わせて 16kHz。
STREAM_SAMPLE_RATE = 16000
STREAM_ENCODING = speech.RecognitionConfig.AudioEncoding.LINEAR16
# 音声キューの上限（過剰蓄積防止）。50 * 約20ms ≒ 1秒分を目安にする。
MAX_AUDIO_QUEUE_SIZE = 50
vertexai.init(project=GCP_PROJECT, location=VERTEX_REGION)
gemini_model = GenerativeModel(GEMINI_MODEL_NAME)
logger.info(
    "Vertex AI initialized project=%s region=%s model=%s",
    GCP_PROJECT,
    VERTEX_REGION,
    GEMINI_MODEL_NAME,
)

# Firebase Admin (IDトークン検証用)
if not firebase_admin._apps:
    firebase_admin.initialize_app()

# Content-Type から拡張子を決めるための簡易マッピング
CONTENT_TYPE_EXTENSIONS = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
}


SessionMode = Literal["lecture", "meeting"]
SESSIONS_COLLECTION = "sessions"


class CreateSessionRequest(BaseModel):
    title: str
    mode: SessionMode
    user_id: str = Field(..., alias="userId", description="一旦テスト用。後でJWTから取得に変更予定。")

    class Config:
        populate_by_name = True


class CreateSessionResponse(BaseModel):
    id: str
    title: str
    mode: str
    status: str
    owner_id: Optional[str] = Field(None, alias="ownerId")


class UploadUrlRequest(BaseModel):
    session_id: str = Field(..., alias="sessionId", min_length=1)
    content_type: str = Field("audio/webm", alias="contentType", min_length=1)
    mode: Optional[str] = Field(None, alias="mode")

    class Config:
        populate_by_name = True
        json_schema_extra = {
            "example": {
                "sessionId": "sess_abc123",
                "contentType": "audio/webm",
            }
        }


class UploadUrlResponse(BaseModel):
    url: str = Field(..., alias="url")
    upload_url: str = Field(..., alias="uploadUrl")
    object_path: str = Field(..., alias="objectPath")
    content_type: str = Field(..., alias="contentType")
    expires_at: datetime = Field(..., alias="expiresAt")

    class Config:
        populate_by_name = True


class StartTranscribeRequest(BaseModel):
    mode: Optional[str] = None


class StartTranscribeResponse(BaseModel):
    id: str
    status: str
    operation_name: str = Field(..., alias="operationName")

    class Config:
        populate_by_name = True


class SessionDetailResponse(BaseModel):
    id: str
    title: str
    mode: str
    status: str
    user_id: str = Field(..., alias="userId")
    owner_id: Optional[str] = Field(None, alias="ownerId")
    audio_path: Optional[str] = Field(None, alias="audioPath")
    stt_operation: Optional[str] = Field(None, alias="sttOperation")
    transcript_path: Optional[str] = Field(None, alias="transcriptPath")
    content_type: Optional[str] = Field(None, alias="contentType")
    created_at: datetime = Field(..., alias="createdAt")

    class Config:
        populate_by_name = True


class RefreshTranscriptResponse(BaseModel):
    id: str
    status: str
    transcript_text: Optional[str] = Field(None, alias="transcriptText")
    model_config = {"populate_by_name": True}


class SummarizeResponse(BaseModel):
    id: str
    status: str
    summary: Dict[str, Any]


class QuizQuestion(BaseModel):
    id: str
    question: str
    choices: List[Dict[str, str]]
    correct_index: int = Field(..., alias="correct_index")
    explanation: Optional[str] = None

    class Config:
        populate_by_name = True


class QuizResponse(BaseModel):
    session_id: str = Field(..., alias="session_id")
    count: int
    questions: List[QuizQuestion]


class QARequest(BaseModel):
    question: str


class QAResponse(BaseModel):
    id: str
    question: str
    answer: str


class StreamingInitConfig(BaseModel):
    languageCode: str = Field("ja-JP", description="例: ja-JP")
    speakerCount: int = Field(2, description="話者数（固定とする場合）")
    sampleRate: int = Field(16000, description="音声のサンプルレート(Hz)")
    model: str = Field(DEFAULT_MODEL, description="Speech v2 streaming 用モデル名")


def build_streaming_config_from_client(config_dict: dict) -> speech.StreamingRecognitionConfig:
    """
    クライアントから送られてきた config(dict) から StreamingRecognitionConfig を組み立てる。
    iOS からは sampleRateHertz で渡ってくるので sampleRate に揃える。
    """
    cfg = dict(config_dict or {})
    if "sampleRate" not in cfg and "sampleRateHertz" in cfg:
        cfg["sampleRate"] = cfg["sampleRateHertz"]

    try:
        init_cfg = StreamingInitConfig(**cfg)
    except Exception:
        init_cfg = StreamingInitConfig()

    sample_rate = init_cfg.sampleRate or 16000
    language_code = init_cfg.languageCode or STREAM_LANGUAGE_CODE
    speaker_count = max(1, init_cfg.speakerCount or 1)
    enable_diar = bool(getattr(init_cfg, "speakerCount", 0) and speaker_count >= 2)

    recog_config = speech.RecognitionConfig(
        language_code=language_code,
        sample_rate_hertz=sample_rate,
        encoding=STREAM_ENCODING,
        audio_channel_count=1,
        enable_automatic_punctuation=True,
    )
    if enable_diar:
        recog_config.diarization_config = speech.SpeakerDiarizationConfig(
            enable_speaker_diarization=True,
            min_speaker_count=speaker_count,
            max_speaker_count=speaker_count,
        )

    return speech.StreamingRecognitionConfig(
        config=recog_config,
        interim_results=True,
        single_utterance=False,
    )


@app.get("/")
def root():
    return {"status": "root-ok"}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


def _normalized_session_id(raw: str) -> str:
    """セッションIDに使えない文字をハイフンに置き換えてパスを安全化する。"""
    normalized = re.sub(r"[^A-Za-z0-9_-]", "-", raw.strip())
    return normalized


def _object_path(session_id: str, content_type: str) -> str:
    ext = CONTENT_TYPE_EXTENSIONS.get(content_type, "dat")
    return f"audio/sessions/{session_id}/input.{ext}"


@app.post("/sessions", response_model=CreateSessionResponse)
def create_session(
    body: CreateSessionRequest, current_uid: str = Depends(verify_firebase_token)
):
    """新しい講義/会議セッションを作成して Firestore に保存する。"""
    logger.info("[/sessions] create_session called")
    try:
        raw_id = f"{body.mode}-{int(datetime.now(timezone.utc).timestamp() * 1000)}"
        session_id = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_id)

        doc_ref = db.collection(SESSIONS_COLLECTION).document(session_id)
        doc = {
            "title": body.title,
            "mode": body.mode,
            "userId": body.user_id or current_uid,
            "ownerId": current_uid,
            "createdAt": datetime.now(timezone.utc),
            "status": "created",
            "audioPath": None,
            "sttOperation": None,
            "transcriptPath": None,
            "contentType": None,
        }

        doc_ref.set(doc)

        return CreateSessionResponse(
            id=session_id,
            title=body.title,
            mode=body.mode,
            status="created",
            owner_id=current_uid,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[/sessions] create_session failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"internal error in /sessions: {exc}",
        ) from exc


@app.post("/upload-url", response_model=UploadUrlResponse)
def create_upload_url(payload: UploadUrlRequest, current_uid: str = Depends(verify_firebase_token)):
    if not AUDIO_BUCKET:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AUDIO_BUCKET が設定されていません。",
        )

    session_id = _normalized_session_id(payload.session_id)
    if not session_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="sessionId が空です。",
        )

    content_type = payload.content_type
    object_path = _object_path(session_id, content_type)
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=SIGNED_URL_EXPIRATION_SECONDS
    )

    try:
        bucket = storage_client.bucket(AUDIO_BUCKET)
        blob = bucket.blob(object_path)
        signed_url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=SIGNED_URL_EXPIRATION_SECONDS),
            method="PUT",
            content_type=content_type,
        )
    except Exception as exc:  # 署名URL生成に失敗した場合
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"署名付きURLの生成に失敗しました: {exc}",
        ) from exc

    doc_ref = db.collection(SESSIONS_COLLECTION).document(session_id)
    snapshot = doc_ref.get()
    if not snapshot.exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )
    session_data = snapshot.to_dict() or {}
    if session_data.get("ownerId") and session_data.get("ownerId") != current_uid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    try:
        doc_ref.update(
            {
                "audioPath": f"gs://{AUDIO_BUCKET}/{object_path}",
                "status": "uploaded_url_issued",
                "contentType": content_type,
            }
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Firestore の更新に失敗しました: {exc}",
        ) from exc

    return UploadUrlResponse(
        url=signed_url,
        upload_url=signed_url,
        object_path=object_path,
        content_type=content_type,
        expires_at=expires_at,
    )


@app.post("/sessions/{session_id}/start_transcribe", response_model=StartTranscribeResponse)
def start_transcribe(
    session_id: str, body: Optional[StartTranscribeRequest] = None, current_uid: str = Depends(verify_firebase_token)
):
    """Speech v2 の long-running を起動し、オペレーション名を Firestore に保存する。"""
    data = _get_session_doc_or_404(session_id)
    if data.get("ownerId") and data.get("ownerId") != current_uid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    doc_ref = data["_ref"]
    audio_path = data.get("audioPath") if data else None
    mode = data.get("mode") if data else None

    if not audio_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="audioPath が未設定です。先に /upload-url でURL発行とアップロードをしてください。",
        )
    if not audio_path.startswith("gs://"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="audioPath の形式が不正です。",
        )

    if mode == "lecture":
        config = cloud_speech.RecognitionConfig(
            auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
            language_codes=["ja-JP"],
            model="latest_long",
            features=cloud_speech.RecognitionFeatures(
                enable_automatic_punctuation=True,
            ),
        )
    elif mode == "meeting":
        config = cloud_speech.RecognitionConfig(
            auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
            language_codes=["ja-JP"],
            model="latest_long",
            features=cloud_speech.RecognitionFeatures(
                enable_automatic_punctuation=True,
                diarization_config=cloud_speech.SpeakerDiarizationConfig(
                    enable_speaker_diarization=True,
                    min_speaker_count=2,
                    max_speaker_count=6,
                ),
            ),
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported mode: {mode}",
        )

    logger.info(
        "start_transcribe: session_id=%s mode=%s audio_path=%s output_prefix=%s",
        session_id,
        mode,
        audio_path,
        f"gs://{AUDIO_BUCKET}/transcripts/{session_id}/",
    )

    output_prefix = f"gs://{AUDIO_BUCKET}/transcripts/{session_id}/"

    request = cloud_speech.BatchRecognizeRequest(
        recognizer=RECOGNIZER_ID,
        config=config,
        files=[
            cloud_speech.BatchRecognizeFileMetadata(
                uri=audio_path,
            )
        ],
        recognition_output_config=cloud_speech.RecognitionOutputConfig(
            gcs_output_config=cloud_speech.GcsOutputConfig(uri=output_prefix)
        ),
    )

    try:
        operation = speech_client.batch_recognize(request=request)
    except Exception as exc:
        logger.exception("start_transcribe: batch_recognize failed (session_id=%s)", session_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"文字起こしジョブの開始に失敗しました: {exc}",
        ) from exc

    raw_op_name = getattr(operation, "operation", None)
    if raw_op_name and hasattr(raw_op_name, "name"):
        op_name = raw_op_name.name
    else:
        op_name = getattr(getattr(operation, "_operation", None), "name", None)

    if not op_name:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="STT オペレーション名の取得に失敗しました",
        )

    logger.info("start_transcribe: operation started op_name=%s session_id=%s", op_name, session_id)

    try:
        doc_ref.update(
            {
                "sttOperation": op_name,
                "status": "transcribing",
                "transcriptOutputPrefix": output_prefix,
            }
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Firestore の更新に失敗しました: {exc}",
        ) from exc

    return StartTranscribeResponse(
        id=session_id,
        status="transcribing",
        operation_name=op_name,
    )


@app.get("/sessions/{session_id}", response_model=SessionDetailResponse)
def get_session(session_id: str, current_uid: str = Depends(verify_firebase_token)):
    """セッション詳細を Firestore から取得して返す。"""
    data = _get_session_doc_or_404(session_id)
    if data.get("ownerId") and data.get("ownerId") != current_uid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    return SessionDetailResponse(
        id=session_id,
        title=data.get("title"),
        mode=data.get("mode"),
        status=data.get("status"),
        user_id=data.get("userId"),
        owner_id=data.get("ownerId"),
        audio_path=data.get("audioPath"),
        stt_operation=data.get("sttOperation"),
        transcript_path=data.get("transcriptPath"),
        content_type=data.get("contentType"),
        created_at=data.get("createdAt"),
    )


@app.websocket("/ws/stream/{session_id}")
async def stream_recognize(websocket: WebSocket, session_id: str, mode: str = "lecture"):
    """WebSocket 経由で Speech v1 StreamingRecognize を中継する。"""
    await websocket.accept()
    logger.error(f"[/ws/stream] handler ENTER session_id={session_id}")
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4401)
        return
    try:
        decoded = firebase_auth.verify_id_token(token)
        uid = decoded.get("uid")
    except Exception:
        logger.info("[/ws/stream] token verification failed session_id=%s", session_id)
        await websocket.close(code=4401)
        return

    session_data = _get_session_doc_or_404(session_id)
    if session_data.get("ownerId") and session_data.get("ownerId") != uid:
        logger.info("[/ws/stream] forbidden session_id=%s uid=%s", session_id, uid)
        await websocket.close(code=4403)
        return
    logger.info("[/ws/stream] connected session_id=%s uid=%s", session_id, uid)
    await websocket.send_json({"event": "connected", "sessionId": session_id})

    audio_queue: "queue.Queue[bytes | None]" = queue.Queue(maxsize=MAX_AUDIO_QUEUE_SIZE)
    loop = asyncio.get_running_loop()
    recognize_task: Optional[asyncio.Task] = None
    streaming_config: Optional[speech.StreamingRecognitionConfig] = None

    try:
        while True:
            msg = await websocket.receive()

            if msg.get("type") == "websocket.disconnect":
                logger.info(f"[/ws/stream] websocket DISCONNECT session_id={session_id}")
                audio_queue.put(None)
                break

            if "bytes" in msg and msg["bytes"] is not None:
                chunk = msg["bytes"]
                if recognize_task is None:
                    logger.error(
                        f"[/ws/stream] audio chunk received before start; ignoring len={len(chunk)} "
                        f"session_id={session_id}"
                    )
                    continue
                logger.debug(
                    f"[/ws/stream] recv audio chunk len={len(chunk)} "
                    f"session_id={session_id}"
                )
                if audio_queue.full():
                    try:
                        audio_queue.get_nowait()
                    except queue.Empty:
                        pass
                try:
                    audio_queue.put_nowait(chunk)
                except queue.Full:
                    logger.warning(f"[/ws/stream] audio queue overflow session_id={session_id}")
            elif "text" in msg and msg["text"] is not None:
                data = None
                try:
                    data = json.loads(msg["text"])
                except Exception:
                    data = None
                event = data.get("event") if isinstance(data, dict) else None

                if event == "ping":
                    await websocket.send_json(
                        {
                            "event": "echo",
                            "sessionId": session_id,
                            "message": data.get("message"),
                        }
                    )
                    continue

                if event == "start":
                    if recognize_task is not None:
                        logger.error(
                            f"[/ws/stream] start received but recognizer already running session_id={session_id}"
                        )
                        continue
                    config_dict = (data.get("config") or {}) if isinstance(data, dict) else {}
                    streaming_config = build_streaming_config_from_client(config_dict)
                    logger.error(
                        f"[/ws/stream] start received; sample_rate={streaming_config.config.sample_rate_hertz} "
                        f"lang={streaming_config.config.language_code} session_id={session_id}"
                    )
                    recognize_task = loop.create_task(
                        run_streaming_recognize(
                            websocket=websocket,
                            session_id=session_id,
                            audio_queue=audio_queue,
                            streaming_config=streaming_config,
                        )
                    )
                    continue

                if event == "stop" or msg["text"] == "STOP":
                    logger.info(f"[/ws/stream] stop requested session_id={session_id}")
                    audio_queue.put(None)
                    break
                logger.error(
                    f"[/ws/stream] unknown text event={event} payload={data} "
                    f"session_id={session_id}"
                )

    except Exception:
        logger.exception(f"[/ws/stream] websocket handler error session_id={session_id}")
        audio_queue.put(None)
    finally:
        audio_queue.put(None)
        if recognize_task is not None:
            try:
                await recognize_task
            except Exception:
                logger.exception(f"[/ws/stream] recognize_task error session_id={session_id}")
        try:
            await websocket.close()
        except Exception:
            pass
        logger.error(f"[/ws/stream] handler EXIT session_id={session_id}")
        logger.info("[/ws/stream] disconnected session_id=%s", session_id)

def _load_transcript_from_gcs(output_prefix: str) -> tuple[Optional[str], Optional[str]]:
    """
    BatchRecognize の GCS 出力(JSON)から transcript を組み立てる。
    output_prefix: "gs://bucket/transcripts/<session_id>/" を想定。
    """
    parsed = urlparse(output_prefix)
    bucket_name = parsed.netloc
    prefix = parsed.path.lstrip("/")
    if not bucket_name or not prefix:
        return None, None
    blobs = list(storage_client.list_blobs(bucket_or_name=bucket_name, prefix=prefix))
    blobs = [b for b in blobs if b.name.endswith(".json")]
    if not blobs:
        return None, None

    blobs.sort(key=lambda b: b.name)
    blob = blobs[-1]
    content = blob.download_as_text()
    data = json.loads(content)

    pieces: List[str] = []
    for result in data.get("results", []):
        alternatives = result.get("alternatives", [])
        if not alternatives:
            continue
        text = alternatives[0].get("transcript", "")
        if text:
            pieces.append(text)

    full_text = "\n".join(pieces)
    gcs_path = f"gs://{bucket_name}/{blob.name}"
    return gcs_path, full_text


def _load_transcript_by_blob(path: str) -> Optional[str]:
    """パスを直接指定して transcript JSON を読み込む。失敗時は None。"""
    try:
        bucket_name, blob_path = _parse_gs_uri(path)
        blob = storage_client.bucket(bucket_name).blob(blob_path)
        if not blob.exists():
            return None
        content = blob.download_as_text()
        data = json.loads(content)
        pieces: List[str] = []
        for result in data.get("results", []):
            alts = result.get("alternatives", [])
            if alts:
                t = alts[0].get("transcript", "")
                if t:
                    pieces.append(t)
        if pieces:
            return "\n".join(pieces)
    except Exception:
        return None
    return None


def _parse_gs_uri(gs_uri: str) -> tuple[str, str]:
    """gs://bucket/path を (bucket, path) に分解する。"""
    if not gs_uri.startswith("gs://"):
        raise ValueError("Invalid GCS URI")
    without_scheme = gs_uri[len("gs://") :]
    bucket, _, path = without_scheme.partition("/")
    return bucket, path


def _refresh_transcript_core(session_id: str) -> RefreshTranscriptResponse:
    """Speech v2 のオペレーション結果を確認し、完了していれば Firestore/GCS に保存する。"""
    doc_ref = db.collection(SESSIONS_COLLECTION).document(session_id)
    snapshot = doc_ref.get()
    if not snapshot.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    data = snapshot.to_dict() or {}
    # すでに Firestore に transcriptText が入っていればそれを優先して返す
    existing_text = _get_transcript_text(data)
    if existing_text:
        return RefreshTranscriptResponse(
            id=session_id,
            status=data.get("status", "transcribed"),
            transcript_text=existing_text,
        )

    op_name = data.get("sttOperation")
    if not op_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No STT operation recorded")

    try:
        operation_pb: operations_pb2.Operation = speech_client.transport.operations_client.get_operation(
            name=op_name
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"STT オペレーションの取得に失敗しました: {exc}",
        ) from exc

    if not operation_pb.done:
        return RefreshTranscriptResponse(id=session_id, status="transcribing", transcript_text=None)

    # 1) まず Operation レスポンスをデコードして transcript を拾う
    full_text = ""
    try:
        op = Operation(
            operation_pb,
            speech_client.transport.operations_client,
            cloud_speech.BatchRecognizeResponse,
            metadata_type=cloud_speech.BatchRecognizeMetadata,
        )
        resp: cloud_speech.BatchRecognizeResponse = op.result()
        pieces: List[str] = []
        for res in resp.results:
            if res.alternatives:
                pieces.append(res.alternatives[0].transcript)
            if getattr(res, "channel_transcripts", None):
                for ch in res.channel_transcripts:
                    if ch.alternatives:
                        pieces.append(ch.alternatives[0].transcript)
        full_text = "\n".join([p for p in pieces if p])
    except Exception:
        full_text = ""

    output_prefix = data.get("transcriptOutputPrefix") or f"gs://{AUDIO_BUCKET}/transcripts/{session_id}/"
    gcs_path, gcs_text = _load_transcript_from_gcs(output_prefix)

    # GCS の内容が取れたらそれを優先
    if gcs_text:
        full_text = gcs_text
    elif op_name:
        # ファイル名にオペレーションIDが含まれるパターンを直接読む
        op_id = op_name.split("/")[-1]
        candidate_path = f"gs://{AUDIO_BUCKET}/transcripts/{session_id}/input_transcript_{op_id}.json"
        fallback_text = _load_transcript_by_blob(candidate_path)
        if fallback_text:
            full_text = fallback_text
            gcs_path = candidate_path
        else:
            # input_transcript_ プレフィックスで片っ端から探す
            list_prefix = f"transcripts/{session_id}/input_transcript_"
            bucket = storage_client.bucket(AUDIO_BUCKET)
            blobs = list(storage_client.list_blobs(bucket, prefix=list_prefix))
            blobs = [b for b in blobs if b.name.endswith(".json")]
            if blobs:
                blobs.sort(key=lambda b: b.name)
                last = blobs[-1]
                text = _load_transcript_by_blob(f"gs://{AUDIO_BUCKET}/{last.name}")
                if text:
                    full_text = text
                    gcs_path = f"gs://{AUDIO_BUCKET}/{last.name}"
                else:
                    # それでも取れなければ生の JSON を transcriptText に入れる
                    try:
                        raw = storage_client.bucket(AUDIO_BUCKET).blob(last.name).download_as_text()
                        if raw:
                            full_text = raw
                            gcs_path = f"gs://{AUDIO_BUCKET}/{last.name}"
                    except Exception:
                        pass

    if not full_text:
        # まだ結果ファイルが見えないケース
        return RefreshTranscriptResponse(id=session_id, status="transcribing", transcript_text=None)

    transcript_blob_path = f"transcripts/{session_id}.stt.json"
    try:
        bucket = storage_client.bucket(AUDIO_BUCKET)
        json_blob = bucket.blob(transcript_blob_path)
        json_blob.upload_from_string(
            json.dumps(
                {"transcript": full_text},
                ensure_ascii=False,
            ),
            content_type="application/json",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"GCS への transcript 保存に失敗しました: {exc}",
        ) from exc

    try:
        doc_ref.update(
            {
                "status": "transcribed",
                "transcriptText": full_text,
                "transcriptPath": gcs_path
                or f"gs://{AUDIO_BUCKET}/{transcript_blob_path}",
            }
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Firestore の更新に失敗しました: {exc}",
        ) from exc

    return RefreshTranscriptResponse(
        id=session_id,
        status="transcribed",
        transcript_text=full_text,
    )


@app.post("/sessions/{session_id}/refresh_transcript", response_model=RefreshTranscriptResponse)
def refresh_transcript(session_id: str, current_uid: str = Depends(verify_firebase_token)):
    snap = db.collection(SESSIONS_COLLECTION).document(session_id).get()
    if not snap.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    data = snap.to_dict() or {}
    owner = data.get("ownerId")
    if owner and owner != current_uid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    resp = _refresh_transcript_core(session_id)
    return resp.model_dump(by_alias=True)


@app.get("/tasks/check-transcripts")
def check_transcripts():
    """transcribing セッションを一括チェックするタスク用エンドポイント。"""
    docs = db.collection(SESSIONS_COLLECTION).where("status", "==", "transcribing").stream()
    checked = 0
    updated = 0

    for snap in docs:
        sid = snap.id
        try:
            res = _refresh_transcript_core(sid)
            checked += 1
            if res.status == "transcribed":
                updated += 1
        except HTTPException as exc:
            print(f"[check-transcripts] session={sid} http_error={exc.status_code}:{exc.detail}")
        except Exception as exc:  # noqa: BLE001
            print(f"[check-transcripts] session={sid} unexpected_error={exc}")

    return {"checked": checked, "updated": updated}


@app.post("/sessions/{session_id}/summarize", response_model=SummarizeResponse)
def summarize_session(session_id: str, current_uid: str = Depends(verify_firebase_token)):
    """transcriptText を元に Gemini で要約を生成する。"""
    try:
        doc_ref = db.collection(SESSIONS_COLLECTION).document(session_id)
        snapshot = doc_ref.get()
        if not snapshot.exists:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

        data = snapshot.to_dict() or {}
        logger.info(
            "[summarize] session_id=%s exists=%s keys=%s",
            session_id,
            snapshot.exists,
            sorted(list(data.keys())),
        )
        if data.get("ownerId") and data.get("ownerId") != current_uid:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        transcript_text = _get_transcript_text(data)
        logger.info(
            "[summarize] transcript_len=%s transcript_path=%s",
            len(transcript_text) if transcript_text else 0,
            data.get("transcriptPath"),
        )
        if not transcript_text:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No transcriptText available. Run refresh_transcript first.",
            )

        prompt = f"""
あなたは大学の講義や会議の内容を学生向けに分かりやすく要約するアシスタントです。

以下は1つのセッションの文字起こしです。内容を整理して、JSON形式で出力してください。

--- Transcript ---
{transcript_text}
-------------------

以下のJSON形式で出力してください（日本語）:

{{
  "overview": "講義全体の概要を5〜8行で",
  "points": [
    "重要なポイント1",
    "重要なポイント2",
    "重要なポイント3"
  ],
  "keywords": [
    "キーワード1",
    "キーワード2",
    "キーワード3"
  ]
}}
"""

        resp = gemini_model.generate_content(
            prompt, generation_config={"temperature": 0.4, "max_output_tokens": 1024}
        )

        text_output = getattr(resp, "text", None)
        if not text_output:
            raise RuntimeError("LLM returned empty response")

        try:
            summary = json.loads(text_output)
        except Exception:
            logger.exception("LLM response parse failed: %s", text_output)
            raise

        doc_ref.update({"summary": summary})

        return SummarizeResponse(id=session_id, status="summarized", summary=summary)
    except HTTPException:
        raise
    except GcpNotFound as exc:
        logger.exception(
            "Gemini model not found for summarize session_id=%s model=%s region=%s",
            session_id,
            GEMINI_MODEL_NAME,
            VERTEX_REGION,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"summarizer_error:GeminiModelNotFound(model={GEMINI_MODEL_NAME},region={VERTEX_REGION})",
        ) from exc
    except Exception as exc:
        logger.exception("summarize_session failed for %s", session_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"summarizer_error:{exc.__class__.__name__}",
        ) from exc


@app.post("/sessions/{session_id}/quiz", response_model=QuizResponse)
def generate_quiz(session_id: str, count: int = 5, current_uid: str = Depends(verify_firebase_token)):
    """transcriptText から 4択クイズを生成する。"""
    doc_ref = db.collection(SESSIONS_COLLECTION).document(session_id)
    snapshot = doc_ref.get()
    if not snapshot.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    data = snapshot.to_dict() or {}
    logger.info(
        "[quiz] session_id=%s exists=%s keys=%s",
        session_id,
        snapshot.exists,
        sorted(list(data.keys())),
    )
    if data.get("ownerId") and data.get("ownerId") != current_uid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    transcript_text = _get_transcript_text(data)
    logger.info(
        "[quiz] transcript_len=%s summary_keys=%s",
        len(transcript_text) if transcript_text else 0,
        list(data.get("summary", {}).keys()) if isinstance(data.get("summary"), dict) else None,
    )
    if not transcript_text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No transcriptText available",
        )

    prompt = f"""
あなたは大学の講義内容からテスト問題を作るアシスタントです。
以下の文字起こしから重要なポイントを選び、4択問題を{count}問作成してください。

--- Transcript ---
{transcript_text}
-------------------

以下のJSON配列形式で出力してください:

[
  {{
    "id": "q1",
    "question": "問題文",
    "choices": [
      {{ "text": "選択肢A" }},
      {{ "text": "選択肢B" }},
      {{ "text": "選択肢C" }},
      {{ "text": "選択肢D" }}
    ],
    "correct_index": 0,
    "explanation": "なぜこの答えが正しいのかの解説"
  }},
  ...
]
"""

    try:
        resp = gemini_model.generate_content(prompt)
    except GcpNotFound as exc:
        logger.exception(
            "Gemini model not found for quiz session_id=%s model=%s region=%s",
            session_id,
            GEMINI_MODEL_NAME,
            VERTEX_REGION,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"クイズ生成に失敗しました: Gemini model not found (model={GEMINI_MODEL_NAME}, region={VERTEX_REGION})",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"クイズ生成に失敗しました: {exc}",
        ) from exc

    text_output = getattr(resp, "text", None)
    if not text_output:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="LLM からテキスト応答がありません",
        )

    try:
        questions_data = json.loads(text_output)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"LLM 応答の JSON 解析に失敗しました: {exc}",
        ) from exc

    try:
        questions = [QuizQuestion(**q) for q in questions_data]
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"クイズデータの整形に失敗しました: {exc}",
        ) from exc

    try:
        quiz_ref = db.collection("quizzes").document(session_id)
        quiz_ref.set(
            {
                "sessionId": session_id,
                "count": count,
                "questions": [q.dict(by_alias=True) for q in questions],
            }
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Firestore の更新に失敗しました: {exc}",
        ) from exc

    return QuizResponse(session_id=session_id, count=count, questions=questions)


@app.post("/sessions/{session_id}/qa", response_model=QAResponse)
def qa_session(session_id: str, body: QARequest, current_uid: str = Depends(verify_firebase_token)):
    """transcriptText を使った QA 回答を生成する。"""
    doc_ref = db.collection(SESSIONS_COLLECTION).document(session_id)
    snapshot = doc_ref.get()
    if not snapshot.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    data = snapshot.to_dict() or {}
    logger.info(
        "[qa] session_id=%s exists=%s keys=%s",
        session_id,
        snapshot.exists,
        sorted(list(data.keys())),
    )
    if data.get("ownerId") and data.get("ownerId") != current_uid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    transcript_text = _get_transcript_text(data)
    logger.info(
        "[qa] transcript_len=%s summary_keys=%s",
        len(transcript_text) if transcript_text else 0,
        list(data.get("summary", {}).keys()) if isinstance(data.get("summary"), dict) else None,
    )
    if not transcript_text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No transcriptText available",
        )

    prompt = f"""
以下は大学の講義または会議の文字起こしです。学生/参加者からの質問に日本語で答えてください。

--- Transcript ---
{transcript_text}
-------------------

質問: {body.question}

ルール:
- 初学者にも分かるように、専門用語は簡単に説明してください。
- 箇条書きで2〜4個のポイントに分けて答えてください。
"""

    try:
        resp = gemini_model.generate_content(prompt)
    except GcpNotFound as exc:
        logger.exception(
            "Gemini model not found for qa session_id=%s model=%s region=%s",
            session_id,
            GEMINI_MODEL_NAME,
            VERTEX_REGION,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"QA 生成に失敗しました: Gemini model not found (model={GEMINI_MODEL_NAME}, region={VERTEX_REGION})",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"QA 生成に失敗しました: {exc}",
        ) from exc

    text_output = getattr(resp, "text", None)
    if not text_output:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="LLM からテキスト応答がありません",
        )

    return QAResponse(id=session_id, question=body.question, answer=text_output.strip())


@app.get("/sessions/{session_id}/audio_url")
def get_audio_url(session_id: str, current_uid: str = Depends(verify_firebase_token)):
    """録音済み音声の署名付きURLを返す。"""
    data = _get_session_doc_or_404(session_id)
    if data.get("ownerId") and data.get("ownerId") != current_uid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    audio_path = data.get("audioPath")
    if not audio_path or not audio_path.startswith("gs://"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="audioPath is not set")
    bucket_name, blob_name = _parse_gs_uri(audio_path)
    try:
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(hours=1),
            method="GET",
            response_type="audio/wav",
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to generate signed url: {exc}",
        ) from exc
    return {"url": url}
