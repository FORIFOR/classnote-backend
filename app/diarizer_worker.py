"""
話者分離 (Speaker Diarization) ワーカー
=========================================

このモジュールは、録音完了後の音声ファイルに対して話者分離を行います。

現在の実装:
- スタブ実装（デモ用のダミーデータを返す）
- 実際の話者分離は ReazonSpeech + OnlineDiarizer などを統合予定

将来の実装:
- ReazonSpeech (Zipformer) による ASR
- ECAPA-TDNN による話者埋め込み
- VarKClustering によるオンラインクラスタリング
"""

import os
import uuid
import tempfile
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from pathlib import Path


# ---------- データクラス ---------- #

@dataclass
class SpeakerInfo:
    """話者情報"""
    id: str
    label: str
    display_name: str
    color_hex: str


@dataclass
class DiarizedSegment:
    """話者分離されたセグメント"""
    id: str
    start: float
    end: float
    speaker_id: str
    text: str


@dataclass
class DiarizationResult:
    """話者分離の結果"""
    speakers: List[SpeakerInfo]
    segments: List[DiarizedSegment]
    stats: Dict[str, Dict[str, Any]]  # {"spk_0": {"total_sec": 120.5, "turns": 15}}


# ---------- 色パレット ---------- #

SPEAKER_COLORS = [
    "#FFADAD",  # ピンク
    "#A0C4FF",  # ブルー
    "#CAFFBF",  # グリーン
    "#FFD6A5",  # オレンジ
    "#BDB2FF",  # パープル
    "#FDFFB6",  # イエロー
    "#9BF6FF",  # シアン
    "#FFC6FF",  # マゼンタ
]


# ---------- スタブ実装 (デモ用) ---------- #

def run_diarization_stub(
    audio_path: str,
    transcript: str,
    num_speakers: Optional[int] = None
) -> DiarizationResult:
    """
    スタブ実装: デモ用のダミー話者分離結果を生成
    
    実際のパイプラインでは:
    1. 音声を VAD でセグメント分割
    2. 各セグメントで ASR (ReazonSpeech)
    3. 各セグメントで話者埋め込み (ECAPA-TDNN)
    4. オンラインクラスタリング (VarKClustering)
    5. 結果をマージして返す
    """
    # transcript を簡易的に文に分割
    import re
    sentences = re.split(r'[。．.！？!?]', transcript)
    sentences = [s.strip() for s in sentences if s.strip()]
    
    if not sentences:
        sentences = [transcript[:100] if transcript else "（文字起こしなし）"]
    
    # 話者数を推定（スタブでは2〜3人固定）
    estimated_speakers = num_speakers or min(3, max(2, len(sentences) // 5))
    
    # 話者情報を生成
    speakers = []
    for i in range(estimated_speakers):
        label = chr(ord('A') + i)
        speakers.append(SpeakerInfo(
            id=f"spk_{i}",
            label=label,
            display_name=f"話者{label}",
            color_hex=SPEAKER_COLORS[i % len(SPEAKER_COLORS)]
        ))
    
    # セグメントを生成（文ごとに交互に話者を割り当て）
    segments = []
    current_time = 0.0
    avg_duration = 3.0  # 1文あたり平均3秒
    
    for i, sentence in enumerate(sentences):
        if not sentence:
            continue
            
        # 話者を交互に割り当て（スタブなので単純化）
        speaker_idx = i % estimated_speakers
        duration = len(sentence) * 0.1 + 1.0  # 文字数に応じた簡易計算
        
        segments.append(DiarizedSegment(
            id=f"seg_{uuid.uuid4().hex[:8]}",
            start=current_time,
            end=current_time + duration,
            speaker_id=f"spk_{speaker_idx}",
            text=sentence
        ))
        
        current_time += duration + 0.2  # 0.2秒の間
    
    # 統計情報を計算
    stats = {}
    for speaker in speakers:
        speaker_segments = [s for s in segments if s.speaker_id == speaker.id]
        total_sec = sum(s.end - s.start for s in speaker_segments)
        stats[speaker.id] = {
            "totalSec": round(total_sec, 2),
            "turns": len(speaker_segments)
        }
    
    return DiarizationResult(
        speakers=speakers,
        segments=segments,
        stats=stats
    )


# ---------- 実際の話者分離パイプライン (要実装) ---------- #

def run_diarization_pipeline(
    audio_path: str,
    num_speakers: Optional[int] = None
) -> DiarizationResult:
    """
    実際の話者分離パイプライン
    
    TODO: ReazonSpeech + OnlineDiarizer を統合
    
    Required:
    - ReazonSpeechEngine (ASR)
    - ECAPA-TDNN (Speaker Embedding)
    - OnlineDiarizer (Clustering)
    """
    raise NotImplementedError(
        "実際の話者分離パイプラインは未実装です。"
        "ReazonSpeech + OnlineDiarizer を統合してください。"
    )


# ---------- メインエントリポイント ---------- #

def process_diarization(
    session_id: str,
    audio_url: str,
    transcript: str,
    use_stub: bool = True,
    num_speakers: Optional[int] = None
) -> DiarizationResult:
    """
    話者分離のメインエントリポイント
    
    Args:
        session_id: セッションID
        audio_url: GCS の音声ファイルURL (gs://...)
        transcript: 文字起こしテキスト
        use_stub: スタブ実装を使うか (デフォルト: True)
        num_speakers: 話者数のヒント (None = 自動推定)
    
    Returns:
        DiarizationResult: 話者分離結果
    """
    print(f"[Diarization] Processing session: {session_id}")
    print(f"[Diarization] Audio URL: {audio_url}")
    print(f"[Diarization] Transcript length: {len(transcript)} chars")
    
    if use_stub:
        print("[Diarization] Using STUB implementation (demo mode)")
        return run_diarization_stub(
            audio_path=audio_url,
            transcript=transcript,
            num_speakers=num_speakers
        )
    else:
        # 実際のパイプラインを実行
        try:
            # 1. GCS から音声をダウンロード
            local_input = download_audio_from_gcs(audio_url)
            
            # 2. WAV (16kHz mono) に変換
            wav_path = convert_to_wav(local_input)
            
            # 3. リファレンス実装（pyannote.audio + ASR）
            # 注: 以下のコードを動かすには pip install pyannote.audio torch torchaudio が必要です
            # また、Hugging Face のアクセストークンが必要です (config.yaml or env)
            
            """
            from pyannote.audio import Pipeline
            import torch
            
            # A. 話者分離パイプラインのロード
            # config.yaml からトークンを読み込むか、環境変数 PYANNOTE_AUTH_TOKEN を設定
            pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                use_auth_token=os.environ.get("HUGGING_FACE_TOKEN")
            )
            
            # GPU があれば使う
            if torch.cuda.is_available():
                pipeline.to(torch.device("cuda"))
            
            # B. 推論実行
            diarization = pipeline(wav_path, num_speakers=num_speakers)
            
            # C. ASR実行 (例: ReazonSpeech / Whisper)
            # ここでは簡略化のため、既存の transcript を強制的にセグメントに割り当てるロジックを想定
            # 本来は、各セグメントの音声に対して ASR をかけるのが最も精度が良い
            
            speakers_map = {}
            segments_result = []
            stats = {}
            
            for turn, _, speaker_label in diarization.itertracks(yield_label=True):
                # turn.start, turn.end, speaker_label (e.g. "SPEAKER_00")
                
                # 話者IDの正規化
                if speaker_label not in speakers_map:
                    idx = len(speakers_map)
                    speakers_map[speaker_label] = {
                        "id": f"spk_{idx}",
                        "label": chr(ord('A') + idx),
                        "display_name": f"話者{chr(ord('A') + idx)}",
                        "color_hex": SPEAKER_COLORS[idx % len(SPEAKER_COLORS)]
                    }
                
                spk_info = speakers_map[speaker_label]
                
                # セグメントID生成
                seg_id = f"seg_{uuid.uuid4().hex[:8]}"
                
                # TODO: この区間のテキストを ASR で取得
                # text = asr_model.transcribe(wav_path, start=turn.start, end=turn.end)
                text = "（音声区間のテキスト認識は未実装）"
                
                segments_result.append(DiarizedSegment(
                    id=seg_id,
                    start=turn.start,
                    end=turn.end,
                    speaker_id=spk_info["id"],
                    text=text
                ))
            
            # 結果整形
            final_speakers = [SpeakerInfo(**v) for k, v in speakers_map.items()]
            
            return DiarizationResult(
                speakers=final_speakers,
                segments=segments_result,
                stats={} # Calculate stats here
            )
            """
            
            raise NotImplementedError(
                "Real pipeline requires 'pyannote.audio' and Hugging Face token. "
                "Uncomment the reference implementation in app/diarizer_worker.py."
            )
            
        finally:
            # クリーンアップ
            if 'local_input' in locals() and os.path.exists(local_input):
                os.unlink(local_input)
            if 'wav_path' in locals() and os.path.exists(wav_path):
                os.unlink(wav_path)



# ---------- ユーティリティ ---------- #

def download_audio_from_gcs(gcs_url: str, local_dir: str = "/tmp") -> str:
    """
    GCS から音声ファイルをダウンロード
    
    Args:
        gcs_url: gs://bucket/path/to/audio.m4a
        local_dir: ダウンロード先ディレクトリ
    
    Returns:
        ローカルファイルパス
    """
    from google.cloud import storage
    
    # gs://bucket/path を分解
    if not gcs_url.startswith("gs://"):
        raise ValueError(f"Invalid GCS URL: {gcs_url}")
    
    parts = gcs_url[5:].split("/", 1)
    bucket_name = parts[0]
    blob_path = parts[1] if len(parts) > 1 else ""
    
    # ダウンロード
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    
    local_path = os.path.join(local_dir, f"audio_{uuid.uuid4().hex[:8]}.m4a")
    blob.download_to_filename(local_path)
    
    print(f"[Diarization] Downloaded audio to: {local_path}")
    return local_path


def convert_to_wav(input_path: str, output_path: Optional[str] = None) -> str:
    """
    音声ファイルを WAV (16kHz mono) に変換
    
    Requires: ffmpeg
    """
    import subprocess
    
    if output_path is None:
        output_path = input_path.rsplit(".", 1)[0] + ".wav"
    
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-ar", "16000", "-ac", "1", "-f", "wav",
        output_path
    ]
    
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"[Diarization] Converted to WAV: {output_path}")
    return output_path


# ---------- テスト用 ---------- #

if __name__ == "__main__":
    # スタブ実装のテスト
    test_transcript = """
    今日は人工知能について学びます。
    はい、よろしくお願いします。
    まず、AIとは何かについて説明します。
    質問があればいつでもどうぞ。
    人工知能は人間の知能を模倣するシステムです。
    なるほど、具体例を教えていただけますか。
    例えば、自動運転車やチャットボットがありますね。
    ありがとうございます、よく分かりました。
    """
    
    result = process_diarization(
        session_id="test-session-123",
        audio_url="gs://test-bucket/audio.m4a",
        transcript=test_transcript,
        use_stub=True
    )
    
    print("\n----- 話者一覧 -----")
    for speaker in result.speakers:
        print(f"  {speaker.id}: {speaker.display_name} ({speaker.color_hex})")
    
    print("\n----- セグメント -----")
    for seg in result.segments[:5]:  # 最初の5件だけ表示
        print(f"  [{seg.start:.1f}s - {seg.end:.1f}s] {seg.speaker_id}: {seg.text[:30]}...")
    
    print("\n----- 統計 -----")
    for spk_id, stats in result.stats.items():
        print(f"  {spk_id}: {stats['totalSec']}秒, {stats['turns']}回発話")
