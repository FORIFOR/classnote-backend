# Client implementation skeleton — Desktop (Tauri/React) + iOS (SwiftUI)

本書は backend の `/v1/chat*` contract v2 (docs/ai-chat-contract-v2.md) を
クライアント側で消費するための**そのまま動く実装骨格**です。別リポジトリ
(`deepnote-desktop`, `ClassnoteX`) で使うコードなので、本リポジトリには
置かず docs として提供します。

## 1. Desktop (React / Tauri + Zustand)

### 1-1. ディレクトリ構成

```
src/
├─ features/chat/
│  ├─ api/
│  │  ├─ streamChat.ts
│  │  ├─ executeAction.ts
│  │  ├─ createConversation.ts
│  │  └─ types.ts
│  ├─ hooks/
│  │  ├─ useSessionAIChat.ts
│  │  └─ useChatStream.ts
│  ├─ stores/
│  │  └─ sessionAIChatStore.ts
│  └─ components/
│     ├─ SessionAIPanel.tsx
│     ├─ ChatMessageList.tsx
│     ├─ ChatInputBar.tsx
│     ├─ CitationChips.tsx
│     └─ ActionButtonRow.tsx
```

### 1-2. `features/chat/api/types.ts`

```ts
export type ChatScope =
  | { type: "session"; sessionId: string }
  | { type: "general" }
  | { type: "multi_session"; sessionIds: string[] }
  | { type: "overlay_live"; sessionId?: string };

export type ChatSurface =
  | "desktop_session_detail"
  | "ios_session_detail"
  | "global_chat"
  | "overlay";

export type ChatTab = "overview" | "transcript" | "notes" | "quiz";

export interface ChatClientContext {
  surface: ChatSurface;
  activeTab?: ChatTab;
  selectedText?: string | null;
  selectedEvidenceId?: string | null;
  selectedSegmentId?: string | null;
  currentPlaybackMs?: number | null;
}

export interface ChatRequest {
  conversationId?: string;
  scope: ChatScope;
  message: { text: string };
  clientContext?: ChatClientContext;
  responseMode?: "default" | "concise" | "structured" | "rewrite" | "coaching";
  preset?:
    | "summarize" | "extract_todos" | "extract_decisions"
    | "next_agenda" | "short_share" | "quiz_questions";
  idempotencyKey?: string;
}

export type AnswerBlock =
  | { type: "paragraph"; text: string }
  | { type: "bullet_list"; items: string[] }
  | { type: "numbered_list"; items: string[] }
  | { type: "section"; title: string; body: string }
  | { type: "warning"; text: string };

export type Citation =
  | { type: "transcript"; segmentId?: string; startMs?: number; endMs?: number;
      speaker?: string; quotePreview?: string; score?: number }
  | { type: "summary_evidence"; evidenceId: string; label: string;
      startMs?: number; endMs?: number; quotePreview?: string }
  | { type: "note"; noteId: string; label: string }
  | { type: "web"; title: string; url: string; snippet?: string; publishedAt?: string };

export type ChatAction =
  | { type: "jump_to_transcript"; targetMs: number; segmentId?: string }
  | { type: "save_as_note"; payload: { text: string } }
  | { type: "create_todo"; payload: { text: string; owner?: string; due?: string } }
  | { type: "copy_answer" }
  | { type: "rewrite_answer"; mode: "slack" | "email" | "summary" };

export interface AssistantMessage {
  messageId: string;
  answer: { text: string; blocks: AnswerBlock[] };
  citations: Citation[];
  actions: ChatAction[];
  confidence: "high" | "medium" | "low";
}

export type SSEEvent =
  | { event: "meta"; data: { conversationId: string; messageId: string; scope: ChatScope;
      intent: string; mode: string; usedModel: string;
      creditCost: number; creditsRemaining?: number } }
  | { event: "status"; data: { phase: "routing" | "retrieving" | "generating" } }
  | { event: "delta"; data: { text: string } }
  | { event: "token"; data: { text: string } }                     // v1 compat (ignore if using delta)
  | { event: "citation"; data: { citation: Citation } }
  | { event: "action"; data: { action: ChatAction } }
  | { event: "message"; data: { message: AssistantMessage } }
  | { event: "done"; data: { conversationId: string; messageId: string;
      answer: AssistantMessage["answer"]; citations: Citation[]; actions: ChatAction[];
      confidence: AssistantMessage["confidence"]; latencyMs: number; intent: string;
      creditCost: number; creditsRemaining?: number } }
  | { event: "error"; data: { code: string; message: string; details?: unknown } };
```

### 1-3. `streamChat.ts` — SSE 受信

```ts
import type { ChatRequest, SSEEvent } from "./types";

export async function streamChat(
  baseUrl: string,
  token: string,
  request: ChatRequest,
  handlers: {
    onEvent: (e: SSEEvent) => void;
    onError?: (e: Error) => void;
    signal?: AbortSignal;
  },
): Promise<void> {
  const res = await fetch(`${baseUrl}/v1/chat:stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(request),
    signal: handlers.signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(`chat stream failed: ${res.status}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";

    for (const frame of frames) {
      let eventName = "message";
      const dataLines: string[] = [];
      for (const raw of frame.split("\n")) {
        if (raw.startsWith("event: ")) eventName = raw.slice(7).trim();
        else if (raw.startsWith("data: ")) dataLines.push(raw.slice(6));
      }
      if (!dataLines.length) continue;
      try {
        const data = JSON.parse(dataLines.join("\n"));
        handlers.onEvent({ event: eventName, data } as SSEEvent);
      } catch (err) {
        handlers.onError?.(err as Error);
      }
    }
  }
}
```

### 1-4. `executeAction.ts`

```ts
import type { ChatAction } from "./types";

export async function executeChatAction(
  baseUrl: string,
  token: string,
  body: {
    action: ChatAction;
    sessionId?: string;
    conversationId?: string;
    messageId?: string;
  },
): Promise<{ action: ChatAction; result: Record<string, unknown>; messageId?: string }> {
  const res = await fetch(`${baseUrl}/v1/chat/actions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(`action failed: ${(err as any)?.error?.code ?? res.status}`);
  }
  return res.json();
}
```

### 1-5. Zustand store

```ts
// stores/sessionAIChatStore.ts
import { create } from "zustand";
import type { AnswerBlock, ChatAction, Citation } from "../api/types";

export type MessageVM =
  | { id: string; role: "user"; text: string; createdAt?: string }
  | {
      id: string;
      role: "assistant";
      streamingText?: string;
      blocks?: AnswerBlock[];
      citations: Citation[];
      actions: ChatAction[];
      confidence?: "high" | "medium" | "low";
      createdAt?: string;
    };

interface State {
  conversationId: string | null;
  messages: MessageVM[];
  isStreaming: boolean;
  statusPhase?: "routing" | "retrieving" | "generating";
  pendingAssistantId: string | null;

  setConversationId(id: string): void;
  pushUser(text: string): void;
  beginAssistant(messageId: string): void;
  appendDelta(text: string): void;
  pushCitation(c: Citation): void;
  pushAction(a: ChatAction): void;
  completeAssistant(m: {
    messageId: string; answer: { blocks: AnswerBlock[] };
    citations: Citation[]; actions: ChatAction[];
    confidence: "high" | "medium" | "low";
  }): void;
  setStatus(p?: "routing" | "retrieving" | "generating"): void;
  setStreaming(v: boolean): void;
  reset(): void;
}

export const useSessionAIChatStore = create<State>((set) => ({
  conversationId: null,
  messages: [],
  isStreaming: false,
  pendingAssistantId: null,

  setConversationId: (conversationId) => set({ conversationId }),

  pushUser: (text) =>
    set((s) => ({ messages: [...s.messages, { id: `user-${Date.now()}`, role: "user", text }] })),

  beginAssistant: (messageId) =>
    set((s) => ({
      pendingAssistantId: messageId,
      isStreaming: true,
      messages: [
        ...s.messages,
        { id: messageId, role: "assistant", streamingText: "", citations: [], actions: [] },
      ],
    })),

  appendDelta: (text) =>
    set((s) => ({
      messages: s.messages.map((m) =>
        m.role === "assistant" && m.id === s.pendingAssistantId
          ? { ...m, streamingText: `${m.streamingText ?? ""}${text}` }
          : m
      ),
    })),

  pushCitation: (c) =>
    set((s) => ({
      messages: s.messages.map((m) =>
        m.role === "assistant" && m.id === s.pendingAssistantId
          ? { ...m, citations: [...m.citations, c] }
          : m
      ),
    })),

  pushAction: (a) =>
    set((s) => ({
      messages: s.messages.map((m) =>
        m.role === "assistant" && m.id === s.pendingAssistantId
          ? { ...m, actions: [...m.actions, a] }
          : m
      ),
    })),

  completeAssistant: (payload) =>
    set((s) => ({
      isStreaming: false,
      pendingAssistantId: null,
      messages: s.messages.map((m) =>
        m.role === "assistant" && m.id === payload.messageId
          ? {
              id: payload.messageId, role: "assistant",
              blocks: payload.answer.blocks,
              citations: payload.citations,
              actions: payload.actions,
              confidence: payload.confidence,
            }
          : m
      ),
    })),

  setStatus: (statusPhase) => set({ statusPhase }),
  setStreaming: (isStreaming) => set({ isStreaming }),
  reset: () => set({ conversationId: null, messages: [], isStreaming: false, pendingAssistantId: null }),
}));
```

### 1-6. `useSessionAIChat` hook

```ts
// hooks/useSessionAIChat.ts
import { useCallback, useMemo, useRef } from "react";
import { v4 as uuid } from "uuid";
import { streamChat } from "../api/streamChat";
import { executeChatAction } from "../api/executeAction";
import { useSessionAIChatStore } from "../stores/sessionAIChatStore";
import type { ChatAction, ChatRequest, SSEEvent } from "../api/types";

interface Args {
  sessionId: string;
  activeTab: "overview" | "transcript" | "notes" | "quiz";
  currentPlaybackMs?: number;
  baseUrl: string;
  getToken: () => Promise<string>;
  onJumpToTranscript?: (targetMs: number, segmentId?: string) => void;
}

export function useSessionAIChat(args: Args) {
  const store = useSessionAIChatStore();
  const abortRef = useRef<AbortController | null>(null);

  const handleEvent = useCallback((ev: SSEEvent) => {
    switch (ev.event) {
      case "status":
        store.setStatus(ev.data.phase);
        break;
      case "meta":
        store.setConversationId(ev.data.conversationId);
        store.beginAssistant(ev.data.messageId);
        break;
      case "delta":
        store.appendDelta(ev.data.text);
        break;
      case "token":
        // v1 compat — ignore when `delta` is already consumed
        break;
      case "citation":
        store.pushCitation(ev.data.citation);
        break;
      case "action":
        store.pushAction(ev.data.action);
        break;
      case "message":
        store.completeAssistant(ev.data.message);
        break;
      case "done":
        store.setStreaming(false);
        store.setStatus(undefined);
        break;
      case "error":
        store.setStreaming(false);
        store.setStatus(undefined);
        console.error("chat error", ev.data);
        break;
    }
  }, [store]);

  const sendMessage = useCallback(async (text: string, preset?: ChatRequest["preset"]) => {
    store.pushUser(text);
    const token = await args.getToken();

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    const request: ChatRequest = {
      conversationId: store.conversationId ?? undefined,
      scope: { type: "session", sessionId: args.sessionId },
      message: { text },
      preset,
      clientContext: {
        surface: "desktop_session_detail",
        activeTab: args.activeTab,
        currentPlaybackMs: args.currentPlaybackMs ?? null,
      },
      responseMode: "default",
      idempotencyKey: `chat-${uuid()}`,
    };

    try {
      await streamChat(args.baseUrl, token, request, {
        onEvent: handleEvent,
        signal: controller.signal,
        onError: (e) => console.error(e),
      });
    } catch (e) {
      console.error(e);
      store.setStreaming(false);
    }
  }, [args, handleEvent, store]);

  const dispatchAction = useCallback(async (action: ChatAction, messageId?: string) => {
    // Client-side actions — don't hit the server
    if (action.type === "copy_answer") {
      const msg = store.messages.find((m) => m.role === "assistant" && m.id === messageId);
      if (msg && msg.role === "assistant") {
        const text = msg.blocks
          ? msg.blocks.map(blockToText).join("\n\n")
          : msg.streamingText ?? "";
        await navigator.clipboard.writeText(text);
      }
      return;
    }
    if (action.type === "jump_to_transcript") {
      args.onJumpToTranscript?.(action.targetMs, action.segmentId);
      return;
    }

    // Server-side actions
    const token = await args.getToken();
    try {
      await executeChatAction(args.baseUrl, token, {
        action,
        sessionId: args.sessionId,
        conversationId: store.conversationId ?? undefined,
        messageId,
      });
    } catch (e) {
      console.error(e);
    }
  }, [args, store]);

  const api = useMemo(() => ({
    conversationId: store.conversationId,
    messages: store.messages,
    isStreaming: store.isStreaming,
    statusPhase: store.statusPhase,
    sendMessage,
    dispatchAction,
    stop: () => abortRef.current?.abort(),
  }), [store, sendMessage, dispatchAction]);

  return api;
}

function blockToText(b: { type: string } & Record<string, any>): string {
  switch (b.type) {
    case "paragraph": return b.text;
    case "bullet_list":
    case "numbered_list": return (b.items ?? []).map((i: string) => `- ${i}`).join("\n");
    case "section": return `${b.title}\n${b.body}`;
    case "warning": return `⚠ ${b.text}`;
    default: return "";
  }
}
```

### 1-7. `SessionAIPanel` UI

```tsx
import React, { useState } from "react";
import { useSessionAIChat } from "../hooks/useSessionAIChat";

const QUICK = [
  { id: "summarize", label: "要点整理" },
  { id: "extract_todos", label: "TODO抽出" },
  { id: "extract_decisions", label: "決定事項" },
  { id: "next_agenda", label: "次回アジェンダ" },
  { id: "short_share", label: "Slack用" },
  { id: "quiz_questions", label: "理解度チェック" },
] as const;

export function SessionAIPanel(props: {
  sessionId: string; activeTab: "overview" | "transcript" | "notes" | "quiz";
  currentPlaybackMs?: number; baseUrl: string; getToken: () => Promise<string>;
  onJumpToTranscript?: (ms: number) => void;
}) {
  const [input, setInput] = useState("");
  const chat = useSessionAIChat(props);

  return (
    <div className="flex h-full flex-col gap-3">
      <div className="flex flex-wrap gap-2">
        {QUICK.map((q) => (
          <button key={q.id} className="rounded-full border px-3 py-1 text-xs"
            onClick={() => void chat.sendMessage("", q.id as any)}>
            {q.label}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-auto rounded-2xl border p-3 space-y-4">
        {chat.messages.map((m) =>
          m.role === "user" ? (
            <div key={m.id} className="rounded-2xl bg-zinc-800 p-3 text-sm">{m.text}</div>
          ) : (
            <div key={m.id} className="rounded-2xl bg-zinc-900 p-3 text-sm">
              {m.streamingText ? (
                <div className="whitespace-pre-wrap">{m.streamingText}</div>
              ) : (
                <>
                  {m.blocks?.map((b, i) => renderBlock(b, i))}
                  {m.citations.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-2">
                      {m.citations.map((c, i) => (
                        <CitationChip key={i} citation={c}
                          onJump={(ms) => chat.dispatchAction(
                            { type: "jump_to_transcript", targetMs: ms },
                            m.id,
                          )}
                        />
                      ))}
                    </div>
                  )}
                  {m.actions.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-2">
                      {m.actions.map((a, i) => (
                        <ActionButton key={i} action={a}
                          onClick={() => void chat.dispatchAction(a, m.id)} />
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>
          )
        )}
      </div>

      {chat.statusPhase && <div className="text-xs text-zinc-500">{chat.statusPhase}...</div>}

      <form className="flex gap-2" onSubmit={(e) => {
        e.preventDefault();
        if (!input.trim()) return;
        void chat.sendMessage(input.trim());
        setInput("");
      }}>
        <input className="flex-1 rounded-2xl border bg-transparent px-4 py-3 text-sm"
          value={input} onChange={(e) => setInput(e.target.value)}
          placeholder="このセッションについて質問" />
        <button className="rounded-2xl border px-4 py-3 text-sm"
          disabled={chat.isStreaming} type="submit">送信</button>
      </form>
    </div>
  );
}

function renderBlock(b: any, i: number) {
  if (b.type === "paragraph") return <p key={i} className="mb-2">{b.text}</p>;
  if (b.type === "bullet_list") return (
    <ul key={i} className="ml-5 list-disc">{b.items.map((it: string) => <li key={it}>{it}</li>)}</ul>
  );
  if (b.type === "numbered_list") return (
    <ol key={i} className="ml-5 list-decimal">{b.items.map((it: string) => <li key={it}>{it}</li>)}</ol>
  );
  if (b.type === "section") return (
    <div key={i} className="mb-2">
      <div className="font-semibold">{b.title}</div>
      <div className="mt-1 text-sm">{b.body}</div>
    </div>
  );
  if (b.type === "warning") return (
    <div key={i} className="mb-2 rounded-xl bg-amber-950/40 p-2 text-amber-200 text-xs">
      {b.text}
    </div>
  );
  return null;
}

function CitationChip({ citation, onJump }: { citation: any; onJump: (ms: number) => void }) {
  const label = citation.label ?? (citation.startMs != null ? msToLabel(citation.startMs) : "根拠");
  const canJump = citation.type === "transcript" && typeof citation.startMs === "number";
  return (
    <button className="rounded-full border px-2 py-0.5 text-xs"
      onClick={canJump ? () => onJump(citation.startMs) : undefined}>
      {label}
    </button>
  );
}

function ActionButton({ action, onClick }: { action: any; onClick: () => void }) {
  const label = {
    jump_to_transcript: "トランスクリプトへ",
    save_as_note: "ノートに保存",
    create_todo: "TODOにする",
    copy_answer: "コピー",
    rewrite_answer: "言い換え",
  }[action.type as string] ?? action.type;
  return (
    <button className="rounded-xl border px-3 py-1 text-xs" onClick={onClick}>
      {label}
    </button>
  );
}

function msToLabel(ms: number): string {
  const s = Math.floor(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
```

---

## 2. iOS (SwiftUI)

### 2-1. ディレクトリ構成

```
Features/SessionAIChat/
├─ SessionAIChatModels.swift
├─ SessionAIChatAPI.swift
├─ SessionAIChatSSEClient.swift
├─ SessionAIChatStore.swift
├─ SessionAIChatSheet.swift
└─ SessionAIMessageRow.swift
```

### 2-2. `SessionAIChatModels.swift`

```swift
import Foundation

enum SessionAIScope {
    case session(id: String)
    case general
    case multiSession(ids: [String])
    case overlayLive(id: String?)
}

enum SessionAIBlock: Decodable, Identifiable {
    case paragraph(text: String)
    case bulletList(items: [String])
    case numberedList(items: [String])
    case section(title: String, body: String)
    case warning(text: String)
    case unknown

    var id: UUID { UUID() }

    private enum CodingKeys: String, CodingKey { case type, text, items, title, body }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let t = try c.decode(String.self, forKey: .type)
        switch t {
        case "paragraph":     self = .paragraph(text: (try? c.decode(String.self, forKey: .text)) ?? "")
        case "bullet_list":   self = .bulletList(items: (try? c.decode([String].self, forKey: .items)) ?? [])
        case "numbered_list": self = .numberedList(items: (try? c.decode([String].self, forKey: .items)) ?? [])
        case "section":
            self = .section(
                title: (try? c.decode(String.self, forKey: .title)) ?? "",
                body: (try? c.decode(String.self, forKey: .body)) ?? ""
            )
        case "warning":       self = .warning(text: (try? c.decode(String.self, forKey: .text)) ?? "")
        default:              self = .unknown
        }
    }
}

struct SessionAICitation: Decodable, Identifiable {
    let id: String
    let type: String
    let segmentId: String?
    let startMs: Int?
    let endMs: Int?
    let speaker: String?
    let quotePreview: String?
    let label: String?

    enum CodingKeys: String, CodingKey {
        case type, segmentId, startMs, endMs, speaker, quotePreview, label
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.type = try c.decode(String.self, forKey: .type)
        self.segmentId = try? c.decode(String.self, forKey: .segmentId)
        self.startMs = try? c.decode(Int.self, forKey: .startMs)
        self.endMs = try? c.decode(Int.self, forKey: .endMs)
        self.speaker = try? c.decode(String.self, forKey: .speaker)
        self.quotePreview = try? c.decode(String.self, forKey: .quotePreview)
        self.label = try? c.decode(String.self, forKey: .label)
        self.id = self.segmentId ?? "\(self.type)-\(UUID().uuidString)"
    }
}

enum SessionAIAction: Decodable, Identifiable {
    case jumpToTranscript(targetMs: Int, segmentId: String?)
    case saveAsNote(text: String)
    case createTodo(text: String, owner: String?, due: String?)
    case copyAnswer
    case rewriteAnswer(mode: String)
    case unknown

    var id: UUID { UUID() }

    private enum CodingKeys: String, CodingKey { case type, targetMs, segmentId, payload, mode }
    private struct Payload: Decodable { let text: String?; let owner: String?; let due: String? }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let t = try c.decode(String.self, forKey: .type)
        switch t {
        case "jump_to_transcript":
            self = .jumpToTranscript(
                targetMs: (try? c.decode(Int.self, forKey: .targetMs)) ?? 0,
                segmentId: try? c.decode(String.self, forKey: .segmentId)
            )
        case "save_as_note":
            let p = try? c.decode(Payload.self, forKey: .payload)
            self = .saveAsNote(text: p?.text ?? "")
        case "create_todo":
            let p = try? c.decode(Payload.self, forKey: .payload)
            self = .createTodo(text: p?.text ?? "", owner: p?.owner, due: p?.due)
        case "copy_answer":
            self = .copyAnswer
        case "rewrite_answer":
            self = .rewriteAnswer(mode: (try? c.decode(String.self, forKey: .mode)) ?? "summary")
        default:
            self = .unknown
        }
    }
}

struct SessionAIMessageState: Identifiable {
    let id: String
    let role: Role
    var userText: String?
    var streamingText: String?
    var blocks: [SessionAIBlock] = []
    var citations: [SessionAICitation] = []
    var actions: [SessionAIAction] = []
    var confidence: String?
    enum Role { case user, assistant }
}
```

### 2-3. `SessionAIChatSSEClient.swift`

```swift
import Foundation

final class SessionAIChatSSEClient {
    struct Event { let event: String; let data: Data }

    func stream(request: URLRequest, onEvent: @escaping (Event) -> Void) async throws {
        let (bytes, response) = try await URLSession.shared.bytes(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }

        var currentEvent = "message"
        var dataLines: [String] = []

        for try await line in bytes.lines {
            if line.isEmpty {
                if !dataLines.isEmpty, let data = dataLines.joined(separator: "\n").data(using: .utf8) {
                    onEvent(.init(event: currentEvent, data: data))
                }
                currentEvent = "message"; dataLines.removeAll()
                continue
            }
            if line.hasPrefix("event: ") { currentEvent = String(line.dropFirst(7)) }
            else if line.hasPrefix("data: ") { dataLines.append(String(line.dropFirst(6))) }
        }
    }
}
```

### 2-4. `SessionAIChatAPI.swift`

```swift
import Foundation

struct SessionAIChatAPI {
    let baseURL: URL
    let tokenProvider: () async throws -> String

    func makeStreamRequest(
        sessionId: String,
        conversationId: String?,
        text: String,
        preset: String?,
        activeTab: String,
        currentPlaybackMs: Int?,
        idempotencyKey: String
    ) async throws -> URLRequest {
        let url = baseURL.appendingPathComponent("/v1/chat:stream")
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("Bearer \(try await tokenProvider())", forHTTPHeaderField: "Authorization")
        var body: [String: Any] = [
            "scope": ["type": "session", "sessionId": sessionId],
            "message": ["text": text],
            "clientContext": [
                "surface": "ios_session_detail",
                "activeTab": activeTab,
                "currentPlaybackMs": currentPlaybackMs as Any,
            ],
            "responseMode": "default",
            "idempotencyKey": idempotencyKey,
        ]
        if let cid = conversationId { body["conversationId"] = cid }
        if let p = preset { body["preset"] = p }
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        return req
    }

    func executeAction(
        action: [String: Any],
        sessionId: String?,
        conversationId: String?,
        messageId: String?
    ) async throws {
        let url = baseURL.appendingPathComponent("/v1/chat/actions")
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("Bearer \(try await tokenProvider())", forHTTPHeaderField: "Authorization")
        var body: [String: Any] = ["action": action]
        if let s = sessionId { body["sessionId"] = s }
        if let c = conversationId { body["conversationId"] = c }
        if let m = messageId { body["messageId"] = m }
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (_, response) = try await URLSession.shared.data(for: req)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
    }
}
```

### 2-5. `SessionAIChatStore.swift`

```swift
import Foundation
import SwiftUI

@MainActor
final class SessionAIChatStore: ObservableObject {
    @Published var conversationId: String?
    @Published var messages: [SessionAIMessageState] = []
    @Published var inputText: String = ""
    @Published var isStreaming: Bool = false
    @Published var statusPhase: String?

    private let api: SessionAIChatAPI
    private let sseClient = SessionAIChatSSEClient()
    private var currentTask: Task<Void, Never>?

    init(api: SessionAIChatAPI) { self.api = api }

    func send(sessionId: String, text: String, preset: String? = nil,
              activeTab: String = "overview", currentPlaybackMs: Int? = nil,
              onJumpToTranscript: ((Int) -> Void)? = nil) {
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || preset != nil else { return }

        messages.append(
            .init(id: "user-\(UUID().uuidString)", role: .user, userText: text)
        )
        currentTask?.cancel()

        currentTask = Task { [weak self] in
            guard let self else { return }
            do {
                let req = try await self.api.makeStreamRequest(
                    sessionId: sessionId, conversationId: self.conversationId,
                    text: text, preset: preset, activeTab: activeTab,
                    currentPlaybackMs: currentPlaybackMs,
                    idempotencyKey: "chat-\(UUID().uuidString)"
                )
                self.isStreaming = true
                try await self.sseClient.stream(request: req) { [weak self] ev in
                    Task { @MainActor in
                        self?.handle(ev, onJumpToTranscript: onJumpToTranscript)
                    }
                }
            } catch {
                self.isStreaming = false
                self.statusPhase = "error"
            }
        }
    }

    func dispatchAction(_ action: SessionAIAction, messageId: String?, sessionId: String?,
                        onJumpToTranscript: ((Int) -> Void)? = nil) {
        switch action {
        case .jumpToTranscript(let ms, _):
            onJumpToTranscript?(ms)
        case .copyAnswer:
            if let msg = messages.first(where: { $0.id == messageId }) {
                let text = msg.blocks.map(self.blockText).joined(separator: "\n\n")
                UIPasteboard.general.string = text
            }
        case .saveAsNote(let text):
            Task {
                try? await api.executeAction(
                    action: ["type": "save_as_note", "payload": ["text": text]],
                    sessionId: sessionId, conversationId: conversationId, messageId: messageId
                )
            }
        case .createTodo(let text, let owner, let due):
            var payload: [String: Any] = ["text": text]
            if let o = owner { payload["owner"] = o }
            if let d = due { payload["due"] = d }
            Task {
                try? await api.executeAction(
                    action: ["type": "create_todo", "payload": payload],
                    sessionId: sessionId, conversationId: conversationId, messageId: messageId
                )
            }
        case .rewriteAnswer(let mode):
            let preset = mode == "summary" ? "summarize" : "short_share"
            if let sid = sessionId {
                send(sessionId: sid, text: "", preset: preset)
            }
        case .unknown:
            break
        }
    }

    // MARK: - SSE handler

    private func handle(_ ev: SessionAIChatSSEClient.Event, onJumpToTranscript: ((Int) -> Void)?) {
        guard let json = try? JSONSerialization.jsonObject(with: ev.data) as? [String: Any] else { return }
        switch ev.event {
        case "status":
            statusPhase = json["phase"] as? String
        case "meta":
            if let cid = json["conversationId"] as? String { conversationId = cid }
            if let mid = json["messageId"] as? String {
                messages.append(.init(id: mid, role: .assistant, streamingText: ""))
            }
        case "delta":
            if let text = json["text"] as? String,
               let idx = messages.lastIndex(where: { $0.role == .assistant }) {
                messages[idx].streamingText = (messages[idx].streamingText ?? "") + text
            }
        case "citation":
            if let obj = json["citation"],
               let data = try? JSONSerialization.data(withJSONObject: obj),
               let citation = try? JSONDecoder().decode(SessionAICitation.self, from: data),
               let idx = messages.lastIndex(where: { $0.role == .assistant }) {
                messages[idx].citations.append(citation)
            }
        case "action":
            if let obj = json["action"],
               let data = try? JSONSerialization.data(withJSONObject: obj),
               let action = try? JSONDecoder().decode(SessionAIAction.self, from: data),
               let idx = messages.lastIndex(where: { $0.role == .assistant }) {
                messages[idx].actions.append(action)
            }
        case "message":
            guard
                let msgObj = json["message"] as? [String: Any],
                let mid = msgObj["messageId"] as? String,
                let answerObj = msgObj["answer"] as? [String: Any],
                let blocksObj = answerObj["blocks"],
                let blocksData = try? JSONSerialization.data(withJSONObject: blocksObj),
                let blocks = try? JSONDecoder().decode([SessionAIBlock].self, from: blocksData)
            else { return }
            if let idx = messages.lastIndex(where: { $0.id == mid }) {
                messages[idx].blocks = blocks
                messages[idx].streamingText = nil
                messages[idx].confidence = msgObj["confidence"] as? String
                if let citsObj = msgObj["citations"],
                   let citsData = try? JSONSerialization.data(withJSONObject: citsObj),
                   let cits = try? JSONDecoder().decode([SessionAICitation].self, from: citsData) {
                    messages[idx].citations = cits
                }
                if let actsObj = msgObj["actions"],
                   let actsData = try? JSONSerialization.data(withJSONObject: actsObj),
                   let acts = try? JSONDecoder().decode([SessionAIAction].self, from: actsData) {
                    messages[idx].actions = acts
                }
            }
        case "done":
            isStreaming = false
            statusPhase = nil
        case "error":
            isStreaming = false
            statusPhase = "error"
        default: break
        }
    }

    private func blockText(_ b: SessionAIBlock) -> String {
        switch b {
        case .paragraph(let t): return t
        case .bulletList(let items): return items.map { "・\($0)" }.joined(separator: "\n")
        case .numberedList(let items):
            return items.enumerated().map { "\($0.offset + 1). \($0.element)" }.joined(separator: "\n")
        case .section(let title, let body): return "\(title)\n\(body)"
        case .warning(let t): return "⚠ \(t)"
        case .unknown: return ""
        }
    }
}
```

### 2-6. UI (抜粋)

```swift
struct SessionAIChatSheet: View {
    @StateObject var store: SessionAIChatStore
    let sessionId: String
    let activeTab: String
    let currentPlaybackMs: Int?
    let onJumpToTranscript: (Int) -> Void

    var body: some View {
        VStack(spacing: 12) {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    ForEach(store.messages) { m in
                        SessionAIMessageRow(
                            message: m,
                            onAction: { action in
                                store.dispatchAction(action, messageId: m.id,
                                                     sessionId: sessionId,
                                                     onJumpToTranscript: onJumpToTranscript)
                            }
                        )
                    }
                }
                .padding(.horizontal, 16)
            }

            if let s = store.statusPhase { Text(s).font(.caption2).foregroundStyle(.secondary) }

            HStack {
                TextField("このセッションについて質問", text: $store.inputText, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                Button("送信") {
                    let t = store.inputText
                    store.inputText = ""
                    store.send(sessionId: sessionId, text: t,
                               activeTab: activeTab, currentPlaybackMs: currentPlaybackMs,
                               onJumpToTranscript: onJumpToTranscript)
                }
                .disabled(store.isStreaming)
            }
            .padding(16)
        }
        .presentationDetents([.medium, .large])
    }
}
```

---

## 3. transcript jump と citation tap

### 3-1. 操作マップ

| 操作 | Desktop | iOS |
|---|---|---|
| citation chip tap | `dispatchAction({type:"jump_to_transcript", targetMs})` → `onJumpToTranscript(ms)` → Transcript タブ `seekTo(ms)` | `dispatchAction(.jumpToTranscript)` → `onJumpToTranscript` callback → ScrollViewReader で Transcript row に scroll + AVPlayer seek |
| action button (save_as_note) | `dispatchAction(action)` → `POST /v1/chat/actions` → notes 更新の listener で UI refresh | 同上 |
| action button (create_todo) | 同上 → `/todos` listener で TODO リストが自動更新 | 同上 |
| action button (copy_answer) | クライアントだけで完結 (`navigator.clipboard.writeText`) | `UIPasteboard.general.string` |
| action button (rewrite_answer) | `POST /v1/chat` を `responseMode=rewrite` + hinted preset で再発行 | 同上 |

### 3-2. Tauri / React: `useTranscriptJumpStore`

```ts
// src/stores/transcriptJumpStore.ts
import { create } from "zustand";

export interface JumpTarget {
  requestId: string;        // unique so the same ms twice re-fires the effect
  startSec: number;
  segmentId?: string;
}

interface State {
  pendingTarget: JumpTarget | null;
  requestJump(target: { startSec: number; segmentId?: string }): void;
  clearPending(): void;
}

export const useTranscriptJumpStore = create<State>((set) => ({
  pendingTarget: null,
  requestJump: (t) =>
    set({ pendingTarget: { requestId: crypto.randomUUID(), ...t } }),
  clearPending: () => set({ pendingTarget: null }),
}));
```

```ts
// src/features/chat/handlers/handleChatAction.ts
import type { ChatAction, Citation } from "../api/types";
import { useTranscriptJumpStore } from "@/stores/transcriptJumpStore";
import { useUIStore } from "@/stores/uiStore";

export function handleCitationClick(citation: Citation) {
  if (citation.type === "transcript" && typeof citation.startMs === "number") {
    useUIStore.getState().setSessionDetailActiveTab?.("transcript");
    useTranscriptJumpStore.getState().requestJump({
      startSec: Math.floor(citation.startMs / 1000),
      segmentId: citation.segmentId,
    });
    return;
  }
  if (citation.type === "summary_evidence" && typeof citation.startMs === "number") {
    useUIStore.getState().setSessionDetailActiveTab?.("transcript");
    useTranscriptJumpStore.getState().requestJump({
      startSec: Math.floor(citation.startMs / 1000),
    });
  }
}

export function handleChatAction(action: ChatAction) {
  if (action.type === "jump_to_transcript") {
    useUIStore.getState().setSessionDetailActiveTab?.("transcript");
    useTranscriptJumpStore.getState().requestJump({
      startSec: Math.floor(action.targetMs / 1000),
      segmentId: action.segmentId,
    });
  }
}
```

```tsx
// src/features/session/TranscriptTab.tsx — listener side
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranscriptJumpStore } from "@/stores/transcriptJumpStore";

interface Segment { id: string; startSec: number; text: string; }

export function TranscriptTab({ segments }: { segments: Segment[] }) {
  const pending = useTranscriptJumpStore((s) => s.pendingTarget);
  const clearPending = useTranscriptJumpStore((s) => s.clearPending);
  const [highlighted, setHighlighted] = useState<string | null>(null);
  const refs = useRef<Record<string, HTMLDivElement | null>>({});

  const targetId = useMemo(() => {
    if (!pending) return null;
    if (pending.segmentId) return pending.segmentId;
    let best: string | null = null, bestD = Infinity;
    for (const s of segments) {
      const d = Math.abs(s.startSec - pending.startSec);
      if (d < bestD) { bestD = d; best = s.id; }
    }
    return best;
  }, [pending, segments]);

  useEffect(() => {
    if (!pending || !targetId) return;
    refs.current[targetId]?.scrollIntoView({ behavior: "smooth", block: "center" });
    setHighlighted(targetId);
    const t = window.setTimeout(() => { setHighlighted(null); clearPending(); }, 1800);
    return () => window.clearTimeout(t);
  }, [pending, targetId, clearPending]);

  return (
    <div className="space-y-2">
      {segments.map((s) => (
        <div
          key={s.id}
          ref={(n) => { refs.current[s.id] = n; }}
          className={highlighted === s.id
            ? "rounded-xl border border-white/30 bg-white/10 p-3 transition"
            : "rounded-xl p-3"}
        >
          <div className="mb-1 text-xs text-zinc-500">{s.startSec}s</div>
          <div className="text-sm">{s.text}</div>
        </div>
      ))}
    </div>
  );
}
```

### 3-3. Rust 側から emit するパターン (optional)

AI panel が別ウィンドウにいる場合や overlay から jump させたい場合のみ使う。frontend 内で完結するなら不要。

```rust
// src-tauri/src/commands.rs
use serde::Serialize;
use tauri::{AppHandle, Emitter};

#[derive(Serialize, Clone)]
pub struct TranscriptJumpPayload {
    pub session_id: String,
    pub target_ms: u64,
    pub segment_id: Option<String>,
}

#[tauri::command]
pub fn emit_transcript_jump(
    app: AppHandle,
    session_id: String,
    target_ms: u64,
    segment_id: Option<String>,
) -> Result<(), String> {
    app.emit("transcript-jump", TranscriptJumpPayload { session_id, target_ms, segment_id })
       .map_err(|e| e.to_string())
}
```

```ts
// src/tauri/registerTranscriptJumpListener.ts
import { listen } from "@tauri-apps/api/event";
import { useTranscriptJumpStore } from "@/stores/transcriptJumpStore";
import { useUIStore } from "@/stores/uiStore";

export async function registerTranscriptJumpListener() {
  return listen<{ session_id: string; target_ms: number; segment_id?: string | null }>(
    "transcript-jump",
    (event) => {
      useUIStore.getState().setSessionDetailActiveTab?.("transcript");
      useTranscriptJumpStore.getState().requestJump({
        startSec: Math.floor(event.payload.target_ms / 1000),
        segmentId: event.payload.segment_id ?? undefined,
      });
    }
  );
}
```

### 3-4. SwiftUI: `SessionDetailTranscriptJumpStore`

```swift
// Features/SessionDetail/SessionDetailTranscriptJumpStore.swift
import Foundation

@MainActor
final class SessionDetailTranscriptJumpStore: ObservableObject {
    struct JumpTarget: Equatable {
        let requestId: UUID
        let targetSec: Double
        let segmentId: String?
    }

    @Published var pendingTarget: JumpTarget?

    func requestJump(targetSec: Double, segmentId: String?) {
        pendingTarget = JumpTarget(
            requestId: UUID(),
            targetSec: targetSec,
            segmentId: segmentId
        )
    }

    func clear() { pendingTarget = nil }
}
```

`SessionAIChatStore` に `jumpStore` を注入し、citation tap / action tap で `requestJump` を呼ぶ:

```swift
@MainActor
final class SessionAIChatStore: ObservableObject {
    // ... 既存 Published ...
    let jumpStore: SessionDetailTranscriptJumpStore
    private let api: SessionAIChatAPI
    private let sseClient = SessionAIChatSSEClient()

    init(api: SessionAIChatAPI, jumpStore: SessionDetailTranscriptJumpStore) {
        self.api = api; self.jumpStore = jumpStore
    }

    func handleCitationTap(_ citation: SessionAICitation) {
        if citation.type == "transcript" || citation.type == "summary_evidence" {
            let sec = Double(citation.startMs ?? 0) / 1000.0
            jumpStore.requestJump(targetSec: sec, segmentId: citation.segmentId)
        }
    }

    func handleActionTap(_ action: SessionAIAction) {
        if case .jumpToTranscript(let ms, let segId) = action {
            jumpStore.requestJump(targetSec: Double(ms) / 1000.0, segmentId: segId)
        }
    }
}
```

```swift
// Features/SessionDetail/TranscriptTabView.swift — scroll + highlight
import SwiftUI

struct TranscriptSegment: Identifiable, Equatable {
    let id: String
    let startSec: Double
    let text: String
}

struct TranscriptTabView: View {
    let segments: [TranscriptSegment]
    @ObservedObject var jumpStore: SessionDetailTranscriptJumpStore
    let onSeekToTime: (Double) -> Void

    @State private var highlighted: String?

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(segments) { seg in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(timeLabel(seg.startSec))
                                .font(.caption2).foregroundStyle(.secondary)
                            Text(seg.text).font(.body)
                        }
                        .padding(12)
                        .background(highlighted == seg.id
                                    ? Color.accentColor.opacity(0.15)
                                    : Color.clear)
                        .clipShape(RoundedRectangle(cornerRadius: 12))
                        .id(seg.id)
                        .onTapGesture { onSeekToTime(seg.startSec) }
                    }
                }
                .padding(16)
            }
            .onChange(of: jumpStore.pendingTarget) { _, target in
                guard let target, let dest = nearest(for: target) else { return }
                withAnimation(.easeInOut(duration: 0.25)) {
                    proxy.scrollTo(dest.id, anchor: .center)
                }
                onSeekToTime(target.targetSec)
                highlighted = dest.id
                Task {
                    try? await Task.sleep(for: .milliseconds(1800))
                    highlighted = nil
                    jumpStore.clear()
                }
            }
        }
    }

    private func nearest(for t: SessionDetailTranscriptJumpStore.JumpTarget) -> TranscriptSegment? {
        if let sid = t.segmentId {
            return segments.first(where: { $0.id == sid })
        }
        return segments.min(by: { abs($0.startSec - t.targetSec) < abs($1.startSec - t.targetSec) })
    }

    private func timeLabel(_ sec: Double) -> String {
        let total = Int(sec); return String(format: "%02d:%02d", total / 60, total % 60)
    }
}
```

### 3-5. SessionDetailScreen での接続

```swift
struct SessionDetailScreen: View {
    @StateObject private var jumpStore = SessionDetailTranscriptJumpStore()
    @State private var selectedTab: DetailTab = .overview
    @State private var showAI = false

    let transcriptSegments: [TranscriptSegment]
    let api: SessionAIChatAPI
    let sessionId: String

    var body: some View {
        VStack {
            // ...header / tabs...
            switch selectedTab {
            case .transcript:
                TranscriptTabView(
                    segments: transcriptSegments,
                    jumpStore: jumpStore,
                    onSeekToTime: { _ in /* AVPlayer seek */ }
                )
            default: EmptyView()
            }
        }
        .sheet(isPresented: $showAI) {
            SessionAIChatSheet(
                store: SessionAIChatStore(api: api, jumpStore: jumpStore),
                sessionId: sessionId,
                activeTab: selectedTab.rawValue,
                currentPlaybackMs: nil,
                onJumpToTranscript: { selectedTab = .transcript }
            )
        }
    }
}
```

**ポイント**:
- `jumpStore` は **SessionDetailScreen 直下で 1 つだけ生成**し、`SessionAIChatSheet` 内の store にも同じインスタンスを渡す。
- citation tap → `SessionAIChatStore.handleCitationTap` → `jumpStore.requestJump` → `TranscriptTabView.onChange(of: pendingTarget)` → `ScrollViewReader.scrollTo` + `AVPlayer.seek`
- `sheet` closed 中でも `jumpStore` は alive なので、sheet を閉じてから transcript の jump を完了させることも可能。

## 4. Folder picker + Conversation Highlights (Phase 7.9)

### 4-1. Backend 契約

新規エンドポイント (`/folders` 系は既存、`create-and-assign` のみ新規):

```
POST /folders                                        # 既存 — create
GET  /folders                                        # 既存 — list (account-aware)
PATCH /folders/{folderId}                            # 既存
DELETE /folders/{folderId}                           # 既存
GET  /folders/{folderId}/sessions                    # 既存
PUT  /sessions/{session_id}/organization             # 既存 — assign
GET  /sessions/{session_id}/organization             # 既存
POST /sessions/{session_id}/folder:create-and-assign # ★ 新規 — atomic 作成+割当
```

`create-and-assign` request/response:
```jsonc
// POST /sessions/{session_id}/folder:create-and-assign
{ "name": "4月定例", "color": null }
// ← 200
{
  "ok": true,
  "folder": { "id": "fld_xxx", "name": "4月定例", "color": null, ... },
  "sessionId": "sess_xxx",
  "folderId": "fld_xxx"
}
```

### 4-2. Summary v2 `conversationHighlights[]`

`/v1/session-details/{id}/overview` (既存) のレスポンスに今回から
`payload.conversationHighlights[]` が含まれます:

```jsonc
"conversationHighlights": [
  {
    "id": "hl_1",
    "text": "最近、家賃が一気に上がったという懸念が共有された。",
    "topic": "生活費",
    "importance": "high" | "medium" | "low",
    "primaryTimestampMs": 3469000,
    "segmentIds": ["seg_241"],
    "evidence": [
      { "segmentId": "seg_241", "startMs": 3469000, "endMs": 3478000, "quotePreview": "..." }
    ]
  }
]
```

- 生成は **summary 生成と同一 LLM コール**で行われる (`summary:generate`
  endpoint の副産物)。別 API は追加しない。
- `primaryTimestampMs` は **transcript segments 突合で backend が確定**
  した値を使う (LLM 生値は不採用)。
- `evidence[]` は **常に配列**、空の場合も `[]`。
- 件数は 5〜12 件程度 (prompt で指定)。

### 4-3. Tauri / React: Folder picker + highlights section

```tsx
// src/features/folders/api/folderApi.ts
import type { FolderDTO } from "./types";

export async function listFolders(token: string): Promise<FolderDTO[]> {
  const r = await fetch("/folders", { headers: { Authorization: `Bearer ${token}` } });
  return (await r.json()) as FolderDTO[];
}
export async function createFolderAndAssign(
  token: string, sessionId: string, name: string
): Promise<{ folder: FolderDTO; folderId: string }> {
  const r = await fetch(`/sessions/${sessionId}/folder:create-and-assign`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ name }),
  });
  if (!r.ok) throw new Error("failed");
  return (await r.json()) as any;
}
export async function assignSessionFolder(
  token: string, sessionId: string, folderId: string | null
) {
  const r = await fetch(`/sessions/${sessionId}/organization`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ folderId }),
  });
  if (!r.ok) throw new Error("failed");
}
```

```tsx
// src/features/folders/components/PostRecordingFolderModal.tsx
import React, { useEffect, useState } from "react";
import { listFolders, assignSessionFolder, createFolderAndAssign } from "../api/folderApi";

export function PostRecordingFolderModal(props: {
  sessionId: string;
  getToken: () => Promise<string>;
  onClose: () => void;
}) {
  const [folders, setFolders] = useState<any[]>([]);
  const [newName, setNewName] = useState("");

  useEffect(() => {
    void (async () => {
      const token = await props.getToken();
      setFolders(await listFolders(token));
    })();
  }, []);

  const handle = async (action: () => Promise<void>) => {
    try { await action(); props.onClose(); } catch (e) { console.error(e); }
  };

  return (
    <div className="rounded-3xl border bg-zinc-950 p-5 w-[420px]">
      <div className="mb-4 text-lg font-semibold">保存先フォルダ</div>
      <div className="mb-4 space-y-2 max-h-[320px] overflow-auto">
        <button className="w-full rounded-2xl border px-4 py-3 text-left text-sm"
          onClick={() => handle(async () => {
            await assignSessionFolder(await props.getToken(), props.sessionId, null);
          })}>
          未分類のまま
        </button>
        {folders.map((f) => (
          <button key={f.id} className="w-full rounded-2xl border px-4 py-3 text-left text-sm"
            onClick={() => handle(async () => {
              await assignSessionFolder(await props.getToken(), props.sessionId, f.id);
            })}>
            {f.name}
          </button>
        ))}
      </div>
      <div className="space-y-2">
        <input className="w-full rounded-2xl border bg-transparent px-4 py-3 text-sm"
          placeholder="新しいフォルダ名" value={newName}
          onChange={(e) => setNewName(e.target.value)} />
        <button className="w-full rounded-2xl border px-4 py-3 text-sm"
          disabled={!newName.trim()}
          onClick={() => handle(async () => {
            await createFolderAndAssign(await props.getToken(), props.sessionId, newName.trim());
          })}>
          作成して保存
        </button>
      </div>
    </div>
  );
}
```

```tsx
// src/features/session/ConversationHighlightsSection.tsx
import React from "react";
import { useTranscriptJumpStore } from "@/stores/transcriptJumpStore";
import { useUIStore } from "@/stores/uiStore";

export type ConvHighlight = {
  id: string;
  text: string;
  topic?: string;
  importance: "high" | "medium" | "low";
  primaryTimestampMs?: number;
  segmentIds?: string[];
};

function fmt(ms: number): string {
  const sec = Math.floor(ms / 1000);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h > 0
    ? `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

export function ConversationHighlightsSection({ items }: { items: ConvHighlight[] }) {
  if (!items || !items.length) return null;
  return (
    <div className="space-y-3">
      <div className="text-base font-semibold">会話ハイライト</div>
      {items.map((item) => (
        <div key={item.id} className="rounded-2xl border bg-white/5 p-4">
          <div className="flex gap-3">
            <div className={`mt-2 h-2 w-2 rounded-full ${
              item.importance === "high" ? "bg-rose-400"
              : item.importance === "medium" ? "bg-violet-400" : "bg-zinc-500"}`} />
            <div className="space-y-2 flex-1">
              {item.topic && (
                <div className="text-xs text-zinc-400">{item.topic}</div>
              )}
              <div className="text-sm leading-7 text-zinc-200">{item.text}</div>
              {item.primaryTimestampMs != null && (
                <button
                  className="text-sm font-semibold text-blue-400 underline"
                  onClick={() => {
                    useUIStore.getState().setSessionDetailActiveTab?.("transcript");
                    useTranscriptJumpStore.getState().requestJump({
                      startSec: Math.floor(item.primaryTimestampMs! / 1000),
                      segmentId: item.segmentIds?.[0],
                    });
                  }}
                >
                  {fmt(item.primaryTimestampMs)}
                </button>
              )}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
```

Summary タブ配置順 (推奨):
1. TL;DR / Decisions / Todos / KeyPoints (既存)
2. **会話ハイライト** ← 新セクション
3. Open Questions / Discussion Points / Terms / Formulas

### 4-4. SwiftUI: Folder picker + highlights section

```swift
// Features/Folders/FolderAPI.swift
import Foundation

struct FolderDTO: Decodable, Identifiable {
    let id: String
    let name: String
    let color: String?
}

struct CreateAndAssignResponse: Decodable {
    let folderId: String
    let folder: FolderDTO
}

final class FolderAPI {
    let baseURL: URL
    let tokenProvider: () async throws -> String
    init(baseURL: URL, tokenProvider: @escaping () async throws -> String) {
        self.baseURL = baseURL
        self.tokenProvider = tokenProvider
    }

    private func authed(_ path: String, method: String = "GET", body: Any? = nil) async throws -> URLRequest {
        var req = URLRequest(url: baseURL.appendingPathComponent(path))
        req.httpMethod = method
        req.setValue("Bearer \(try await tokenProvider())", forHTTPHeaderField: "Authorization")
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        return req
    }

    func list() async throws -> [FolderDTO] {
        let (data, _) = try await URLSession.shared.data(for: try await authed("/folders"))
        return try JSONDecoder().decode([FolderDTO].self, from: data)
    }

    func assign(sessionId: String, folderId: String?) async throws {
        _ = try await URLSession.shared.data(
            for: try await authed(
                "/sessions/\(sessionId)/organization",
                method: "PUT",
                body: ["folderId": folderId as Any]
            )
        )
    }

    func createAndAssign(sessionId: String, name: String) async throws -> CreateAndAssignResponse {
        let (data, _) = try await URLSession.shared.data(
            for: try await authed(
                "/sessions/\(sessionId)/folder:create-and-assign",
                method: "POST",
                body: ["name": name]
            )
        )
        return try JSONDecoder().decode(CreateAndAssignResponse.self, from: data)
    }
}
```

```swift
// Features/Folders/PostRecordingFolderPickerSheet.swift
import SwiftUI

@MainActor
final class PostRecordingFolderPickerStore: ObservableObject {
    @Published var folders: [FolderDTO] = []
    @Published var newFolderName: String = ""
    @Published var isLoading = false

    private let api: FolderAPI
    init(api: FolderAPI) { self.api = api }

    func load() async {
        isLoading = true
        defer { isLoading = false }
        folders = (try? await api.list()) ?? []
    }

    func assign(sessionId: String, folderId: String?) async {
        try? await api.assign(sessionId: sessionId, folderId: folderId)
    }

    func createAndAssign(sessionId: String) async {
        let name = newFolderName.trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty else { return }
        _ = try? await api.createAndAssign(sessionId: sessionId, name: name)
    }
}

struct PostRecordingFolderPickerSheet: View {
    @StateObject var store: PostRecordingFolderPickerStore
    let sessionId: String
    let onDone: () -> Void

    var body: some View {
        NavigationStack {
            List {
                Section("既存フォルダ") {
                    Button("未分類のまま") {
                        Task { await store.assign(sessionId: sessionId, folderId: nil); onDone() }
                    }
                    ForEach(store.folders) { folder in
                        Button(folder.name) {
                            Task { await store.assign(sessionId: sessionId, folderId: folder.id); onDone() }
                        }
                    }
                }
                Section("新規作成") {
                    TextField("新しいフォルダ名", text: $store.newFolderName)
                    Button("作成して保存") {
                        Task { await store.createAndAssign(sessionId: sessionId); onDone() }
                    }
                    .disabled(store.newFolderName.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .navigationTitle("保存先フォルダ")
            .task { await store.load() }
        }
    }
}
```

```swift
// Features/SessionDetail/ConversationHighlightsSection.swift
import SwiftUI

struct ConversationHighlightItem: Identifiable, Decodable {
    let id: String
    let text: String
    let topic: String?
    let importance: String
    let primaryTimestampMs: Int?
    let segmentIds: [String]?
}

struct ConversationHighlightsSection: View {
    let items: [ConversationHighlightItem]
    let onTapTimestamp: (Int, String?) -> Void

    var body: some View {
        if items.isEmpty { EmptyView() } else {
            VStack(alignment: .leading, spacing: 12) {
                Text("会話ハイライト").font(.headline)
                ForEach(items) { item in
                    HStack(alignment: .top, spacing: 10) {
                        Circle()
                            .fill(color(for: item.importance))
                            .frame(width: 8, height: 8)
                            .padding(.top, 6)
                        VStack(alignment: .leading, spacing: 8) {
                            if let t = item.topic {
                                Text(t).font(.caption2).foregroundStyle(.secondary)
                            }
                            Text(item.text).font(.body)
                            if let ms = item.primaryTimestampMs {
                                Button(timeLabel(ms)) {
                                    onTapTimestamp(ms, item.segmentIds?.first)
                                }
                                .font(.callout.weight(.semibold))
                            }
                        }
                    }
                    .padding(14)
                    .background(.regularMaterial)
                    .clipShape(RoundedRectangle(cornerRadius: 16))
                }
            }
        }
    }

    private func color(for imp: String) -> Color {
        switch imp {
        case "high":   return .pink
        case "medium": return .purple
        default:       return .secondary
        }
    }

    private func timeLabel(_ ms: Int) -> String {
        let sec = ms / 1000, h = sec / 3600, m = (sec % 3600) / 60, s = sec % 60
        return h > 0
            ? String(format: "%02d:%02d:%02d", h, m, s)
            : String(format: "%02d:%02d", m, s)
    }
}
```

SummaryTab での利用:
```swift
if !summary.conversationHighlights.isEmpty {
    ConversationHighlightsSection(
        items: summary.conversationHighlights,
        onTapTimestamp: { ms, segmentId in
            jumpStore.requestJump(
                targetSec: Double(ms) / 1000.0,
                segmentId: segmentId
            )
        }
    )
}
```

## 5. チェックリスト

- [ ] Desktop: `SessionAIPanel` を SessionDetail 右ペインに差し込み
- [ ] iOS: `SessionAIChatSheet` を SessionDetailScreen の AI ボタンで open
- [ ] 両方: `idempotencyKey` を UUID で毎リクエスト生成
- [ ] 両方: `clientContext.currentPlaybackMs` を AVPlayer / `<audio>` の current time から送信
- [ ] Desktop: Firestore Listener で `/todos` と `sessions/{id}.notes` を subscribe し、action 後の UI 反映を自動化
- [ ] iOS: 同上
- [ ] 両方: `confidence=low` で「推測に近い」バッジ / `=medium` で「要確認」
- [ ] 両方: citation chip tap で transcript jump が動く
- [ ] 両方: oflline 時は `send` を retry queue に入れて復帰後送信 (optional)
