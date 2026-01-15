import asyncio
import struct
import math
import time
import logging
from typing import AsyncGenerator, Optional
from google.cloud import speech

logger = logging.getLogger("app.streaming_stt")


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


class StreamingSTT:
    def __init__(self, language_code: str = "ja-JP", sample_rate: int = 16000, enable_diarization: bool = False, di_speaker_count: int = 2):
        self.language_code = language_code
        self.sample_rate = sample_rate
        self.enable_diarization = enable_diarization
        self.di_speaker_count = di_speaker_count
        self.client = speech.SpeechAsyncClient()

    def create_silence_chunk(self, duration_ms: int = 100) -> bytes:
        """Create a silent LINEAR16 PCM chunk."""
        num_samples = int(self.sample_rate * (duration_ms / 1000.0))
        return b'\x00' * (num_samples * 2)  # 2 bytes per sample for LINEAR16

    async def recognize_stream(self, audio_generator: AsyncGenerator[bytes, None]):
        """
        Takes an async generator of audio bytes and yields transcript events.
        """
        
        # Configure the request
        diarization_config = speech.SpeakerDiarizationConfig(
            enable_speaker_diarization=self.enable_diarization,
            min_speaker_count=self.di_speaker_count,
            max_speaker_count=self.di_speaker_count,
        )
        
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=self.sample_rate,
            language_code=self.language_code,
            diarization_config=diarization_config if self.enable_diarization else None,
            model="latest_long" 
        )
        
        streaming_config = speech.StreamingRecognitionConfig(
            config=config,
            interim_results=True
        )
        
        # Statistics tracking
        total_bytes = 0
        chunk_count = 0
        max_amplitude_seen = 0
        cumulative_rms = 0.0

        # Bridge: AsyncGenerator[bytes] -> AsyncIterable[StreamingRecognizeRequest]
        async def request_generator():
            nonlocal total_bytes, chunk_count, max_amplitude_seen, cumulative_rms
            last_yield_time = 0
            
            yield speech.StreamingRecognizeRequest(streaming_config=streaming_config)
            logger.info("[StreamingSTT] Sent config, starting audio stream")
            
            async for chunk in audio_generator:
                chunk_count += 1
                total_bytes += len(chunk)
                
                # Compute stats every 10 chunks (reduce log spam)
                if chunk_count % 10 == 0 or chunk_count == 1:
                    stats = compute_audio_stats(chunk)
                    max_amplitude_seen = max(max_amplitude_seen, stats["max_abs"])
                    cumulative_rms = (cumulative_rms * (chunk_count - 1) + stats["rms"]) / chunk_count
                    logger.info(f"[StreamingSTT] Chunk #{chunk_count}: {len(chunk)} bytes, "
                               f"max_abs={stats['max_abs']}, rms={stats['rms']} ({stats['rms_db']}dB)")
                
                # [DEBUG] Latency Check
                current_time = time.time()
                delta_ms = (current_time - last_yield_time) * 1000 if last_yield_time > 0 else 0
                last_yield_time = current_time
                
                # Check for large gaps > 200ms
                if delta_ms > 200:
                    logger.warning(f"[StreamingSTT] Slow yield to Google: delta={delta_ms:.1f}ms")

                yield speech.StreamingRecognizeRequest(audio_content=chunk)
            
            # Log final stats
            duration_sec = total_bytes / (self.sample_rate * 2)  # 16-bit = 2 bytes/sample
            logger.info(f"[StreamingSTT] Audio stream ended: {chunk_count} chunks, "
                       f"{total_bytes} bytes (~{duration_sec:.1f}s), "
                       f"max_amplitude={max_amplitude_seen}, avg_rms={cumulative_rms:.1f}")
            
            # Warn if audio appears silent
            if max_amplitude_seen < 500:
                logger.warning(f"[StreamingSTT] ⚠️ Audio appears SILENT (max_amplitude={max_amplitude_seen} < 500). "
                              "Check client audio capture!")

        # Call the API
        result_count = 0
        try:
            logger.info(f"[StreamingSTT] Starting streaming_recognize (lang={self.language_code}, "
                       f"diarization={self.enable_diarization})")
            
            responses = await self.client.streaming_recognize(requests=request_generator())
            
            async for response in responses:
                # Log raw response info
                # [DEBUG] Dump full response for deep inspection
                logger.debug(f"[StreamingSTT] Raw Response: {response}")
                logger.debug(f"[StreamingSTT] Response: results_count={len(response.results)}, "
                            f"speech_event_type={response.speech_event_type}")
                
                if not response.results:
                    continue
                
                result = response.results[0]
                if not result.alternatives:
                    logger.debug("[StreamingSTT] Result has no alternatives, skipping")
                    continue
                
                result_count += 1
                alternative = result.alternatives[0]
                transcript = alternative.transcript
                
                logger.info(f"[StreamingSTT] Result #{result_count}: is_final={result.is_final}, "
                           f"confidence={alternative.confidence:.2f}, text='{transcript[:50]}...'")
                
                words_info = []
                if self.enable_diarization:
                     for word in alternative.words:
                         words_info.append({
                             "word": word.word,
                             "start": word.start_time.total_seconds(),
                             "end": word.end_time.total_seconds(),
                             "speakerTag": word.speaker_tag
                         })

                yield {
                    "is_final": result.is_final,
                    "transcript": transcript,
                    "words": words_info
                }
            
            logger.info(f"[StreamingSTT] Stream completed. Total results yielded: {result_count}")

        except Exception as e:
            logger.error(f"[StreamingSTT] Error: {e}", exc_info=True)
            logger.info(f"[StreamingSTT] Stats at error: chunks={chunk_count}, bytes={total_bytes}, results={result_count}")
            raise e
