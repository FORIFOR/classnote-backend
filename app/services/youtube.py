
import os
import subprocess
import logging
from google.cloud import storage
from google.cloud.speech_v2 import SpeechClient
from google.cloud.speech_v2.types import cloud_speech
from google.api_core.client_options import ClientOptions

logger = logging.getLogger("app.services.youtube")

PROJECT_ID = os.environ.get("GCP_PROJECT", "classnote-x-dev")
REGION = "asia-northeast1"
BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME", "classnote-x-media")

def _download_and_convert(url: str, session_id: str) -> str:
    """
    Download audio from YouTube via yt-dlp, convert to 16kHz mono FLAC via ffmpeg.
    Returns the path to the local FLAC file.
    """
    tmp_base = f"/tmp/{session_id}"
    tmp_in = f"{tmp_base}_in" # yt-dlp adds extension
    tmp_out = f"{tmp_base}.flac"

    # 1. Download audio (m4a/best)
    logger.info(f"Downloading from YouTube: {url}")
    # Note: yt-dlp appends extension to output template if not specified carefully. 
    # We use -o to specify a predictable prefix/name. 
    # However, if we don't know the extension, it's tricky.
    # We force m4a or similar.
    try:
        # Capture output for debugging
        subprocess.run([
            "yt-dlp",
            # Enable Node.js
            "--js-runtimes", "nodejs", 
            "--no-playlist",
            "-f", "bestaudio[ext=m4a]/bestaudio/best",
            "-o", tmp_in + ".%(ext)s",
            url
        ], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        logger.error(f"yt-dlp failed: {e}")
        logger.error(f"yt-dlp stderr: {e.stderr}")
        logger.error(f"yt-dlp stdout: {e.stdout}")
        raise ValueError(f"Failed to download video (yt-dlp error): {e.stderr}")

    # Find the downloaded file
    # yt-dlp might download .m4a, .webm, etc.
    if os.path.exists(tmp_in + ".m4a"):
        downloaded_path = tmp_in + ".m4a"
    elif os.path.exists(tmp_in + ".webm"):
        downloaded_path = tmp_in + ".webm"
    else:
        # Fallback search
        found = [f for f in os.listdir("/tmp") if f.startswith(f"{session_id}_in")]
        if not found:
            raise ValueError("Downloaded file not found")
        downloaded_path = f"/tmp/{found[0]}"

    # 2. Normalize to 16kHz mono FLAC
    logger.info(f"Converting to FLAC: {downloaded_path} -> {tmp_out}")
    try:
        subprocess.check_call([
            "ffmpeg", "-y",
            "-i", downloaded_path,
            "-ac", "1",
            "-ar", "16000",
            tmp_out
        ])
    except subprocess.CalledProcessError as e:
        logger.error(f"ffmpeg failed: {e}")
        raise ValueError("Failed to convert audio (ffmpeg error)")
    finally:
        # Cleanup input
        if os.path.exists(downloaded_path):
            os.remove(downloaded_path)

    return tmp_out

def _upload_to_gcs(local_path: str, destination_blob_name: str) -> str:
    """Uploads file to GCS and returns gs:// URI."""
    storage_client = storage.Client()
    bucket = storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(destination_blob_name)
    
    logger.info(f"Uploading to gs://{BUCKET_NAME}/{destination_blob_name}")
    blob.upload_from_filename(local_path, content_type="audio/flac")
    return f"gs://{BUCKET_NAME}/{destination_blob_name}"

def _transcribe_chirp_3(gcs_uri: str, language_code: str = "ja-JP") -> str:
    """
    Transcribes audio using Google Cloud Speech-to-Text v2 (Chirp 3) BatchRecognize.
    This blocks until completion (up to timeout).
    """
    logger.info(f"Starting Chirp 3 transcription for {gcs_uri}")
    
    client_options = ClientOptions(api_endpoint=f"{REGION}-speech.googleapis.com")
    client = SpeechClient(client_options=client_options)

    config = cloud_speech.RecognitionConfig(
        auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
        language_codes=[language_code],
        model="chirp_3",
        features=cloud_speech.RecognitionFeatures(
            enable_automatic_punctuation=True,
            enable_word_time_offsets=True,
        ),
    )

    request = cloud_speech.BatchRecognizeRequest(
        recognizer=f"projects/{PROJECT_ID}/locations/{REGION}/recognizers/_",
        config=config,
        files=[cloud_speech.BatchRecognizeFileMetadata(uri=gcs_uri)],
        recognition_output_config=cloud_speech.RecognitionOutputConfig(
            inline_response_config=cloud_speech.InlineOutputConfig()
        ),
    )

    operation = client.batch_recognize(request=request)
    
    # Wait for completion (long timeout for YouTube videos)
    # Cloud Run timeout is usually 60m. 
    logger.info("Waiting for BatchRecognize operation to complete...")
    response = operation.result(timeout=3500) # Slightly less than 3600

    # Process results
    if not response.results or gcs_uri not in response.results:
        logger.error(f"No results for {gcs_uri}")
        raise ValueError("Transcription returned no results")
    
    file_result = response.results[gcs_uri]
    if file_result.error and file_result.error.message:
        raise ValueError(f"STT Error: {file_result.error.message}")

    # Concatenate transcript (Chirp 3 Batch returns alternatives)
    # The structure is: response.results[uri].transcript.results[list].alternatives[0].transcript
    transcript_segments = file_result.transcript.results
    full_text = "\n".join(
        (seg.alternatives[0].transcript if seg.alternatives else "") 
        for seg in transcript_segments
    )
    
    return full_text

def process_youtube_import(session_id: str, url: str, language: str = "ja-JP") -> str:
    """
    Orchestrates the YouTube import flow.
    Returns the full transcript text.
    Processing happens in /tmp.
    """
    local_audio = None
    try:
        # 1. Download & Convert
        local_audio = _download_and_convert(url, session_id)
        
        # 2. Upload to GCS
        gcs_path = f"imports/{session_id}.flac"
        gcs_uri = _upload_to_gcs(local_audio, gcs_path)
        
        # 3. Transcribe
        # Ensure language is properly formatted (e.g. "ja" -> "ja-JP")
        # STT v2 expects BCP-47. "ja" is usually acceptable but "ja-JP" is safer for Chirp.
        # Simple mapping for common short codes if needed, or trust input.
        lang_code = language if "-" in language else f"{language}-{language.upper()}" if len(language)==2 else language
        if language == "ja": lang_code = "ja-JP"
        if language == "en": lang_code = "en-US"
        
        transcript = _transcribe_chirp_3(gcs_uri, language_code=lang_code)
        return transcript

    finally:
        # Cleanup
        if local_audio and os.path.exists(local_audio):
            os.remove(local_audio)
