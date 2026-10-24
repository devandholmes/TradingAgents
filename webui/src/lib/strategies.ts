import { useEffect, useMemo, useState, useSyncExternalStore } from "react"

import { apiFetch, describe } from "@/lib/api"

// --- API shapes (see contract: /api/strategies*, /ws/strategies) ---

export const WINDOWS = ["7d", "30d", "90d", "1y", "live"] as const
export type StrategyWindow = (typeof WINDOWS)[number]
export const STRATEGY_INTERVALS = ["15m", "1h", "4h", "1d"] as const

export type Family =
  | "trend"
  | "mean_reversion"
  | "breakout"
  | "momentum"
  | "accumulation"
  | "benchmark"
  | "ai"

export const FAMILY_LABEL: Record<Family, string> = {
  trend: "Trend",
  mean_reversion: "Mean reversion",
  breakout: "Breakout",
  momentum: "Momentum",
  accumulation: "Accumulation",
  benchmark: "Benchmark",
  ai: "AI",
}

/** Every instance starts with this much virtual capital. */
export const REFERENCE_CAPITAL = 1000
/** Binance Spot minNotional: bot sizes below it are paper-mirrored, never sent. */
export const MIN_NOTIONAL_USDT = 5

export type CatalogEntry = {
  key: string
  name: string
  family: Family
  description: string
  params: Record<string, unknown>
  intervals: string[]
}

export type Metrics = {
  return_pct: number | null
  sharpe: number | null
  sortino: number | null
  max_drawdown_pct: number | null
  win_rate: number | null
  trades: number | null
  exposure_pct: number | null
  updated_at?: string | null
}

export type LeaderRow = Metrics & {
  instance_id: string
  strategy_key: string
  name: string
  family: Family
  symbol: string
  interval: string
  /** Current target exposure, 0..1. */
  exposure: number | null
  /** Current equity in USDT (started at REFERENCE_CAPITAL). */
  equity: number | null
  live_since: string | null
  updated_at: string | null
}

export type Fill = {
  ts: string
  side: "BUY" | "SELL"
  price: number
  qty: number
  quote: number
  fee: number
  exposure_after: number
  source: "backtest" | "live"
}

export type InstanceDetail = LeaderRow & {
  params: Record<string, unknown>
  description: string
  metrics: Partial<Record<StrategyWindow, Metrics>>
  recent_fills: Fill[]
}

/** Equity normalised to 100 at the window start; `t` in unix seconds. */
export type Point = { t: number; v: number }

/** The strategy endpoints answer 503 while Postgres is down. */
export const isOffline = (error?: string) => !!error?.startsWith("HTTP 503")

/** ISO string, unix seconds or unix ms -> unix seconds. */
export function toSec(t: string | number | null | undefined): number {
  if (t == null) return 0
  if (typeof t === "number") return t > 1e12 ? Math.floor(t / 1000) : t
  return Math.floor(Date.parse(t) / 1000) || 0
}

// --- catalog: fetched once per page load ---

let catalog: Promise<CatalogEntry[]> | undefined

export function useCatalog(): CatalogEntry[] | undefined {
  const [data, setData] = useState<CatalogEntry[]>()
  useEffect(() => {
    let live = true
    catalog ??= apiFetch<CatalogEntry[]>("/api/strategies")
    catalog.then(
      (c) => live && setData(c),
      () => (catalog = undefined) // let the next mount retry
    )
    return () => {
      live = false
    }
  }, [])
  return data
}

// --- leaderboard: one full board per window, merged with /ws/strategies deltas ---
//
// Each window is fetched once (every instance, filtered client-side, so filter changes never
// refetch). Deltas replace rows by instance_id; untouched rows keep their identity, so memoised
// table rows skip re-rendering. A reconnect refetches the loaded windows (missed deltas).

export type Board = {
  rows: LeaderRow[] | null
  error?: string
  loading: boolean
}

const LOADING: Board = { rows: null, loading: true }
const boards = new Map<StrategyWindow, Board>()
const boardListeners = new Set<() => void>()
/** Latest equity per instance and when it was seen, for appending live points to cached curves. */
const lastEquity = new Map<string, { equity: number; t: number }>()

function setBoard(w: StrategyWindow, b: Board) {
  boards.set(w, b)
  boardListeners.forEach((l) => l())
}

function noteEquity(rows: LeaderRow[]) {
  for (const r of rows)
    if (r.equity != null)
      lastEquity.set(r.instance_id, {
        equity: r.equity,
        t: toSec(r.updated_at),
      })
}

async function loadBoard(w: StrategyWindow) {
  const prev = boards.get(w)
  setBoard(w, { rows: prev?.rows ?? null, loading: true })
  try {
    // ponytail: every instance in one response (~210 rows); page it if the universe grows 5x.
    const rows = await apiFetch<LeaderRow[]>(
      `/api/strategies/leaderboard?window=${w}&limit=500`
    )
    noteEquity(rows)
    setBoard(w, { rows, loading: false })
  } catch (err) {
    setBoard(w, {
      rows: prev?.rows ?? null,
      loading: false,
      error: describe(err),
    })
  }
}

export function reloadBoard(w: StrategyWindow) {
  void loadBoard(w)
}

function mergeRows(w: StrategyWindow | undefined, changed: LeaderRow[]) {
  appendLivePoints(changed)
  const targets = w ? [w] : [...boards.keys()]
  for (const win of targets) {
    const b = boards.get(win)
    if (!b?.rows) continue
    const byId = new Map(changed.map((r) => [r.instance_id, r]))
    const rows = b.rows.map((r) => byId.get(r.instance_id) ?? r)
    // New instances (e.g. the AI strategy's first live bar) join the board.
    const known = new Set(b.rows.map((r) => r.instance_id))
    for (const r of changed) if (!known.has(r.instance_id)) rows.push(r)
    boards.set(win, { ...b, rows })
  }
  boardListeners.forEach((l) => l())
}

function subscribeBoards(cb: () => void) {
  boardListeners.add(cb)
  const release = retainStrategiesSocket()
  return () => {
    boardListeners.delete(cb)
    release()
  }
}

/** Every instance's metrics for `window`, kept live over /ws/strategies. */
export function useLeaderboard(w: StrategyWindow): Board {
  useEffect(() => {
    if (!boards.has(w)) void loadBoard(w)
  }, [w])
  return useSyncExternalStore(subscribeBoards, () => boards.get(w) ?? LOADING)
}

// --- equity curves: cached per (window, resolution, instance), fetched in batches ---

const equity = new Map<string, Point[]>()
const inflight = new Set<string>()
const equityListeners = new Set<() => void>()
let equityVersion = 0
const ekey = (w: string, points: number, id: string) => `${w}|${points}|${id}`

function bumpEquity() {
  equityVersion++
  equityListeners.forEach((l) => l())
}

type RawPoint = { t: number | string; v: number }

async function fetchEquity(w: StrategyWindow, points: number, ids: string[]) {
  ids.forEach((id) => inflight.add(ekey(w, points, id)))
  try {
    const data = await apiFetch<Record<string, RawPoint[]>>(
      `/api/strategies/equity?ids=${ids.map(encodeURIComponent).join(",")}&window=${w}&points=${points}`
    )
    for (const id of ids)
      equity.set(
        ekey(w, points, id),
        (data[id] ?? []).map((p) => ({ t: toSec(p.t), v: p.v }))
      )
  } catch {
    // Empty curves for this batch; the table still shows numbers. A reconnect clears and retries.
    for (const id of ids) equity.set(ekey(w, points, id), [])
  } finally {
    ids.forEach((id) => inflight.delete(ekey(w, points, id)))
    bumpEquity()
  }
}

/**
 * A closed bar moved an instance's equity: move its cached curves by the same ratio, so
 * sparklines and the compare chart follow without refetching.
 * ponytail: the window start is not slid forward; a reload re-anchors the curve.
 */
function appendLivePoints(rows: LeaderRow[]) {
  let touched = false
  for (const r of rows) {
    if (r.equity == null) continue
    const t = toSec(r.updated_at)
    const prev = lastEquity.get(r.instance_id)
    lastEquity.set(r.instance_id, { equity: r.equity, t })
    if (!prev || t <= prev.t || !prev.equity) continue
    const ratio = r.equity / prev.equity
    for (const [k, pts] of equity) {
      if (!k.endsWith(`|${r.instance_id}`) || !pts.length) continue
      const last = pts[pts.length - 1]
      if (t <= last.t) continue
      const v = last.v * ratio
      // Keep the curve's resolution: within one step of the last point, move it instead of adding one.
      const step = pts.length > 1 ? (last.t - pts[0].t) / (pts.length - 1) : 0
      equity.set(
        k,
        t - last.t < step
          ? [...pts.slice(0, -1), { t: last.t, v }]
          : [...pts, { t, v }]
      )
      touched = true
    }
  }
  if (touched) bumpEquity()
}

function subscribeEquity(cb: () => void) {
  equityListeners.add(cb)
  return () => equityListeners.delete(cb)
}

const BATCH = 40

/** Normalised equity curves for `ids`; missing ones are fetched in batched requests. */
export function useEquity(
  ids: readonly string[],
  w: StrategyWindow,
  points: number
): Record<string, Point[] | undefined> {
  const version = useSyncExternalStore(subscribeEquity, () => equityVersion)
  const idKey = ids.join(",")

  useEffect(() => {
    const missing = idKey
      .split(",")
      .filter(
        (id) =>
          id &&
          !equity.has(ekey(w, points, id)) &&
          !inflight.has(ekey(w, points, id))
      )
    for (let i = 0; i < missing.length; i += BATCH)
      void fetchEquity(w, points, missing.slice(i, i + BATCH))
  }, [idKey, w, points, version])

  return useMemo(() => {
    void version // recompute when the cache changes
    const out: Record<string, Point[] | undefined> = {}
    for (const id of idKey.split(",")) out[id] = equity.get(ekey(w, points, id))
    return out
  }, [idKey, w, points, version])
}

// --- instance detail: fetched on open, refetched when its row changes ---

export function useInstance(id: string | null, version: string | null) {
  const key = id ? `${id}#${version}` : ""
  const [state, setState] = useState<{
    key: string
    data?: InstanceDetail
    error?: string
  }>({ key: "" })

  useEffect(() => {
    if (!id) return
    const ctrl = new AbortController()
    apiFetch<InstanceDetail>(
      `/api/strategies/instances/${encodeURIComponent(id)}`,
      { signal: ctrl.signal }
    ).then(
      (data) => setState({ key, data }),
      (err: Error) => {
        if (!ctrl.signal.aborted)
          setState((s) => ({ key, data: s.data, error: err.message }))
      }
    )
    return () => ctrl.abort()
  }, [id, key])

  const fresh = state.data?.instance_id === id
  return {
    data: fresh ? state.data : undefined,
    error: state.key === key ? state.error : undefined,
    loading: !!id && state.key !== key && !fresh,
  }
}

// --- /ws/strategies: ONE socket shared by the leaderboard and the bot store ---
//
// Opened by the first consumer, closed IDLE_CLOSE_MS after the last one leaves; reconnects with
// exponential backoff (1s -> 30s) and, once back, refetches what it may have missed.

export type SocketStatus = "connecting" | "open" | "closed"
type BotListener = (msg: Record<string, unknown>) => void

const IDLE_CLOSE_MS = 60_000
let socket: WebSocket | undefined
let connection: AbortController | undefined
let retainers = 0
let attempt = 0
let retry: ReturnType<typeof setTimeout> | undefined
let idle: ReturnType<typeof setTimeout> | undefined
let opened = false // a later open means we reconnected
let status: SocketStatus = "connecting"
const statusListeners = new Set<() => void>()
const botListeners = new Set<BotListener>()

function setStatus(s: SocketStatus) {
  status = s
  statusListeners.forEach((l) => l())
}

function wsUrl(path: string): string {
  return `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${path}`
}

function onMessage(msg: {
  type?: string
  window?: StrategyWindow
  rows?: LeaderRow[]
}) {
  if (msg.type === "leaderboard" && Array.isArray(msg.rows))
    mergeRows(
      WINDOWS.includes(msg.window as StrategyWindow) ? msg.window : undefined,
      msg.rows
    )
  else if (msg.type === "bot") botListeners.forEach((l) => l(msg))
}

function scheduleRetry() {
  setStatus("closed")
  const delay =
    Math.min(30_000, 1000 * 2 ** attempt++) * (0.75 + Math.random() * 0.5)
  retry = setTimeout(connect, delay)
}

async function connect() {
  retry = undefined
  const ctrl = (connection = new AbortController())
  setStatus("connecting")
  try {
    const session = await apiFetch<{ token: string }>("/api/session", {
      signal: ctrl.signal,
    })
    if (ctrl.signal.aborted) return
    const sock = (socket = new WebSocket(wsUrl("/ws/strategies")))
    sock.onopen = () => {
      sock.send(JSON.stringify({ token: session.token }))
      attempt = 0
      setStatus("open")
      if (opened) {
        // Back after a drop: refetch rather than trust a gap of missed deltas.
        equity.clear()
        bumpEquity()
        for (const w of boards.keys()) void loadBoard(w)
        botListeners.forEach((l) => l({ type: "reconnect" }))
      }
      opened = true
    }
    sock.onmessage = (ev) => {
      try {
        onMessage(JSON.parse(ev.data))
      } catch {
        /* ignore malformed frame */
      }
    }
    sock.onclose = () => {
      if (socket !== sock) return
      socket = undefined
      scheduleRetry()
    }
  } catch {
    if (!ctrl.signal.aborted) scheduleRetry()
  }
}

function disconnect() {
  idle = undefined
  connection?.abort()
  connection = undefined
  clearTimeout(retry)
  retry = undefined
  attempt = 0
  const sock = socket
  socket = undefined // its onclose sees this and does not retry
  sock?.close()
  setStatus("connecting")
}

/** Keep the shared socket open; `onBot` receives `{type: "bot", ...status}` frames. Returns the release. */
export function retainStrategiesSocket(onBot?: BotListener): () => void {
  clearTimeout(idle)
  idle = undefined
  if (!connection) void connect()
  retainers++
  if (onBot) botListeners.add(onBot)
  return () => {
    if (onBot) botListeners.delete(onBot)
    if (--retainers === 0) idle = setTimeout(disconnect, IDLE_CLOSE_MS)
  }
}

function subscribeStatus(cb: () => void) {
  statusListeners.add(cb)
  return () => statusListeners.delete(cb)
}

export function useStrategiesSocket(): SocketStatus {
  return useSyncExternalStore(subscribeStatus, () => status)
}
