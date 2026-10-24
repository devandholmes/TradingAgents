import { useSyncExternalStore } from "react"

import { streamSSE } from "@/lib/market"
import { toast } from "@/lib/toast"

// One conversation shared by the Dashboard dock and the Assistant workspace, so a chat started in
// one continues in the other. Persisted to localStorage (last 50 messages) between streams.

export type ToolPart = {
  kind: "tool"
  name: string
  args: Record<string, unknown>
  status: "running" | "ok" | "error"
  result?: unknown
}
export type TradePart = {
  kind: "trade"
  status: "filled" | "new" | "rejected" | "error"
  order?: Record<string, unknown>
  reason?: string
  /** Args of the place_order call that produced it (rejections carry no order). */
  request?: Record<string, unknown>
}
export type Part = ToolPart | TradePart

export type TextMessage = {
  id: string
  role: "user" | "assistant"
  content: string
  parts?: Part[]
  error?: string
  ts: number
}
/** An analysis run started from the conversation, shown as a card in the thread. */
export type RunMessage = {
  id: string
  role: "run"
  runId: string
  symbol: string
  ts: number
}
export type ChatMessage = TextMessage | RunMessage

export type ChatContext = { symbol: string; interval: string }

export type ChatState = {
  messages: ChatMessage[]
  draft: string
  busy: boolean
  context: ChatContext
  /** Bumped to ask the mounted composer to focus its input (e.g. after a prefill). */
  focusTick: number
}

const STORAGE_KEY = "chat.v1"
const KEEP = 50

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v)

function load(): Pick<ChatState, "messages" | "context"> {
  const fallback = {
    messages: [],
    context: { symbol: "BTCUSDT", interval: "1h" },
  }
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "null")
    if (!isRecord(saved) || !Array.isArray(saved.messages)) return fallback
    const ctx = isRecord(saved.context) ? saved.context : {}
    return {
      messages: (saved.messages as ChatMessage[]).filter(
        (m) => isRecord(m) && typeof m.id === "string"
      ),
      context: {
        symbol:
          typeof ctx.symbol === "string" ? ctx.symbol : fallback.context.symbol,
        interval:
          typeof ctx.interval === "string"
            ? ctx.interval
            : fallback.context.interval,
      },
    }
  } catch {
    return fallback // storage unavailable or corrupt
  }
}

let state: ChatState = { ...load(), draft: "", busy: false, focusTick: 0 }
const listeners = new Set<() => void>()
let abort: AbortController | null = null
let seq = 0

function set(patch: Partial<ChatState>, persist = false) {
  state = { ...state, ...patch }
  listeners.forEach((l) => l())
  if (persist) save()
}

function save() {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        messages: state.messages.slice(-KEEP),
        context: state.context,
      })
    )
  } catch {
    /* storage unavailable or full */
  }
}

const newId = () => `${Date.now().toString(36)}-${(seq++).toString(36)}`

function subscribe(cb: () => void) {
  listeners.add(cb)
  return () => listeners.delete(cb)
}

export function useChat(): ChatState {
  return useSyncExternalStore(subscribe, () => state)
}

export function setDraft(draft: string) {
  set({ draft })
}

/** Replace the draft and focus the composer (e.g. "Ask the assistant to act on this"). */
export function prefill(text: string) {
  set({ draft: text, focusTick: state.focusTick + 1 })
}

export function setChatContext(context: ChatContext) {
  if (
    context.symbol === state.context.symbol &&
    context.interval === state.context.interval
  )
    return
  set({ context }, true)
}

export function newChat() {
  abort?.abort()
  set({ messages: [], draft: "", busy: false }, true)
}

export function stopChat() {
  abort?.abort()
}

export function addRunMessage(runId: string, symbol: string) {
  set(
    {
      messages: [
        ...state.messages,
        { id: newId(), role: "run", runId, symbol, ts: Date.now() },
      ],
    },
    true
  )
}

/** A finished tool event replaces its own "running" row; anything else appends. */
function withTool(list: Part[], raw: Record<string, unknown>): Part[] {
  const tool: ToolPart = {
    kind: "tool",
    name: String(raw.name ?? "tool"),
    args: isRecord(raw.args) ? raw.args : {},
    status:
      raw.status === "ok" || raw.status === "error" ? raw.status : "running",
    result: raw.result,
  }
  if (tool.status !== "running") {
    const i = list.findLastIndex(
      (a) => a.kind === "tool" && a.name === tool.name && a.status === "running"
    )
    if (i !== -1) return list.with(i, tool)
  }
  return [...list, tool]
}

function toTrade(list: Part[], raw: Record<string, unknown>): TradePart {
  const status: TradePart["status"] =
    raw.status === "filled" || raw.status === "new" || raw.status === "rejected"
      ? raw.status
      : "error"
  const call = list.findLast(
    (a): a is ToolPart => a.kind === "tool" && a.name === "place_order"
  )
  return {
    kind: "trade",
    status,
    order: isRecord(raw.order) ? raw.order : undefined,
    reason: typeof raw.reason === "string" ? raw.reason : undefined,
    request: call?.args,
  }
}

function toastTrade(t: TradePart) {
  const o = t.order ?? {}
  const r = t.request ?? {}
  const what =
    `${String(o.side ?? r.side ?? "")} ${String(o.symbol ?? r.symbol ?? "")}`.trim() ||
    undefined
  if (t.status === "filled")
    toast({ kind: "success", title: "Order filled", body: what })
  else if (t.status === "new")
    toast({ kind: "info", title: "Order placed", body: what })
  else
    toast({
      kind: "error",
      title: t.status === "rejected" ? "Order rejected" : "Order failed",
      body: t.reason ?? what,
    })
}

/**
 * Send `text` (default: the draft) to POST /api/chat and stream the reply into the shared thread.
 * `onTrade` runs after a trade event, so the caller can refresh the trading status.
 */
export async function sendChat(text = state.draft, onTrade?: () => void) {
  const content = text.trim()
  if (!content || state.busy) return
  const history = [
    ...state.messages.flatMap((m) =>
      m.role !== "run" && !m.error && m.content
        ? [{ role: m.role, content: m.content }]
        : []
    ),
    { role: "user" as const, content },
  ].slice(-KEEP)
  const replyId = newId()
  set(
    {
      messages: [
        ...state.messages,
        { id: newId(), role: "user", content, ts: Date.now() },
        { id: replyId, role: "assistant", content: "", ts: Date.now() },
      ],
      draft: text === state.draft ? "" : state.draft,
      busy: true,
    },
    true
  )
  const reply = (fn: (m: TextMessage) => TextMessage) =>
    set({
      messages: state.messages.map((m) =>
        m.id === replyId && m.role !== "run" ? fn(m) : m
      ),
    })
  const ctrl = (abort = new AbortController())
  try {
    await streamSSE(
      "/api/chat",
      { messages: history, ...state.context },
      (ev) => {
        if (typeof ev.delta === "string") {
          const delta = ev.delta
          reply((m) => ({ ...m, content: m.content + delta }))
        } else if (isRecord(ev.tool)) {
          const tool = ev.tool
          reply((m) => ({ ...m, parts: withTool(m.parts ?? [], tool) }))
        } else if (isRecord(ev.trade)) {
          const raw = ev.trade
          let trade: TradePart | undefined
          reply((m) => {
            trade = toTrade(m.parts ?? [], raw)
            return { ...m, parts: [...(m.parts ?? []), trade] }
          })
          if (trade) toastTrade(trade)
          onTrade?.()
        } else if (typeof ev.error === "string") {
          const error = ev.error
          reply((m) => ({ ...m, error }))
        }
      },
      ctrl.signal
    )
  } catch (err) {
    if (!ctrl.signal.aborted) {
      const error = (err as Error).message
      reply((m) => ({ ...m, error }))
    }
  } finally {
    if (abort === ctrl) {
      abort = null
      set({ busy: false }, true)
    }
  }
}
