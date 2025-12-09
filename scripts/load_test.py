#!/usr/bin/env python3
"""
Load Test Script for ClassnoteX Backend API
============================================

このスクリプトは classnote-api の負荷試験を行います。
主に POST /sessions/{id}/summarize と /quiz エンドポイントをテストします。

使い方:
    # 依存関係インストール
    pip install httpx asyncio

    # 実行（デフォルト: 5並列、30秒）
    python load_test.py

    # カスタム設定
    python load_test.py --concurrency 10 --duration 60 --endpoint summarize
"""

import asyncio
import argparse
import time
import statistics
from datetime import datetime
from typing import List, Dict, Any
import json

try:
    import httpx
except ImportError:
    print("httpx が必要です: pip install httpx")
    exit(1)

# ========== 設定 ==========
BASE_URL = "https://classnote-api-900324644592.asia-northeast1.run.app"

# テスト用のダミー文字起こし（実際の講義に近い長さ）
DUMMY_TRANSCRIPT = """
これは負荷試験用のダミー講義文字起こしです。
今日は人工知能、特にディープラーニングについて学びます。

まず、人工知能とは何かについて説明します。
人工知能、略してAIは、人間の知能を模倣するコンピュータシステムのことです。
機械学習はAIの一分野で、データからパターンを学習します。

ディープラーニングは機械学習の一種で、多層のニューラルネットワークを使用します。
これにより、画像認識、音声認識、自然言語処理など、様々なタスクで高い性能を発揮します。

具体的な応用例としては、自動運転車、医療診断支援、翻訳サービスなどがあります。
ChatGPTやGeminiのような大規模言語モデルもディープラーニングの成果です。

重要なポイントをまとめると：
1. AIは人間の知能を模倣するシステム
2. 機械学習はデータから学習する手法
3. ディープラーニングは多層ニューラルネットワークを使用
4. 様々な実用的な応用がある

次回は、これらの技術の具体的な実装方法について学びます。
質問がある方は、講義後にお声がけください。
"""


class LoadTestResult:
    def __init__(self):
        self.total_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        self.latencies: List[float] = []
        self.errors: List[str] = []
        self.start_time = None
        self.end_time = None

    def add_success(self, latency_ms: float):
        self.total_requests += 1
        self.successful_requests += 1
        self.latencies.append(latency_ms)

    def add_failure(self, error: str):
        self.total_requests += 1
        self.failed_requests += 1
        self.errors.append(error)

    def get_summary(self) -> Dict[str, Any]:
        duration = (self.end_time - self.start_time) if self.start_time and self.end_time else 0
        
        if self.latencies:
            sorted_latencies = sorted(self.latencies)
            p50 = sorted_latencies[len(sorted_latencies) // 2]
            p95 = sorted_latencies[int(len(sorted_latencies) * 0.95)] if len(sorted_latencies) >= 20 else max(sorted_latencies)
            p99 = sorted_latencies[int(len(sorted_latencies) * 0.99)] if len(sorted_latencies) >= 100 else max(sorted_latencies)
            avg = statistics.mean(self.latencies)
        else:
            p50 = p95 = p99 = avg = 0

        return {
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "success_rate": f"{(self.successful_requests / self.total_requests * 100):.1f}%" if self.total_requests > 0 else "N/A",
            "duration_seconds": f"{duration:.1f}",
            "requests_per_second": f"{self.total_requests / duration:.2f}" if duration > 0 else "N/A",
            "latency_avg_ms": f"{avg:.0f}",
            "latency_p50_ms": f"{p50:.0f}",
            "latency_p95_ms": f"{p95:.0f}",
            "latency_p99_ms": f"{p99:.0f}",
            "unique_errors": list(set(self.errors))[:5],
        }


async def create_test_session(client: httpx.AsyncClient) -> str:
    """テスト用セッションを作成"""
    url = f"{BASE_URL}/sessions"
    payload = {
        "title": f"Load Test Session {datetime.now().isoformat()}",
        "mode": "lecture",
        "userId": "load-test-user"
    }
    response = await client.post(url, json=payload)
    response.raise_for_status()
    return response.json()["id"]


async def setup_transcript(client: httpx.AsyncClient, session_id: str):
    """テスト用トランスクリプトを設定"""
    url = f"{BASE_URL}/sessions/{session_id}/transcript"
    payload = {"transcriptText": DUMMY_TRANSCRIPT}
    response = await client.post(url, json=payload)
    response.raise_for_status()


async def call_summarize(client: httpx.AsyncClient, session_id: str) -> float:
    """要約APIを呼び出し、レイテンシをmsで返す"""
    url = f"{BASE_URL}/sessions/{session_id}/summarize"
    start = time.perf_counter()
    response = await client.post(url)
    latency_ms = (time.perf_counter() - start) * 1000
    response.raise_for_status()
    return latency_ms


async def call_quiz(client: httpx.AsyncClient, session_id: str) -> float:
    """クイズAPIを呼び出し、レイテンシをmsで返す"""
    url = f"{BASE_URL}/sessions/{session_id}/quiz?count=5"
    start = time.perf_counter()
    response = await client.post(url)
    latency_ms = (time.perf_counter() - start) * 1000
    response.raise_for_status()
    return latency_ms


async def worker(
    worker_id: int,
    session_ids: List[str],
    endpoint: str,
    result: LoadTestResult,
    stop_event: asyncio.Event,
    client: httpx.AsyncClient
):
    """ワーカータスク: 指定されたエンドポイントを繰り返し呼び出す"""
    idx = 0
    while not stop_event.is_set():
        session_id = session_ids[idx % len(session_ids)]
        idx += 1
        
        try:
            if endpoint == "summarize":
                latency = await call_summarize(client, session_id)
            elif endpoint == "quiz":
                latency = await call_quiz(client, session_id)
            else:
                latency = await call_summarize(client, session_id)
            
            result.add_success(latency)
            print(f"[Worker {worker_id}] ✅ {endpoint} @ {session_id[:20]}... - {latency:.0f}ms")
            
        except httpx.HTTPStatusError as e:
            error_msg = f"HTTP {e.response.status_code}"
            result.add_failure(error_msg)
            print(f"[Worker {worker_id}] ❌ {endpoint} @ {session_id[:20]}... - {error_msg}")
            
        except Exception as e:
            error_msg = str(e)[:50]
            result.add_failure(error_msg)
            print(f"[Worker {worker_id}] ❌ {endpoint} @ {session_id[:20]}... - {error_msg}")
        
        # 少し待機（サーバーを圧倒しすぎないように）
        await asyncio.sleep(0.5)


async def run_load_test(concurrency: int, duration_seconds: int, endpoint: str):
    """負荷試験を実行"""
    print(f"\n{'='*60}")
    print(f"ClassnoteX Backend Load Test")
    print(f"{'='*60}")
    print(f"Target: {BASE_URL}")
    print(f"Endpoint: /sessions/{{id}}/{endpoint}")
    print(f"Concurrency: {concurrency} workers")
    print(f"Duration: {duration_seconds} seconds")
    print(f"{'='*60}\n")

    result = LoadTestResult()
    
    async with httpx.AsyncClient(timeout=120.0) as client:
        # テスト用セッションを作成
        print("📦 Creating test sessions...")
        session_ids = []
        for i in range(min(concurrency, 5)):  # 最大5セッションを再利用
            try:
                session_id = await create_test_session(client)
                await setup_transcript(client, session_id)
                session_ids.append(session_id)
                print(f"   Created: {session_id}")
            except Exception as e:
                print(f"   ❌ Failed to create session: {e}")
                return
        
        if not session_ids:
            print("❌ No test sessions available. Aborting.")
            return
        
        print(f"\n🚀 Starting load test with {concurrency} workers for {duration_seconds}s...\n")
        
        stop_event = asyncio.Event()
        result.start_time = time.time()
        
        # ワーカータスクを起動
        workers = [
            asyncio.create_task(worker(i, session_ids, endpoint, result, stop_event, client))
            for i in range(concurrency)
        ]
        
        # 指定時間待機
        await asyncio.sleep(duration_seconds)
        
        # 停止シグナルを送信
        stop_event.set()
        result.end_time = time.time()
        
        # ワーカーの終了を待機（最大5秒）
        await asyncio.wait(workers, timeout=5)
        
        # 結果を表示
        print(f"\n{'='*60}")
        print("📊 Load Test Results")
        print(f"{'='*60}")
        
        summary = result.get_summary()
        print(f"""
Total Requests:     {summary['total_requests']}
Successful:         {summary['successful_requests']}
Failed:             {summary['failed_requests']}
Success Rate:       {summary['success_rate']}
Duration:           {summary['duration_seconds']}s
Requests/Second:    {summary['requests_per_second']}

Latency:
  Average:          {summary['latency_avg_ms']}ms
  P50 (median):     {summary['latency_p50_ms']}ms
  P95:              {summary['latency_p95_ms']}ms
  P99:              {summary['latency_p99_ms']}ms
""")
        
        if summary['unique_errors']:
            print("Errors encountered:")
            for err in summary['unique_errors']:
                print(f"  - {err}")
        
        print(f"\n{'='*60}")
        
        # 評価
        success_rate = result.successful_requests / result.total_requests * 100 if result.total_requests > 0 else 0
        p95 = float(summary['latency_p95_ms']) if summary['latency_p95_ms'] != "0" else 0
        
        print("\n📋 Assessment:")
        if success_rate >= 99 and p95 < 5000:
            print("   ✅ EXCELLENT - This concurrency level is well within safe limits")
        elif success_rate >= 95 and p95 < 10000:
            print("   ⚠️ ACCEPTABLE - Some latency increase observed, consider this the soft limit")
        else:
            print("   ❌ NOT RECOMMENDED - Too many errors or high latency at this concurrency level")


def main():
    parser = argparse.ArgumentParser(description="ClassnoteX Backend Load Test")
    parser.add_argument("--concurrency", "-c", type=int, default=5, help="Number of concurrent workers (default: 5)")
    parser.add_argument("--duration", "-d", type=int, default=30, help="Test duration in seconds (default: 30)")
    parser.add_argument("--endpoint", "-e", choices=["summarize", "quiz"], default="summarize", help="Endpoint to test (default: summarize)")
    
    args = parser.parse_args()
    
    asyncio.run(run_load_test(args.concurrency, args.duration, args.endpoint))


if __name__ == "__main__":
    main()
