# Capacity Analysis Report

**Date:** 2025-12-10  
**Target System:** ClassnoteX API (Cloud Run)  
**Endpoint Analyzed:** `POST /sessions/{id}/summarize`

## 1. Executive Summary
The current system architecture relies on synchronous calls to the Gemini API for summarization. Load testing reveals significant latency (~13 seconds per request) and instability under low concurrency (3 workers). The primary bottleneck is the external LLM service latency. To achieve higher capacity and reliability, moving to an asynchronous processing model is strongly recommended.

## 2. Load Testing Results

### Test Configuration
- **Script:** `scripts/load_test.py`
- **Concurrency:** 3 concurrent workers
- **Duration:** 10 seconds
- **Target:** Production Cloud Run instance

### Metrics
| Metric | Value | Notes |
| :--- | :--- | :--- |
| **Total Requests** | 2 | Limited by high latency |
| **Success Rate** | 50% | 1 success, 1 failure (HTTP 500) |
| **Average Latency** | 12,878 ms (~12.9s) | Very high due to LLM processing |
| **Throughput** | ~0.2 RPS | Requests Per Second |

### Error Analysis
- **HTTP 500 Internal Server Error**: Observed immediately at concurrency = 3.
- **Cause**: likely due to LLM API rate limiting, timeouts, or race conditions in updating Firestore documents for the same session.

## 3. Architectural Bottlenecks

1.  **Synchronous LLM Processing**: The `summarize_session` endpoint waits for the Gemini API response before returning. This ties up Cloud Run container instances for 10-20 seconds per request, drastically reducing the number of concurrent requests a single instance can handle.
2.  **API Quotas**: Frequent 500 errors suggest we may be hitting Gemini API rate limits or concurrent request quotas.
3.  **Database Contention**: Multiple requests hitting the same session ID might cause race conditions when updating `summaryStatus` in Firestore.

## 4. Recommendations

### Short Term
-   **Implement Client-side Retries**: The web/iOS clients should handle 500 errors with exponential backoff.
-   **Rate Limiting**: Enforce rate limits per user to prevent abuse.

### Long Term (Recommended)
-   **Asynchronous Processing**:
    -   Change the `/summarize` endpoint to return `202 Accepted` immediately.
    -   Offload the summarization task to Google Cloud Tasks or Pub/Sub.
    -   Clients should poll for status or listen to Firestore snapshots for the `summaryStatus` update.
-   **Caching**: Check if a summary already exists (`summaryStatus == 'completed'`) before calling Gemini again.
-   **Instance Scaling**: Increase `min_instances` in Cloud Run if cold starts are contributing to latency (though in this test, latency was dominated by execution time).

## 5. Deployment Capacity Estimation
Based on current performance (~13s per request):
-   **1 Instance (Concurrency 80)**: Theoretically could hold 80 connections, but if CPU/Memory is bound by Python's async loop or external I/O wait, real throughput might be lower.
-   **Estimated Max Throughput per Instance**: ~4-6 requests per minute (assuming serial processing of heavy tasks, or higher with async I/O but limited by upstream quotas).

**Conclusion**: The system is functional but requires architectural changes (Async/Queue pattern) to scale beyond prototype levels.
