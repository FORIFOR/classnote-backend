from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

video_id = "N-NrA10514w"

try:
    print(f"Checking {video_id} locally...")
    tx = YouTubeTranscriptApi.list_transcripts(video_id)
    print("Available transcripts:")
    for t in tx:
        print(f"- {t.language} ({t.language_code}) Generated:{t.is_generated}")
except Exception as e:
    print(f"Error: {e}")
