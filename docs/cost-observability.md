# Cost Observability (Phase cost.1 — implemented)

既存 `tools/monitoring_dashboard` の `💰 Costs` タブを「**経営視点で毎日
見れる原価/粗利ダッシュボード**」に格上げするための backend 土台。

## 実装範囲 (今 PR)

- `app/services/cost_pricing.py` — Gemini / Firestore / Cloud Run / GCS / STT
  の単価定義 (single source of truth)
- `app/services/cost_calculator.py` — per-request 純計算 (I/O なし・
  unit-testable)
- `app/services/usage_metering.py` — `/usage_events/{eventId}` に cost
  enriched 行を書き込む best-effort helper (失敗しても user flow を
  壊さない)
- `app/routes/admin_costs.py` — admin 専用の 5 endpoints
- `main.py` に router 登録

## 実装 **未** 範囲 (次 PR 以降)

- Phase cost.2: 既存 LLM 呼び出し箇所 (gemini_chat / assist / summarize
  worker) に `record_usage_event` 呼び出しを挿入
- Phase cost.3: 日次集計 (`/user_daily_usage`, `/account_daily_usage`)
  を `aggregate_daily_usage.py` から書き込む
- Phase cost.4: BigQuery Cloud Billing export と突合する
  `/billing_reconciliation/{yyyy_mm}` + correctionFactor 適用
- Phase cost.5: tools/monitoring_dashboard/app.py の `tab_costs` を
  新 API 消費に書き換え

## 新規エンドポイント (admin only — Firebase custom claim `admin:true` 必須)

| Method | Path | 用途 |
|---|---|---|
| GET | `/admin/costs/overview?from_date=&to_date=` | KPI (売上/原価/粗利/粗利率/平均原価) |
| GET | `/admin/costs/timeseries?from_date=&to_date=&group_by=day` | 日次 cost 推移 |
| GET | `/admin/costs/top-users?from_date=&to_date=&limit=20` | 原価上位ユーザ |
| GET | `/admin/costs/top-sessions?from_date=&to_date=&limit=20` | 原価上位セッション |
| GET | `/admin/costs/features?from_date=&to_date=` | 機能別コスト内訳 |

全て `dateKey` ("YYYY-MM-DD") 範囲フィルタで `/usage_events` を集計。

### 例: `/admin/costs/overview`

```jsonc
{
  "range": {"fromDate": "2026-04-01", "toDate": "2026-04-30"},
  "revenueJpy": 0.0,             // Phase cost.4 まで 0
  "estimatedCostUsd": 412.83,
  "estimatedCostJpy": 61924.5,
  "grossProfitJpy": -61924.5,    // revenue=0 の間は負
  "grossMarginPct": 0.0,
  "costBreakdown": {
    "vertexUsd": 332.17, "firestoreUsd": 41.92, "cloudRunUsd": 24.44,
    "storageUsd": 14.30, "sttUsd": 0.00
  },
  "tokens": {"input": 145392012, "output": 22381928},
  "usage": {
    "activeUsers": 842, "sessionCount": 3911,
    "recordingSeconds": 10019520, "avgCostUsdPerSession": 0.1055
  },
  "reconciled": false            // Phase cost.4 で true に flip
}
```

### 例: `/admin/costs/features`

```jsonc
{
  "items": [
    {"feature": "summary", "costUsd": 214.32, "callCount": 3911,
     "inputTokens": 82000000, "outputTokens": 9100000,
     "avgInputTokens": 20966.0, "avgOutputTokens": 2327.0,
     "avgCostUsd": 0.05481, "avgDurationMs": 1820.0,
     "sessionCount": 3911, "userCount": 842, "costJpy": 32148.0},
    {"feature": "chat", "costUsd": 88.11, ...},
    {"feature": "assist", "costUsd": 65.04, ...}
  ]
}
```

## Firestore `/usage_events` スキーマ (Phase cost.1 で確定)

各 LLM 呼び出し / Firestore batch / Cloud Run request の終了時に
`record_usage_event(...)` が 1 行書き込む (Phase cost.2 で実装)。

```jsonc
/usage_events/{eventId}
  eventId, userId, accountId, sessionId?, requestId?,
  feature,       // "summary" | "quiz" | "chat" | "assist" | "transcribe" | "todo" | ...
  service,       // "vertex_ai" | "firestore" | "cloud_run" | "gcs" | "stt"
  provider,      // "google_cloud"
  region, model, status,
  startedAt, finishedAt, durationMs,
  dateKey,       // "YYYY-MM-DD" — 範囲 query の index
  billable: {
    inputTokens, outputTokens, groundedPrompts, groundedPromptsOverFree,
    documentReads, documentWrites, documentDeletes,
    vcpuSecondsEst, gibSecondsEst, requestCount,
    storageGiBHours, classAOps, classBOps, egressGiB,
    sttMinutes
  },
  estimatedCostUsd, estimatedCostJpy, usdJpyRate,
  costBreakdown: {vertexUsd, firestoreUsd, cloudRunUsd, storageUsd, sttUsd,
                  perModelUsd: {"gemini-2.0-flash-lite": 0.00133, ...}},
  createdAt: serverTimestamp,
  extra: {}
```

### 推奨 Firestore index

```
usage_events:
  dateKey (ASC) + createdAt (ASC)            # range query
  userId (ASC) + dateKey (ASC)               # top-users
  sessionId (ASC) + dateKey (ASC)            # top-sessions
  feature (ASC) + dateKey (ASC)              # per-feature
```

firestore.indexes.json は次 PR で更新。Phase cost.1 はコレクション無し
でも 500 にならない (空配列を返す)。

## Phase cost.2 — LLM 呼び出しへの metering hook (次 PR)

挿入箇所 (最小限):

| 位置 | feature | model | 追加 metering |
|---|---|---|---|
| `app/services/gemini_chat.py:call_gemini_chat` | chat | CHAT_MODEL_NAME | Vertex + CloudRun |
| `app/services/gemini_chat.py:call_gemini_general_chat` | chat | GENERAL_MODEL_NAME | 同 |
| `app/services/gemini_chat.py:call_gemini_general_with_search` | chat | SEARCH_MODEL_NAME | Vertex (grounded_prompts=1) + CloudRun |
| `app/services/gemini_chat.py:call_gemini_search_hybrid` | chat | SEARCH_MODEL_NAME | 同 |
| `app/routes/tasks.py:_handle_summarize_task_core` | summary | GEMINI_MODEL_NAME | Vertex + CloudRun |
| `app/routes/tasks.py:_handle_quiz_task_core` | quiz | GEMINI_MODEL_NAME | 同 |
| `app/routes/tasks.py:_handle_playlist_task_core` | playlist | GEMINI_MODEL_NAME | 同 |
| `app/routes/assist.py:assist` | assist | ASSIST_MODEL_NAME | Vertex (+grounded_prompts=1 for fact_check) + CloudRun |
| `app/routes/tasks.py:_handle_transcribe_task_core` | transcribe | — | STT (minutes) |

実装パターン (共通):

```python
from time import perf_counter
from app.services.usage_metering import record_usage_event
from app.services.cost_calculator import VertexUsage, CloudRunUsage

t0 = perf_counter()
resp = gemini_model.generate_content(...)
elapsed = perf_counter() - t0

try:
    usage = resp.usage_metadata
    record_usage_event(
        user_id=user_id,
        account_id=account_id,
        session_id=session_id,
        feature="summary",
        service="vertex_ai",
        model="gemini-2.0-flash-lite",
        duration_ms=int(elapsed * 1000),
        vertex_usage=VertexUsage(
            model="gemini-2.0-flash-lite",
            input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
            output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
        ),
        cloud_run_usage=CloudRunUsage(
            vcpu_seconds_est=elapsed / 15.0,
            gib_seconds_est=(elapsed * 2.0) / 15.0,
            request_count=1,
        ),
    )
except Exception:
    pass  # never raise from metering
```

never raise — Phase 1 の usage_metering 自体が try/except 済み。

## Phase cost.5 — Dashboard 改修 (次々 PR)

現状 `tools/monitoring_dashboard/app.py:2651` の `tab_costs` は:
- Firestore billable_reads / billable_writes を直接計算
- Speech STT rate をハードコード乗算
- ユーザー/セッション単位の詳細なし

これを新 API 消費に書き換える:

```python
# tools/monitoring_dashboard/app.py の該当部分のみ差し替え
with tab_costs:
    col1, col2 = st.columns(2)
    with col1:
        from_date = st.date_input("from", value=first_of_month).isoformat()
    with col2:
        to_date = st.date_input("to", value=today).isoformat()

    ov = api_get(f"/admin/costs/overview?from_date={from_date}&to_date={to_date}")

    # KPI cards
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("売上", f"¥{ov['revenueJpy']:,.0f}")
    c2.metric("原価", f"¥{ov['estimatedCostJpy']:,.0f}")
    c3.metric("粗利", f"¥{ov['grossProfitJpy']:,.0f}")
    c4.metric("粗利率", f"{ov['grossMarginPct']:.1f}%")
    c5.metric("平均/セッション", f"${ov['usage']['avgCostUsdPerSession']:.4f}")

    # Pie: cost breakdown
    cb = ov["costBreakdown"]
    pie_df = pd.DataFrame({"service": list(cb.keys()), "usd": list(cb.values())})
    st.plotly_chart(px.pie(pie_df, values="usd", names="service", hole=0.4))

    # Line: daily cost
    ts = api_get(f"/admin/costs/timeseries?from_date={from_date}&to_date={to_date}")
    ts_df = pd.DataFrame(ts["items"])
    st.plotly_chart(px.line(ts_df, x="date", y="costUsd"))

    # Top users / sessions / features
    tu = api_get(f"/admin/costs/top-users?from_date={from_date}&to_date={to_date}&limit=20")
    st.dataframe(pd.DataFrame(tu["items"]))
    tf = api_get(f"/admin/costs/features?from_date={from_date}&to_date={to_date}")
    st.dataframe(pd.DataFrame(tf["items"]))
```

admin API 呼び出しは **Firebase admin ID token** を `Authorization: Bearer`
で送る必要あり。既存 dashboard が admin ログインしているなら token 取得
ロジックは既に存在。

## 非機能要件

- **fail-open**: `record_usage_event` が失敗してもユーザーリクエストは成功する (Phase 1 で対応済み)
- **backward-compat**: 既存 `app/services/usage.py:usage_logger.log()` は維持。新 usage_metering は **追加的**
- **PII**: usage_events には `transcript` / `summary` 本文は入らない (カウンタ + メタのみ)
- **retention**: 90 日 TTL が推奨 (Firestore TTL field `deleteAfterAt` を Phase cost.2 で付与)

## テスト checklist

Phase cost.1:
- [ ] `/admin/costs/overview` に admin 未認証で 401
- [ ] `/admin/costs/overview` に admin 認証ありで 200、`/usage_events` 空なら全部 0
- [ ] 日付フォーマット不正で 400 `BAD_DATE_RANGE`
- [ ] `from_date > to_date` で 400
- [ ] `/usage_events/{eventId}` を手動で Firestore に投入 → overview / timeseries / top-users / features で該当行が集計される
- [ ] `usage_metering.record_usage_event()` を呼ぶと `/usage_events` に 1 行、`estimatedCostUsd > 0` で Vertex tokens が反映される
