import os
import logging
import json
import re
from typing import Optional, Tuple, List, Dict, Any
from google.cloud import speech_v2
from google.cloud.speech_v2.types import cloud_speech
from google.cloud import storage
from google.api_core.client_options import ClientOptions
from google.api_core import exceptions

logger = logging.getLogger(__name__)

# Config
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
REGION = os.environ.get("TASKS_LOCATION", "asia-northeast1") # Default region
from app.firebase import storage_client, AUDIO_BUCKET_NAME

# Initialize Clients
try:
    storage_client = storage.Client()
    # Speech V2 requires regional endpoint
    api_endpoint = f"{REGION}-speech.googleapis.com"
    client_options = ClientOptions(api_endpoint=api_endpoint)
    speech_client = speech_v2.SpeechClient(client_options=client_options)
except Exception as e:
    logger.warning(f"Google Cloud Clients failed to init (Local mode?): {e}")
    storage_client = None
    speech_client = None

def _get_or_create_recognizer(recognizer_id: str = "classnote-general-v2") -> str:
    """
    Get or Create a V2 Recognizer resource.
    """
    parent = f"projects/{PROJECT_ID}/locations/{REGION}"
    recognizer_path = f"{parent}/recognizers/{recognizer_id}"
    
    try:
        speech_client.get_recognizer(name=recognizer_path)
        return recognizer_path
    except exceptions.NotFound:
        logger.info(f"Recognizer {recognizer_id} not found, creating...")
        
    # Create - Note: auto_decoding_config removed to avoid SDK version issues
    # V2 reliably auto-detects WAV (Linear16) from headers
    recognizer_request = cloud_speech.CreateRecognizerRequest(
        parent=parent,
        recognizer_id=recognizer_id,
        recognizer=cloud_speech.Recognizer(
            default_recognition_config=cloud_speech.RecognitionConfig(
                auto_decoding_config=cloud_speech.AutoDecodingConfig(),  # [FIX] Required for V2
                language_codes=["ja-JP"],
                model="long",  # V2 model name (long, short, telephony, medical, etc.)
                features=cloud_speech.RecognitionFeatures(
                    enable_automatic_punctuation=True,
                    enable_word_time_offsets=True,
                ),
            )
        )
    )
    operation = speech_client.create_recognizer(request=recognizer_request)
    return operation.result().name

import subprocess
import tempfile
import shutil

def convert_to_wav(local_input_path: str, output_path: Optional[str] = None) -> str:
    """
    Convert audio file to WAV (16kHz, mono, 16-bit) using ffmpeg.
    Required for stable Google Cloud Speech-to-Text V2 Batch results.
    """
    if output_path is None:
        output_path = local_input_path + ".wav"
        
    # ffmpeg -i input -ar 16000 -ac 1 -c:a pcm_s16le output.wav
    cmd = [
        "ffmpeg", "-y", "-i", local_input_path,
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
        output_path
    ]
    
    logger.info(f"Converting audio: {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        logger.error(f"ffmpeg failed: {e.stderr.decode()}")
        raise RuntimeError(f"Audio conversion failed: {e.stderr.decode()}")
        
    return output_path

def _parse_time_to_sec(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if s.endswith("s"):
            s = s[:-1]
        try:
            return float(s)
        except ValueError:
            return None
    if isinstance(value, dict):
        seconds = value.get("seconds")
        nanos = value.get("nanos") or 0
        if seconds is None:
            return None
        try:
            return float(seconds) + float(nanos) / 1_000_000_000
        except (TypeError, ValueError):
            return None
    return None


def _extract_segments_from_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    segments: List[Dict[str, Any]] = []
    cursor_end = 0.0
    for result in results:
        alternatives = result.get("alternatives") or []
        if not alternatives:
            continue
        alt = alternatives[0] or {}
        text = (alt.get("transcript") or "").strip()
        if not text:
            continue

        words = alt.get("words") or result.get("words") or []
        start_sec = end_sec = None
        if words:
            first = words[0]
            last = words[-1]
            start_sec = _parse_time_to_sec(first.get("startTime") or first.get("start_time"))
            end_sec = _parse_time_to_sec(last.get("endTime") or last.get("end_time"))

        if start_sec is None or end_sec is None:
            result_end = _parse_time_to_sec(
                result.get("resultEndOffset") or result.get("resultEndTime")
            )
            if result_end is not None:
                start_sec = cursor_end
                end_sec = result_end

        if start_sec is None or end_sec is None:
            continue
        if end_sec < start_sec:
            start_sec, end_sec = end_sec, start_sec

        segments.append({
            "startSec": float(start_sec),
            "endSec": float(end_sec),
            "text": text,
        })
        cursor_end = max(cursor_end, end_sec)
    return segments


def _read_transcript_outputs(output_prefix: str) -> Tuple[str, List[Dict[str, Any]]]:
    blobs = list(storage_client.list_blobs(AUDIO_BUCKET_NAME, prefix=output_prefix))
    transcript_parts: List[str] = []
    segments: List[Dict[str, Any]] = []
    found_json = False

    for blob in sorted(blobs, key=lambda b: b.name):
        if not blob.name.endswith(".json"):
            continue
        found_json = True
        json_bytes = blob.download_as_bytes()
        data = json.loads(json_bytes)

        results = data.get("results", [])
        for result in results:
            alts = result.get("alternatives", [])
            if alts:
                transcript_parts.append(alts[0].get("transcript", ""))
        segments.extend(_extract_segments_from_results(results))

    if not found_json:
        raise RuntimeError("STT completed but no output JSON found in GCS.")

    transcript_text = "".join(transcript_parts)
    return transcript_text, segments


def transcribe_audio_google_with_segments(
    gcs_uri: str,
    language_code: str = "ja-JP",
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Transcribes audio using Google Cloud Speech-to-Text V2 BatchRecognize.
    Now supports M4A/AAC by converting to WAV (16kHz mono) first.
    
    Args:
        gcs_uri: gs://bucket/path/to/audio.m4a
        language_code: "ja-JP"
        
    Returns:
        Full transcript string and timestamped segments.
    """
    if not storage_client or not speech_client:
        raise RuntimeError("Google Cloud clients not initialized")

    if not gcs_uri.startswith("gs://"):
        raise ValueError(f"Invalid GCS URI: {gcs_uri}")

    import uuid
    job_uuid = uuid.uuid4().hex
    
    # 1. Download Input Audio
    logger.info(f"Downloading original audio from {gcs_uri}...")
    
    input_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".m4a")
    input_local_path = input_tmp.name
    input_tmp.close()
    
    converted_local_path = None
    converted_gcs_uri = None
    
    try:
        # Parse bucket/path
        parts = gcs_uri[5:].split("/", 1)
        bucket_name = parts[0]
        blob_name = parts[1]
        
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        if not blob.exists():
             raise FileNotFoundError(f"GCS File not found: {gcs_uri}")
             
        blob.download_to_filename(input_local_path)
        
        # 2. Check size (avoid empty files causing obscure errors)
        if os.path.getsize(input_local_path) < 100:
             raise ValueError("Audio file is too small or empty.")

        # 3. Convert to WAV
        logger.info("Converting to WAV (16kHz mono)...")
        converted_local_path = input_local_path + ".wav"
        convert_to_wav(input_local_path, converted_local_path)
        
        # 4. Upload Converted Audio to Temporary GCS
        converted_blob_name = f"tmp_conversion/{job_uuid}.wav"
        converted_gcs_uri = f"gs://{AUDIO_BUCKET_NAME}/{converted_blob_name}"
        
        logger.info(f"Uploading converted WAV to {converted_gcs_uri}...")
        res_bucket = storage_client.bucket(AUDIO_BUCKET_NAME)
        res_blob = res_bucket.blob(converted_blob_name)
        res_blob.upload_from_filename(converted_local_path)
        
        # 5. Call STT with WAV
        recognizer_name = _get_or_create_recognizer()
        output_prefix = f"transcripts/{job_uuid}/"
        output_uri = f"gs://{AUDIO_BUCKET_NAME}/{output_prefix}"

        logger.info(f"Starting STT Batch V2 for {converted_gcs_uri} -> {output_uri}")

        # Explicit Decoding Config NO LONGER NEEDED for WAV (Linear16 is auto-detected well)
        # But we can still be explicit if we want. V2 auto-detects WAV headers reliably.
        
        files = [cloud_speech.BatchRecognizeFileMetadata(uri=converted_gcs_uri)]
        
        config = cloud_speech.RecognitionConfig(
            language_codes=[language_code],
            model="long",
            features=cloud_speech.RecognitionFeatures(
               enable_automatic_punctuation=True,
               enable_word_time_offsets=True,
            )
        )

        request = cloud_speech.BatchRecognizeRequest(
            recognizer=recognizer_name,
            config=config,
            files=files,
            recognition_output_config=cloud_speech.RecognitionOutputConfig(
                gcs_output_config=cloud_speech.GcsOutputConfig(uri=output_uri)
            )
        )

        operation = speech_client.batch_recognize(request=request)
        logger.info(f"STT Operation started: {operation.operation.name}")
        
        # Wait for completion (Blocking)
        result = operation.result(timeout=1800)
        
        # Check errors
        for file_res in result.results.values():
            if file_res.error and file_res.error.code != 0:
                 raise RuntimeError(f"STT processing failed: {file_res.error.message}")

        logger.info("STT Operation completed, fetching results from GCS")

        transcript_text, segments = _read_transcript_outputs(output_prefix)
        return transcript_text, segments

    finally:
        # Cleanup
        if os.path.exists(input_local_path):
            os.unlink(input_local_path)
        if converted_local_path and os.path.exists(converted_local_path):
            os.unlink(converted_local_path)
            
        # Optional: Cleanup GCS tmp file (async or fire-and-forget ideal, but sync here is safer for cost)
        if converted_gcs_uri and storage_client:
            try:
                parts = converted_gcs_uri[5:].split("/", 1)
                b_name = parts[0]
                blob_n = parts[1]
                storage_client.bucket(b_name).blob(blob_n).delete()
                logger.info("Cleaned up temporary GCS wav file")
            except Exception as e:
                logger.warning(f"Failed to cleanup GCS tmp file: {e}")


def transcribe_audio_google(gcs_uri: str, language_code: str = "ja-JP") -> str:
    transcript_text, _segments = transcribe_audio_google_with_segments(
        gcs_uri, language_code=language_code
    )
    return transcript_text

