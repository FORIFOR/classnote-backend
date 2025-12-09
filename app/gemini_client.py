import os
from typing import List, Optional

import google.auth
import vertexai
from vertexai.generative_models import GenerativeModel

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
VERTEX_REGION = os.environ.get("VERTEX_REGION", "asia-northeast1")
GEMINI_MODEL_NAME = os.environ.get("GEMINI_MODEL_NAME", "gemini-1.5-flash")

_vertex_initialized = False
_model: Optional[GenerativeModel] = None


def _ensure_vertex():
    global _vertex_initialized, _model, PROJECT_ID
    if not _vertex_initialized:
        if not PROJECT_ID:
            try:
                # Cloud Run / ADC からプロジェクトIDを取得試行
                _, PROJECT_ID = google.auth.default()
            except Exception as e:
                print(f"Warning: failed to get project from ADC: {e}")

        if not PROJECT_ID:
            raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set and could not be inferred from ADC")

        vertexai.init(project=PROJECT_ID, location=VERTEX_REGION)
        _model = GenerativeModel(GEMINI_MODEL_NAME)
        _vertex_initialized = True


def summarize_transcript(transcript: str, mode: str = "lecture") -> str:
    """
    transcript（1本のテキスト）を受け取り、充実した要約テキストを返す。
    """
    _ensure_vertex()

    if mode == "lecture":
        prompt = f"""あなたは優秀な講義ノート作成アシスタントです。

以下の講義の文字起こしを、学生が復習・試験対策に使える**充実した講義ノート**にまとめてください。

【要約の構成】
1. **📋 概要** (2-3文で講義全体の主題を説明)
2. **🎯 学習目標** (この講義で理解すべきポイントを3-5個)
3. **📝 主要トピック** (各トピックについて詳しく説明)
   - 重要な定義・概念
   - キーワードとその説明
   - 具体例・事例
   - 計算式・公式（あれば）
4. **💡 重要ポイント** (特に覚えておくべき内容を箇条書き)
5. **❓ よくある疑問点** (学生が疑問に思いそうな点とその回答)
6. **🔗 関連トピック** (関連する概念や次に学ぶべき内容)

【注意事項】
- Markdown形式で見やすく整形してください
- 専門用語には簡単な説明を添えてください
- 重要な部分は**太字**で強調してください
- 箇条書きを活用して読みやすくしてください
- 単なる文字起こしの要約ではなく、理解を助ける構成にしてください

=== 講義の文字起こし ===
{transcript}
"""
    else:
        # 会議モード
        prompt = f"""あなたはプロフェッショナルな会議議事録アシスタントです。

以下の会議の文字起こしを、**ビジネスで即座に活用できる議事録**にまとめてください。

【議事録の構成】
1. **📋 会議概要**
   - 主なテーマ・目的
   - 参加者の立場・役割（推測可能な場合）

2. **📌 決定事項** (合意された内容を明確に)
   - 何が決まったか
   - 決定の理由・背景

3. **✅ アクションアイテム (TODO)**
   - タスク内容
   - 担当者（特定できる場合）
   - 期限（言及があれば）
   - 優先度（推測可能な場合）

4. **💬 議論のポイント**
   - 主要な論点
   - 各立場の意見
   - 未解決の課題

5. **📊 共有された情報・数値**
   - 報告された数値・データ
   - 共有された事実・状況

6. **⚠️ 懸念事項・リスク**
   - 指摘された問題点
   - 今後の注意点

7. **📅 次のステップ**
   - 次回の予定
   - フォローアップが必要な項目

【注意事項】
- Markdown形式で見やすく整形してください
- ビジネスで即座に参照できる形式にしてください
- 重要な決定事項やTODOは**太字**で強調してください
- 曖昧な表現は避け、具体的に記載してください
- 発言内容が不明確な場合は「（確認が必要）」と記載

=== 会議の文字起こし ===
{transcript}
"""

    if _model is None:
         raise RuntimeError("Vertex AI model not initialized")

    resp = _model.generate_content([prompt])
    return resp.text.strip()


def generate_quiz(transcript: str, mode: str = "lecture", count: int = 5) -> str:
    """
    transcript をもとに小テスト（4択選択式 x 5問）を作る。
    iOS の QuizParser が期待するフォーマットで出力させる。
    """
    _ensure_vertex()

    if mode == "lecture":
        role = "あなたは優秀な講義用の小テスト作成アシスタントです。"
    else:
        role = "あなたはビジネス研修用の小テスト作成アシスタントです。"

    prompt = f"""{role}

以下の文字起こしの内容を理解しているか確認するために、
日本語で **必ず5問** の小テストを作成してください。

【重要な制約】
- 必ず **5問ちょうど** 作成すること
- すべての問題を **4択の選択問題** にすること（A, B, C, D の4つ）
- 短答式や穴埋め式は **禁止**
- 以下の出力フォーマットを **厳密に** 守ること

【出力フォーマット】

### 問題1
質問文をここに書く

- A. 選択肢Aの内容
- B. 選択肢Bの内容
- C. 選択肢Cの内容
- D. 選択肢Dの内容

**正解:** A
**解説:** 正解の理由を1〜2文で説明

### 問題2
（同じ形式で続ける）

### 問題3
（同じ形式で続ける）

### 問題4
（同じ形式で続ける）

### 問題5
（同じ形式で続ける）

【注意】
- 上記フォーマット以外の余計な文章は出力しないでください
- 各問題には必ず A, B, C, D の4つの選択肢を付けてください
- 正解は A, B, C, D のいずれか1つだけを記載してください

=== 文字起こし ===
{transcript}
"""

    if _model is None:
        raise RuntimeError("Vertex AI model not initialized")

    resp = _model.generate_content([prompt])
    return resp.text.strip()

