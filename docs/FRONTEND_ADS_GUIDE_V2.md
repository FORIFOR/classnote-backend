# [FROZEN] Rich Ad v2 - フロントエンド実装ガイド

> **Note**: 2026-01-19 実装取りやめにより凍結中。実装不要です。

## 概要

Rich Ad v2 は、画像/動画/レイアウトを **サーバー側で差し替え可能** な広告フォーマットです。
フロントエンドは `format` と `renderHints` に従って描画するだけで、広告主の切り替えに対応できます。

---

## API エンドポイント

### 広告取得: `GET /ads/placement`

```
GET /ads/placement?slot=summary_generating&session_id=xxx&job_id=yyy
Authorization: Bearer {token}  # Optional (Premium判定用)
```

**パラメータ:**
| 名前 | 必須 | 説明 |
|------|------|------|
| `slot` | ✅ | `summary_generating`, `quiz_generating`, `app_open` |
| `session_id` | ✅ | セッションID |
| `job_id` | - | ジョブID (あれば) |

**レスポンス例 (Rich v2):**
```json
{
  "ad": {
    "id": "ad_demo_v2_001",
    "placementId": "plc_abc123",
    "sponsorName": "Classnote Enterprise",
    "headline": "全社会議の議事録を自動化",
    "body": "セキュリティと管理機能を強化。",
    "format": "rich_v2",
    
    "assets": {
      "logo": { "type": "image", "url": "https://..." },
      "hero": { "type": "image", "url": "https://...", "blurHash": "L02..." },
      "video": { "type": "video", "url": "https://...", "posterUrl": "...", "muted": true }
    },
    
    "actions": [
      { "id": "primary", "style": "primary", "text": "資料請求", "url": "https://...", "openMode": "in_app" },
      { "id": "secondary", "style": "secondary", "text": "機能一覧", "url": "https://...", "openMode": "safari" }
    ],
    
    "policy": {
      "minViewSec": 10,
      "maxViewSec": 30,
      "skippableAfterSec": 10,
      "autodismissAtSec": 30
    },
    
    "renderHints": {
      "layout": "hero_blur_card",
      "showSponsorBadge": true,
      "showCountdown": true,
      "ctaPlacement": "card_bottom",
      "videoPlacement": "inline_in_card"
    },
    
    "theme": {
      "accentHex": "#3b82f6",
      "surfaceStyle": "ultraThin",
      "cornerRadius": 24
    }
  }
}
```

**Premiumユーザー / 広告なしの場合:**
```json
{ "ad": null }
```

---

## Swift Models

```swift
struct AdResponse: Decodable {
    let ad: SponsoredAd?
}

struct SponsoredAd: Decodable, Identifiable {
    let id: String
    let placementId: String
    let sponsorName: String
    let headline: String
    let body: String?
    let format: String? // "rich_v2" or nil (legacy)
    
    // v2 Fields
    let assets: Assets?
    let actions: [Action]?
    let policy: Policy?
    let renderHints: RenderHints?
    let theme: Theme?
    
    // Legacy Fallback
    let ctaText: String?
    let clickUrl: URL?
    
    struct Assets: Decodable {
        let logo: Asset?
        let hero: Asset?
        let video: Asset?
    }
    
    struct Asset: Decodable {
        let type: String // "image" | "video"
        let url: URL
        let posterUrl: URL?
        let muted: Bool?
        let loop: Bool?
        let blurHash: String?
    }
    
    struct Action: Decodable, Identifiable {
        let id: String
        let style: String // "primary" | "secondary"
        let text: String
        let url: URL
        let openMode: String // "safari" | "in_app" | "deeplink"
        let fallbackUrl: URL?
    }
    
    struct Policy: Decodable {
        let minViewSec: Int
        let maxViewSec: Int
        let skippableAfterSec: Int
        let autodismissAtSec: Int
    }
    
    struct RenderHints: Decodable {
        let layout: String // "hero_blur_card" | "hero_full_bleed" | "minimal_card"
        let showSponsorBadge: Bool?
        let showCountdown: Bool?
        let ctaPlacement: String?
        let videoPlacement: String?
    }
    
    struct Theme: Decodable {
        let accentHex: String?
        let surfaceStyle: String?
        let cornerRadius: Int?
    }
}
```

---

## レイアウト種別 (`renderHints.layout`)

| layout | 説明 | 推奨用途 |
|--------|------|---------|
| `hero_blur_card` | 背景にheroをぼかして敷く + 下にカード | 上品・鉄板 |
| `hero_full_bleed` | ヒーロー画像を全面に見せる | インパクト重視 |
| `minimal_card` | 画像なしでも成立 | B2B/硬いスポンサー |

---

## CTA開き方 (`openMode`)

| openMode | 動作 |
|----------|------|
| `safari` | 外部ブラウザ (Safari) で開く |
| `in_app` | SFSafariViewController で開く |
| `deeplink` | アプリを開く (失敗時は `fallbackUrl`) |

---

## イベントトラッキング: `POST /ads/events`

```json
{
  "event": "impression",
  "placement_id": "plc_abc123",
  "ad_id": "ad_demo_v2_001",
  "session_id": "sess_xxx",
  "job_id": "job_yyy",
  "ts_ms": 1705641234567,
  "meta": {}
}
```

**イベント種別:**
| event | タイミング |
|-------|-----------|
| `impression` | 広告表示開始 |
| `click` | CTAボタンタップ |
| `dismiss` | ユーザーが閉じた / タイムアウト |
| `heartbeat` | 10秒ごとに視聴継続中を報告 (任意) |

---

## 実装フロー

```
1. 要約/クイズ生成開始
2. GET /ads/placement (slot=summary_generating)
3. ad != null なら RichSponsoredLoadingView を表示
4. POST /ads/events (event=impression)
5. skippableAfterSec 経過後に × ボタン有効化
6. autodismissAtSec 経過で自動終了
7. CTAタップ → POST /ads/events (event=click) → openMode に従って開く
8. 閉じる → POST /ads/events (event=dismiss)
9. 生成完了を確認して結果画面へ遷移
```

---

## 注意事項

- **Premium判定**: サーバー側で plan != free なら `ad: null` を返します
- **動画優先**: `assets.video` があれば動画を表示、なければ `assets.hero` (画像)
- **CTAは2つまで**: `actions[0]` = primary, `actions[1]` = secondary
- **posterUrl必須推奨**: 動画ロード中のサムネイル表示用
