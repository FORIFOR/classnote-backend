import os
import logging
import struct
import math
from typing import AsyncGenerator
from google.cloud.speech_v2 import SpeechAsyncClient
from google.cloud.speech_v2.types import cloud_speech as cs
from google.api_core.client_options import ClientOptions

logger = logging.getLogger("app.streaming_stt_v2")

# Environment Config
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
REGION = os.environ.get("TASKS_LOCATION", "asia-northeast1")
RECOGNIZER_ID = os.environ.get("STT_RECOGNIZER_ID", "classnote-general-v2")

def compute_audio_stats(pcm_bytes: bytes) -> dict:
    """Compute audio statistics from LINEAR16 PCM bytes."""
    if len(pcm_bytes) < 2:
        return {"samples": 0, "max_abs": 0, "rms": 0.0, "rms_db": -100.0}
    
    num_samples = len(pcm_bytes) // 2
    samples = struct.unpack(f"<{num_samples}h", pcm_bytes[:num_samples * 2])
    
    if not samples:
        return {"samples": 0, "max_abs": 0, "rms": 0.0, "rms_db": -100.0}
    
    max_abs = max(abs(s) for s in samples)
    sum_sq = sum(s * s for s in samples)
    rms = math.sqrt(sum_sq / num_samples)
    # dB relative to full scale (32767)
    rms_db = 20 * math.log10(rms / 32767.0) if rms > 0 else -100.0
    
    return {
        "samples": num_samples,
        "max_abs": max_abs,
        "rms": round(rms, 2),
        "rms_db": round(rms_db, 1)
    }

class StreamingSTTV2:
    def __init__(self):
        # Initialize Speech V2 Async Client
        self.api_endpoint = f"{REGION}-speech.googleapis.com"
        self.client_options = ClientOptions(api_endpoint=self.api_endpoint)
        self.client = SpeechAsyncClient(client_options=self.client_options)
        
        self.recognizer_path = f"projects/{PROJECT_ID}/locations/{REGION}/recognizers/{RECOGNIZER_ID}"

    def build_config(self, sample_rate: int = 16000, language_code: str = "ja-JP") -> cs.StreamingRecognitionConfig:
        """
        Builds the StreamingRecognitionConfig with ExplicitDecodingConfig (Required for V2 Streaming).
        """
        # Explicit Decoding Config (Raw PCM, LINEAR16)
        explicit_decoding = cs.ExplicitDecodingConfig(
            encoding=cs.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=sample_rate,
            audio_channel_count=1,
        )

        # Recognition Config
        recognition_config = cs.RecognitionConfig(
            explicit_decoding_config=explicit_decoding,
            language_codes=[language_code],
            model="long", 
            features=cs.RecognitionFeatures(
                enable_automatic_punctuation=True,
            ),
        )

        # Streaming Features
        streaming_features = cs.StreamingRecognitionFeatures(
            interim_results=True,
            enable_voice_activity_events=True,
        )

        return cs.StreamingRecognitionConfig(
            config=recognition_config,
            streaming_features=streaming_features,
        )
        
    def create_silence_chunk(self, duration_ms: int = 100, sample_rate: int = 16000) -> bytes:
        """Create a silent LINEAR16 PCM chunk."""
        num_samples = int(sample_rate * (duration_ms / 1000.0))
        return b'\x00' * (num_samples * 2)

    async def recognize_stream(
        self, 
        audio_generator: AsyncGenerator[bytes, None], 
        sample_rate: int = 16000, 
        language_code: str = "ja-JP"
    ) -> AsyncGenerator[dict, None]:
        """
        Streams audio to Google STT V2 and yields events.
        Bridge from AsyncGenerator[bytes] -> Stream of Requests -> Stream of Responses.
        """
        streaming_config = self.build_config(sample_rate, language_code)
        
        async def request_generator():
            # 1. Send Config
            yield cs.StreamingRecognizeRequest(
                recognizer=self.recognizer_path,
                streaming_config=streaming_config,
            )
            logger.info("[StreamingSTTv2] Sent V2 Config, starting stream")
            
            # 2. Send Audio
            async for chunk in audio_generator:
                yield cs.StreamingRecognizeRequest(audio=chunk)

        # Call Streaming Recognize
        try:
            responses = await self.client.streaming_recognize(requests=request_generator())
            
            async for response in responses:
                # Handle Results
                if response.results:
                    for result in response.results:
                        if not result.alternatives:
                            continue
                        alt = result.alternatives[0]
                        yield {
                            "is_final": result.is_final,
                            "transcript": alt.transcript,
                            "confidence": alt.confidence if hasattr(alt, "confidence") else 0.0,
                            "stability": result.stability if hasattr(result, "stability") else 0.0
                        }
                
                # Handle VAD Events
                if response.speech_event_type:
                    # SpeechEventType.SPEECH_EVENT_TYPE_UNSPECIFIED = 0
                    # SpeechEventType.SPEECH_ACTIVITY_BEGIN = 1
                    # SpeechEventType.SPEECH_ACTIVITY_END = 2
                    event_map = {1: "START", 2: "END"}
                    evt = event_map.get(response.speech_event_type)
                    if evt:
                        yield {"vad_event": evt}

        except Exception as e:
            logger.error(f"[StreamingSTTv2] Error: {e}")
            raise e
