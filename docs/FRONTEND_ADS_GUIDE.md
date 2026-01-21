# Frontend Ad Implementation Guide (Branded Loading)

This guide details how to implement the "Branded Loading" ad format in the Classnote iOS app. This replaces the standard loading spinner with a branded experience during long-running tasks like Summary or Quiz generation.

## 1. Specification

- **Trigger**: Starts immediately when a Summary or Quiz generation job is initiated.
- **Duration**:
    - **Minimum**: 10 seconds (User cannot dismiss).
    - **Maximum**: 30 seconds (Auto-dismiss).
    - **Dismissal**:
        - Premium users: Immediate dismissal allowed (or no ad shown).
        - Free users: "Close" button enabled after 10s.
- **Interaction**:
    - Tap CTA -> Opens URL (SFSafariViewController or External).
    - Tap Close -> Returns to "Waiting" state (standard spinner).
- **Background**: Generation continues regardless of ad state.

## 2. API Integration

### Ad Service (`AdService.swift`)

Wait for the job to start, then request an ad.

```swift
import Foundation

struct SponsoredAd: Decodable, Identifiable {
    struct Creative: Decodable {
        let logoUrl: URL?
        let heroUrl: URL?
        let backgroundHex: String?
    }

    let id: String
    let placementId: String
    let sponsorName: String
    let headline: String
    let body: String?
    let ctaText: String
    let clickUrl: URL
    let creative: Creative
    let minViewSec: Int
    let maxViewSec: Int
}

struct BodyEvent: Encodable {
    let event: String
    let placement_id: String
    let ad_id: String
    let session_id: String
    let job_id: String?
    let ts_ms: Int
    let meta: [String: String]
}

final class AdService: ObservableObject {
    private let baseURL = URL(string: "https://classnote-api-900324644592.asia-northeast1.run.app")! // Use your actual API URL
    
    // Auth token provider (inject your auth logic)
    var tokenProvider: (() async throws -> String)?

    func fetchPlacement(slot: String, sessionId: String, jobId: String?) async throws -> SponsoredAd? {
        var comps = URLComponents(url: baseURL.appendingPathComponent("/ads/placement"), resolvingAgainstBaseURL: false)!
        comps.queryItems = [
            .init(name: "slot", value: slot),
            .init(name: "session_id", value: sessionId),
            .init(name: "job_id", value: jobId ?? "")
        ]
        
        var req = URLRequest(url: comps.url!)
        req.httpMethod = "GET"
        if let token = try? await tokenProvider?() {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }

        let (data, resp) = try await URLSession.shared.data(for: req)
        guard (resp as? HTTPURLResponse)?.statusCode == 200 else { return nil }

        struct PlacementResp: Decodable { let ad: SponsoredAd? }
        return try JSONDecoder().decode(PlacementResp.self, from: data).ad
    }
    
    func sendEvent(_ name: String, placementId: String, adId: String, sessionId: String, jobId: String?, meta: [String: String] = [:]) async {
        let url = baseURL.appendingPathComponent("/ads/events")
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        
        if let token = try? await tokenProvider?() {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }

        let body = BodyEvent(
            event: name,
            placement_id: placementId,
            ad_id: adId,
            session_id: sessionId,
            job_id: jobId,
            ts_ms: Int(Date().timeIntervalSince1970 * 1000),
            meta: meta
        )
        
        req.httpBody = try? JSONEncoder().encode(body)
        _ = try? await URLSession.shared.data(for: req)
    }
}
```

## 3. UI Component (`SponsoredLoadingView.swift`)

This view overlays the screen.

```swift
import SwiftUI

struct SponsoredLoadingView: View {
    let ad: SponsoredAd
    let onDismiss: (_ reason: String) -> Void
    let onClick: () -> Void

    @State private var elapsed: Int = 0
    @State private var timer: Timer?
    
    private var canClose: Bool { elapsed >= ad.minViewSec }
    private var remainingToClose: Int { max(ad.minViewSec - elapsed, 0) }

    var body: some View {
        ZStack {
            // Background
            if let hex = ad.creative.backgroundHex {
                Color(hex: hex).ignoresSafeArea()
            } else {
                Color(.systemBackground).ignoresSafeArea()
            }

            // Main Content
            VStack(spacing: 18) {
                // Header (Label)
                HStack {
                    Text("Sponsored")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                }
                .padding(.horizontal, 20)
                .padding(.top, 10)

                Spacer()

                // Spinner & Status
                VStack(spacing: 10) {
                    ProgressView()
                        .scaleEffect(1.5)
                    Text("要約を生成中…")
                        .font(.headline)
                    Text("AIが内容を分析しています")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                
                Spacer()

                // Sponsor Card
                VStack(alignment: .leading, spacing: 12) {
                    HStack(spacing: 10) {
                        // Logo (Placeholder or AsyncImage)
                        if let url = ad.creative.logoUrl {
                             AsyncImage(url: url) { phase in
                                if let image = phase.image {
                                    image.resizable().aspectRatio(contentMode: .fit)
                                } else {
                                    Color.gray.opacity(0.3)
                                }
                             }
                             .frame(width: 36, height: 36)
                             .clipShape(Circle())
                        }

                        VStack(alignment: .leading, spacing: 2) {
                            Text(ad.sponsorName)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Text(ad.headline)
                                .font(.title3.weight(.semibold))
                                .lineLimit(2)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer()
                    }

                    if let bodyText = ad.body {
                        Text(bodyText)
                            .font(.body)
                            .foregroundStyle(.primary.opacity(0.8))
                            .lineLimit(3)
                    }

                    Button(action: onClick) {
                        Text(ad.ctaText)
                            .font(.headline)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 12)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(.blue)
                }
                .padding(20)
                .background(.ultraThinMaterial)
                .clipShape(RoundedRectangle(cornerRadius: 24, style: .continuous))
                .shadow(color: .black.opacity(0.1), radius: 10, x: 0, y: 5)
                .padding(.horizontal, 16)
                .padding(.bottom, safetyBottom + 10)
            }

            // Close Button (Top Right)
            VStack {
                HStack {
                    Spacer()
                    Button {
                        if canClose { onDismiss("user_close") }
                    } label: {
                        ZStack {
                            Circle()
                                .fill(Color(.secondarySystemBackground))
                                .frame(width: 36, height: 36)
                            Image(systemName: "xmark")
                                .font(.system(size: 14, weight: .bold))
                                .foregroundStyle(canClose ? .primary : .secondary.opacity(0.5))
                        }
                    }
                    .disabled(!canClose)
                    .padding(.trailing, 16)
                    .padding(.top, 16)
                }

                if !canClose {
                    HStack {
                        Spacer()
                        Text("あと \(remainingToClose) 秒")
                            .font(.caption2)
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                            .padding(.trailing, 20)
                    }
                }
                Spacer()
            }
        }
        .onAppear {
            startTimer()
        }
        .onDisappear {
            timer?.invalidate()
        }
    }
    
    private func startTimer() {
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in
            elapsed += 1
            if elapsed >= ad.maxViewSec {
                onDismiss("timeout")
            }
        }
    }
    
    private var safetyBottom: CGFloat {
        if #available(iOS 11.0, *), let window = UIApplication.shared.windows.first {
            return window.safeAreaInsets.bottom
        }
        return 0
    }
}

// Hex Color Extension helper
extension Color {
    init(hex: String) {
        let hex = hex.trimmingCharacters(in: CharacterSet.alphanumerics.inverted)
        var int: UInt64 = 0
        Scanner(string: hex).scanHexInt64(&int)
        let a, r, g, b: UInt64
        switch hex.count {
        case 3: // RGB (12-bit)
            (a, r, g, b) = (255, (int >> 8) * 17, (int >> 4 & 0xF) * 17, (int & 0xF) * 17)
        case 6: // RGB (24-bit)
            (a, r, g, b) = (255, int >> 16, int >> 8 & 0xFF, int & 0xFF)
        case 8: // ARGB (32-bit)
            (a, r, g, b) = (int >> 24, int >> 16 & 0xFF, int >> 8 & 0xFF, int & 0xFF)
        default:
            (a, r, g, b) = (1, 1, 1, 0)
        }
        self.init(
            .sRGB,
            red: Double(r) / 255,
            green: Double(g) / 255,
            blue: Double(b) / 255,
            opacity: Double(a) / 255
        )
    }
}
```

## 4. Integration Logic (`SessionDetailScreen` or `ViewModel`)

Update your `triggerSummary` function to handle the ad flow.

```swift
func triggerSummary() {
    Task {
        // 1. Start Job (Existing Logic - Non-blocking)
        let jobId = await apiClient.startSummaryJob(sessionId: session.id)
        
        // 2. Fetch Ad (Parallel)
        if let ad = try? await adService.fetchPlacement(slot: "summary_generating", sessionId: session.id, jobId: jobId) {
            // Show Ad
            self.presentedAd = ad
            await adService.sendEvent("impression", placementId: ad.placementId, adId: ad.id, sessionId: session.id, jobId: jobId)
        } else {
            // No Ad -> Show Standard Helper/Loading
        }
    }
}
```
