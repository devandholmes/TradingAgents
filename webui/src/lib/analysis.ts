import { useCallback, useEffect, useState, useSyncExternalStore } from "react"

import { apiFetch, describe, sessionFetch } from "@/lib/api"
import { toast } from "@/lib/toast"

// --- API shapes (see contract: /api/analyses*) ---

export type AnalysisStatus = "running" | "done" | "error" | "cancelled"
export type AnalysisSummary = {
  id: string
  symbol: string
  ticker: string
  date: string
  status: AnalysisStatus
  rating: string | null
  started_at: string
  finished_at: string | null
  duration_s: number | null
}
type AnalysisDetail = AnalysisSummary & {
  stages?: unknown[]
  error?: string | null
}

export const ANALYSTS = [
  { key: "market", label: "Market" },
  { key: "social", label: "Sentiment" },
  { key: "news", label: "News" },
  { key: "fundamentals", label: "Fundamentals" },
] as const
export type AnalystKey = (typeof ANALYSTS)[number]["key"]
export type StageKey = AnalystKey | "research" | "trader" | "risk" | "decision"

export const STAGES: Record<StageKey, { label: string; agents: string }> = {
  market: { label: "Market analyst", agents: "Price action and indicators" },
  social: { label: "Sentiment analyst", agents: "Social sentiment" },
  news: { label: "News analyst", agents: "Macro and headlines" },
  fundamentals: { label: "Fundamentals analyst", agents: "Financials" },
  research: {
    label: "Research debate",
    agents: "Bull and Bear researchers, Research Manager",
  },
  trader: { label: "Trader", agents: "Trade plan" },
  risk: {
    label: "Risk debate",
    agents: "Aggressive, Conservative and Neutral analysts",
  },
  decision: { label: "Decision", agents: "Portfolio Manager" },
}
const ANALYST_KEYS: readonly StageKey[] = ANALYSTS.map((a) => a.key)
const LATER_STAGES: StageKey[] = ["research", "trader", "risk", "decision"]
const isStage = (v: unknown): v is StageKey =>
  typeof v === "string" && v in STAGES

// --- run state, folded from the event stream ---

export type StageState = {
  status: "pending" | "running" | "done"
  startedAt?: number
  endedAt?: number
  /** Who is working on it now (the current debate speaker, the analyst). */
  agent?: string
}
export type AgentActivity = {
  agent: string
  stage: StageKey | null
  tools: { name: string; count: number }[]
}
export type Report = {
  stage: StageKey | null
  section: string
  title: string
  content: string
}
export type RunState = {
  id: string
  summary?: AnalysisSummary
  /** Stages in pipeline order: only the selected analysts, then the fixed stages. */
  order: StageKey[]
  stages: Partial<Record<StageKey, StageState>>
  activity: AgentActivity[]
  reports: Report[]
  decision?: { rating: string; summary: string; at?: number }
  error?: string
  status: AnalysisStatus
  firstAt?: number
  lastAt?: number
  lastId: number | null
  /** The server sent {"type":"end"}: every event has been replayed and the run is over. */
  ended: boolean
  connection: "connecting" | "open" | "retrying"
  loadError?: string
}

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v)

/** Event timestamps may be ISO strings or epoch seconds/ms. */
export function toMs(ts: unknown): number | undefined {
  if (typeof ts === "number") return ts < 1e12 ? ts * 1000 : ts
  if (typeof ts === "string") {
    const n = Date.parse(ts)
    return Number.isNaN(n) ? undefined : n
  }
  return undefined
}

/** The pipeline stage an agent belongs to, from its display name. */
function stageOfAgent(agent: string): StageKey | null {
  const a = agent.toLowerCase()
  if (a.includes("market")) return "market"
  if (a.includes("social") || a.includes("sentiment")) return "social"
  if (a.includes("news")) return "news"
  if (a.includes("fundamental")) return "fundamentals"
  if (a.includes("bull") || a.includes("bear") || a.includes("research"))
    return "research"
  if (a.includes("trader")) return "trader"
  if (/aggressive|conservative|neutral|risk|safe/.test(a)) return "risk"
  if (a.includes("portfolio")) return "decision"
  return null
}

function withOrder(run: RunState, extra: Iterable<string>): StageKey[] {
  const analysts = new Set(run.order.filter((k) => ANALYST_KEYS.includes(k)))
  for (const k of extra)
    if (ANALYST_KEYS.includes(k as StageKey)) analysts.add(k as StageKey)
  return [...ANALYST_KEYS.filter((k) => analysts.has(k)), ...LATER_STAGES]
}

function touchAgent(
  list: AgentActivity[],
  agent: string,
  stage: StageKey | null
): AgentActivity[] {
  const i = list.findIndex((a) => a.agent === agent)
  if (i === -1) return [...list, { agent, stage, tools: [] }]
  return list[i].stage || !stage ? list : list.with(i, { ...list[i], stage })
}

function reduce(run: RunState, ev: Record<string, unknown>): RunState {
  const at = toMs(ev.ts)
  const id = typeof ev.id === "number" ? ev.id : Number(ev.id)
  const next: RunState = {
    ...run,
    lastId: Number.isFinite(id) ? id : run.lastId,
    firstAt: run.firstAt ?? at,
    lastAt: at ?? run.lastAt,
  }
  const runningStage = () =>
    next.order.findLast((k) => next.stages[k]?.status === "running") ?? null

  switch (ev.type) {
    case "stage": {
      if (!isStage(ev.stage)) break
      const key = ev.stage
      const prev = next.stages[key]
      const status = ev.status === "done" ? "done" : "running"
      next.order = withOrder(next, [key])
      next.stages = {
        ...next.stages,
        [key]: {
          status,
          startedAt: prev?.startedAt ?? at,
          endedAt: status === "done" ? at : undefined,
          agent: typeof ev.agent === "string" ? ev.agent : prev?.agent,
        },
      }
      if (typeof ev.agent === "string")
        next.activity = touchAgent(next.activity, ev.agent, key)
      break
    }
    case "activity": {
      const agent = typeof ev.agent === "string" ? ev.agent : "Agent"
      const tool = typeof ev.tool === "string" ? ev.tool : "tool"
      const list = touchAgent(
        next.activity,
        agent,
        stageOfAgent(agent) ?? runningStage()
      )
      const i = list.findIndex((a) => a.agent === agent)
      const entry = list[i]
      const j = entry.tools.findIndex((t) => t.name === tool)
      const tools =
        j === -1
          ? [...entry.tools, { name: tool, count: 1 }]
          : entry.tools.with(j, { name: tool, count: entry.tools[j].count + 1 })
      next.activity = list.with(i, { ...entry, tools })
      break
    }
    case "report": {
      const section = String(ev.section ?? "report")
      const report: Report = {
        stage: isStage(ev.stage) ? ev.stage : null,
        section,
        title: String(ev.title ?? section.replace(/_/g, " ")),
        content: String(ev.content ?? ""),
      }
      if (report.stage) next.order = withOrder(next, [report.stage])
      next.reports = [
        ...next.reports.filter((r) => r.section !== section),
        report,
      ]
      break
    }
    case "decision":
      next.decision = {
        rating: String(ev.rating ?? ""),
        summary: String(ev.summary ?? ""),
        at,
      }
      next.stages = {
        ...next.stages,
        decision: {
          status: "done",
          startedAt: next.stages.decision?.startedAt ?? at,
          endedAt: at,
        },
      }
      break
    case "error":
      next.error = String(ev.message ?? "Analysis failed")
      break
    case "status":
      if (
        ev.status === "running" ||
        ev.status === "done" ||
        ev.status === "error" ||
        ev.status === "cancelled"
      )
        next.status = ev.status
      break
    case "end":
      next.ended = true
      if (next.status === "running") next.status = next.error ? "error" : "done"
      break
  }
  return next
}

// --- analysts chosen per run (the summary does not carry them; remembered for the stepper) ---

const ANALYSTS_KEY = "analysis.analysts.v1"

function rememberedAnalysts(): Record<string, string[]> {
  try {
    const v = JSON.parse(localStorage.getItem(ANALYSTS_KEY) ?? "{}")
    return isRecord(v) ? (v as Record<string, string[]>) : {}
  } catch {
    return {}
  }
}

function rememberAnalysts(id: string, analysts: string[]) {
  try {
    const entries = Object.entries(rememberedAnalysts()).slice(-29)
    localStorage.setItem(
      ANALYSTS_KEY,
      JSON.stringify(Object.fromEntries([...entries, [id, analysts]]))
    )
  } catch {
    /* storage unavailable */
  }
}

/** Stage keys from the detail's `stages` (strings or {key|stage} objects), if it lists them. */
function stageKeys(stages: unknown[] | undefined): string[] {
  return (stages ?? []).flatMap((s) => {
    if (typeof s === "string") return [s]
    const key = isRecord(s) ? (s.key ?? s.stage) : undefined
    return typeof key === "string" ? [key] : []
  })
}

// --- one shared connection per run id: re-attaches with Last-Event-ID ---

type Entry = {
  run: RunState
  listeners: Set<() => void>
  ctrl?: AbortController
}
const runs = new Map<string, Entry>()

function blankRun(id: string): RunState {
  const run: RunState = {
    id,
    order: [],
    stages: {},
    activity: [],
    reports: [],
    status: "running",
    lastId: null,
    ended: false,
    connection: "connecting",
  }
  return { ...run, order: withOrder(run, rememberedAnalysts()[id] ?? []) }
}

function update(id: string, fn: (r: RunState) => RunState) {
  const e = runs.get(id)
  if (!e) return
  e.run = fn(e.run)
  e.listeners.forEach((l) => l())
}

/** Parse a text/event-stream body; `id:` lines fill in a missing JSON `id`. */
async function readEvents(
  res: Response,
  onEvent: (ev: Record<string, unknown>) => void
) {
  if (!res.body) throw new Error("Empty event stream")
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ""
  for (;;) {
    const { value, done } = await reader.read()
    if (done) return
    buffer += value.replace(/\r\n/g, "\n")
    let sep: number
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      let sseId: string | undefined
      const data: string[] = []
      for (const line of block.split("\n")) {
        if (line.startsWith("data:")) data.push(line.slice(5).trimStart())
        else if (line.startsWith("id:")) sseId = line.slice(3).trim()
      }
      if (!data.length) continue
      try {
        const ev = JSON.parse(data.join("\n"))
        if (isRecord(ev))
          onEvent(ev.id == null && sseId ? { ...ev, id: sseId } : ev)
      } catch {
        /* ignore malformed event */
      }
    }
  }
}

async function connectRun(id: string, ctrl: AbortController) {
  const { signal } = ctrl
  const path = `/api/analyses/${encodeURIComponent(id)}`
  // Metadata (symbol, date, stages) in parallel with the event stream.
  void apiFetch<AnalysisDetail>(path, { signal }).then(
    (detail) =>
      update(id, (r) => ({
        ...r,
        summary: detail,
        order: withOrder(r, stageKeys(detail.stages)),
        error: r.error ?? detail.error ?? undefined,
        status: r.ended ? r.status : detail.status,
      })),
    () => {} // the event stream reports failures
  )
  for (let attempt = 0; !signal.aborted; attempt++) {
    if (runs.get(id)?.run.ended) return
    try {
      const lastId = runs.get(id)?.run.lastId
      const headers = new Headers({ Accept: "text/event-stream" })
      if (lastId != null) headers.set("Last-Event-ID", String(lastId))
      const res = await sessionFetch(`${path}/events`, { headers, signal })
      update(id, (r) => ({ ...r, connection: "open", loadError: undefined }))
      attempt = 0
      await readEvents(res, (ev) => update(id, (r) => reduce(r, ev)))
    } catch (err) {
      if (signal.aborted) return
      const message = describe(err)
      if (/HTTP 404/.test(message)) {
        update(id, (r) => ({
          ...r,
          loadError: "This run no longer exists on the server.",
          ended: true,
        }))
        return
      }
      update(id, (r) => ({ ...r, loadError: message }))
    }
    if (runs.get(id)?.run.ended) {
      onRunEnded(id)
      return
    }
    // Dropped before "end" (server restart, network): re-attach and resume from Last-Event-ID.
    update(id, (r) => ({ ...r, connection: "retrying" }))
    const delay = Math.min(15_000, 1000 * 2 ** attempt)
    await new Promise((ok) => setTimeout(ok, delay))
  }
}

function subscribeRun(id: string, cb: () => void) {
  let e = runs.get(id)
  if (!e) runs.set(id, (e = { run: blankRun(id), listeners: new Set() }))
  e.listeners.add(cb)
  if (!e.ctrl && !e.run.ended) {
    const ctrl = (e.ctrl = new AbortController())
    void connectRun(id, ctrl).finally(() => {
      const cur = runs.get(id)
      if (cur?.ctrl === ctrl) cur.ctrl = undefined
    })
  }
  return () => {
    const cur = runs.get(id)
    if (!cur) return
    cur.listeners.delete(cb)
    // Last viewer gone: drop the connection, keep the folded state for a quick re-attach.
    if (!cur.listeners.size) {
      cur.ctrl?.abort()
      cur.ctrl = undefined
    }
  }
}

/** Live state of one run: replays every past event over SSE, then follows it until it ends. */
export function useAnalysis(id: string | null): RunState | null {
  const subscribe = useCallback(
    (cb: () => void) => (id ? subscribeRun(id, cb) : () => {}),
    [id]
  )
  return useSyncExternalStore(subscribe, () =>
    id ? (runs.get(id)?.run ?? null) : null
  )
}

// --- run history (refreshed on run start, cancel and end; never polled) ---

export type HistoryState = {
  list?: AnalysisSummary[]
  error?: string
  loading: boolean
}
let history: HistoryState = { loading: true }
const historyListeners = new Set<() => void>()
let historyRequested = false

function setHistory(next: HistoryState) {
  history = next
  historyListeners.forEach((l) => l())
}

export function refreshAnalyses(): Promise<void> {
  historyRequested = true
  return apiFetch<AnalysisSummary[]>("/api/analyses?limit=30").then(
    (list) => setHistory({ list, loading: false }),
    (err: Error) =>
      setHistory({ ...history, error: err.message, loading: false })
  )
}

function onRunEnded(id: string) {
  // Only a run we saw running changes the history; replaying an old run does not.
  const seen = history.list?.find((s) => s.id === id && s.status === "running")
  if (!seen) return
  void refreshAnalyses()
  const run = runs.get(id)?.run
  if (!run || run.status === "cancelled") return
  toast(
    run.status === "error"
      ? {
          kind: "error",
          title: `Analysis of ${seen.symbol} failed`,
          body: run.error,
          key: id,
        }
      : {
          kind: "success",
          title: `Analysis of ${seen.symbol} finished`,
          body: run.decision?.rating
            ? `Rating: ${run.decision.rating}`
            : undefined,
          key: id,
        }
  )
}

function subscribeHistory(cb: () => void) {
  historyListeners.add(cb)
  if (!historyRequested) void refreshAnalyses()
  return () => {
    historyListeners.delete(cb)
  }
}

export function useAnalyses(): HistoryState {
  return useSyncExternalStore(subscribeHistory, () => history)
}

/** "HTTP 429: Two analyses are already running" → "Two analyses are already running". */
export const cleanError = (err: unknown) =>
  (err instanceof Error ? err.message : String(err)).replace(/^HTTP \d+: /, "")

export async function startAnalysis(body: {
  symbol: string
  date?: string | null
  analysts?: AnalystKey[] | null
}): Promise<AnalysisSummary> {
  const summary = await apiFetch<AnalysisSummary>("/api/analyses", {
    method: "POST",
    body: JSON.stringify({
      symbol: body.symbol,
      date: body.date || null,
      analysts: body.analysts?.length ? body.analysts : null,
    }),
  })
  if (body.analysts?.length) rememberAnalysts(summary.id, body.analysts)
  setHistory({
    ...history,
    list: [summary, ...(history.list ?? []).filter((s) => s.id !== summary.id)],
  })
  return summary
}

export async function cancelAnalysis(id: string): Promise<void> {
  const summary = await apiFetch<AnalysisSummary>(
    `/api/analyses/${encodeURIComponent(id)}`,
    { method: "DELETE" }
  )
  if (summary?.id)
    setHistory({
      ...history,
      list: history.list?.map((s) => (s.id === id ? summary : s)),
    })
}

// --- display helpers ---

/** Current time, ticking every second while `active` (for elapsed-time labels). */
export function useNow(active: boolean): number {
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    if (!active) return
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [active])
  return now
}

export function fmtDuration(ms: number | undefined | null): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return ""
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`
}

export type RatingTone = "up" | "down" | "hold" | "none"

/** Buy / Overweight read as up, Sell / Underweight as down, Hold as neutral. */
export function ratingTone(rating: string | null | undefined): RatingTone {
  const r = (rating ?? "").toUpperCase()
  if (r.includes("UNDERWEIGHT") || r.includes("SELL")) return "down"
  if (r.includes("OVERWEIGHT") || r.includes("BUY")) return "up"
  if (r.includes("HOLD")) return "hold"
  return "none"
}
