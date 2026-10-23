import {
  useEffect,
  useEffectEvent,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react"

import { checkResponse, describe, sessionFetch } from "@/lib/api"

// --- API shapes (see contract: GET /api/*) ---

export type Kline = {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume: number
}
export type TrendRow = {
  symbol: string
  lastPrice: number
  priceChangePercent: number
  quoteVolume: number
}
export type Trends = {
  gainers: TrendRow[]
  losers: TrendRow[]
  volume: TrendRow[]
}
export type Indicators = {
  symbol: string
  interval: string
  price: number
  rsi14: number | null
  macd: number | null
  macd_signal: number | null
  macd_hist: number | null
  sma20: number | null
  sma50: number | null
  ema20: number | null
  trend: "up" | "down" | "sideways"
}

export const INTERVALS = ["1m", "5m", "15m", "1h", "4h", "1d"] as const
export type Interval = (typeof INTERVALS)[number]

/** GET a JSON endpoint; refetches every `refreshMs` when given. Keeps the last good data on refresh errors. */
export function useApi<T>(url: string, refreshMs?: number) {
  const [state, setState] = useState<{ url: string; data?: T; error?: string }>(
    { url: "" }
  )

  useEffect(() => {
    let ctrl: AbortController | undefined
    const load = () => {
      ctrl?.abort()
      const c = (ctrl = new AbortController())
      fetch(url, { signal: c.signal })
        .then(checkResponse)
        .then((r) => r.json() as Promise<T>)
        .then(
          (data) => setState({ url, data }),
          (err) => {
            if (c.signal.aborted) return
            setState((s) => ({
              url,
              data: s.url === url ? s.data : undefined,
              error: describe(err),
            }))
          }
        )
    }
    load()
    const id = refreshMs ? setInterval(load, refreshMs) : undefined
    return () => {
      ctrl?.abort()
      clearInterval(id)
    }
  }, [url, refreshMs])

  const current = state.url === url
  return {
    data: current ? state.data : undefined,
    error: current ? state.error : undefined,
    loading: !current,
  }
}

/** POST `body` (with X-TA-Session) and call `onEvent` for every `data: {...}` server-sent event until the stream ends. */
export async function streamSSE(
  url: string,
  body: unknown,
  onEvent: (event: Record<string, unknown>) => void,
  signal: AbortSignal
): Promise<void> {
  let res: Response
  try {
    res = await sessionFetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify(body),
      signal,
    })
  } catch (err) {
    if (signal.aborted) throw err
    throw new Error(describe(err), { cause: err })
  }
  if (!res.body) throw new Error("Empty response stream")

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ""
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += value.replace(/\r\n/g, "\n")
    let sep: number
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      const data = block
        .split("\n")
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trimStart())
        .join("\n")
      if (!data) continue
      try {
        onEvent(JSON.parse(data))
      } catch {
        /* ignore malformed event */
      }
    }
  }
}

// --- Binance public market-data WebSocket (browser connects directly) ---
//
// ONE combined-stream socket for the whole app. Streams are ref-counted by listener; changes are
// coalesced and sent as a single UNSUBSCRIBE + SUBSCRIBE pair, at most every FLUSH_GAP_MS, which
// stays under Binance's 5 client messages/second limit. Reconnects with exponential backoff
// (1s → 30s) and resubscribes everything that is still wanted via the connect URL.

const WS_URL = "wss://data-stream.binance.vision/stream"
const FLUSH_GAP_MS = 600
export type WsStatus = "connecting" | "open" | "closed"
type StreamListener = (stream: string, data: unknown) => void

const streamListeners = new Map<string, Set<StreamListener>>()
let socket: WebSocket | undefined
let serverStreams = new Set<string>() // what the server is sending on `socket`
let wsStatus: WsStatus = "connecting"
const statusListeners = new Set<() => void>()
let attempt = 0
let retryTimer: ReturnType<typeof setTimeout> | undefined
let flushTimer: ReturnType<typeof setTimeout> | undefined
let lastFlush = 0
let requestId = 0

function setWsStatus(status: WsStatus) {
  if (status === wsStatus) return
  wsStatus = status
  statusListeners.forEach((l) => l())
}

function connect() {
  retryTimer = undefined
  const streams = [...streamListeners.keys()]
  if (!streams.length) return // reconnects on the next subscribe
  setWsStatus("connecting")
  const ws = (socket = new WebSocket(`${WS_URL}?streams=${streams.join("/")}`))
  serverStreams = new Set(streams)
  ws.onopen = () => {
    attempt = 0
    setWsStatus("open")
    scheduleFlush() // streams added or dropped while connecting
  }
  ws.onmessage = (ev) => {
    let msg: { stream?: string; data?: unknown }
    try {
      msg = JSON.parse(ev.data)
    } catch {
      return // malformed frame
    }
    const stream = msg?.stream
    if (!stream) return // SUBSCRIBE/UNSUBSCRIBE acks
    streamListeners.get(stream)?.forEach((l) => {
      try {
        l(stream, msg.data)
      } catch (err) {
        console.error(err)
      }
    })
  }
  ws.onclose = () => {
    if (socket !== ws) return
    socket = undefined
    setWsStatus("closed")
    const delay =
      Math.min(30_000, 1000 * 2 ** attempt++) * (0.75 + Math.random() * 0.5)
    retryTimer = setTimeout(connect, delay)
  }
}

function flush() {
  flushTimer = undefined
  if (!socket) {
    if (!retryTimer) connect()
    return
  }
  if (socket.readyState !== WebSocket.OPEN) return
  const wanted = new Set(streamListeners.keys())
  const drop = [...serverStreams].filter((s) => !wanted.has(s))
  const add = [...wanted].filter((s) => !serverStreams.has(s))
  if (drop.length)
    socket.send(
      JSON.stringify({ method: "UNSUBSCRIBE", params: drop, id: ++requestId })
    )
  if (add.length)
    socket.send(
      JSON.stringify({ method: "SUBSCRIBE", params: add, id: ++requestId })
    )
  serverStreams = wanted
  if (drop.length || add.length) lastFlush = Date.now()
}

// Deferred so a burst of mounts/unmounts (or StrictMode's double effects) nets out to one change.
function scheduleFlush() {
  if (flushTimer) return
  flushTimer = setTimeout(
    flush,
    Math.max(0, lastFlush + FLUSH_GAP_MS - Date.now())
  )
}

/** Add a listener for one stream on the shared socket; returns the unsubscribe function. */
export function subscribeStream(
  stream: string,
  listener: StreamListener
): () => void {
  let set = streamListeners.get(stream)
  if (!set) streamListeners.set(stream, (set = new Set()))
  set.add(listener)
  scheduleFlush()
  return () => {
    set.delete(listener)
    if (!set.size && streamListeners.get(stream) === set)
      streamListeners.delete(stream)
    scheduleFlush()
  }
}

function subscribeStatus(cb: () => void) {
  statusListeners.add(cb)
  return () => {
    statusListeners.delete(cb)
  }
}

/** Status of the shared market-data socket. */
export function useStreamStatus(): WsStatus {
  return useSyncExternalStore(subscribeStatus, () => wsStatus)
}

/** Listen to one stream (e.g. `btcusdt@kline_1h`) on the shared socket. */
export function useBinanceStream(
  stream: string,
  onMessage: (stream: string, data: unknown) => void
): WsStatus {
  const handle = useEffectEvent(onMessage)
  useEffect(
    () => subscribeStream(stream, (s, data) => handle(s, data)),
    [stream]
  )
  return useStreamStatus()
}

// --- live prices from !miniTicker@arr, batched once per animation frame ---

export type LivePrice = { price: number; pct: number }
type MiniTicker = { s: string; c: string; o: string }

const TICKER_STREAM = "!miniTicker@arr"
const prices = new Map<string, LivePrice>() // entries are replaced, never mutated
let priceVersion = 0
const staged = new Map<string, MiniTicker>() // latest raw ticker per symbol since last frame
let frame = 0
const priceListeners = new Set<() => void>()
let stopTicker: (() => void) | undefined

function onTickers(_stream: string, data: unknown) {
  if (!Array.isArray(data)) return
  for (const t of data as MiniTicker[]) staged.set(t.s, t)
  // rAF also pauses in background tabs; `staged` stays bounded (one entry per symbol).
  if (!frame) frame = requestAnimationFrame(applyTickers)
}

function applyTickers() {
  frame = 0
  let changed = false
  for (const t of staged.values()) {
    const price = +t.c
    const open = +t.o
    const pct = open ? ((price - open) / open) * 100 : 0
    const prev = prices.get(t.s)
    if (prev && prev.price === price && prev.pct === pct) continue
    prices.set(t.s, { price, pct })
    changed = true
  }
  staged.clear()
  if (!changed) return
  priceVersion++
  priceListeners.forEach((l) => l())
}

function subscribePrices(cb: () => void) {
  if (!priceListeners.size)
    stopTicker = subscribeStream(TICKER_STREAM, onTickers)
  priceListeners.add(cb)
  return () => {
    priceListeners.delete(cb)
    if (!priceListeners.size) stopTicker?.()
  }
}

/** Snapshot getter that keeps its object identity until one of *its* symbols changes. */
function priceView(key: string) {
  const symbols = key ? key.split(",") : []
  let version = -1
  let snap: Record<string, LivePrice> = {}
  return () => {
    if (version === priceVersion) return snap
    version = priceVersion
    const next: Record<string, LivePrice> = {}
    let changed = false
    for (const s of symbols) {
      const p = prices.get(s)
      if (p) next[s] = p
      if (p !== snap[s]) changed = true
    }
    if (changed) snap = next
    return snap
  }
}

/** Live last price and 24h % for `symbols` (e.g. "BTCUSDT"); re-renders only when one of them changes. */
export function useLivePrices(symbols: string[]): Record<string, LivePrice> {
  const key = symbols.join(",")
  const getSnapshot = useMemo(() => priceView(key), [key])
  return useSyncExternalStore(subscribePrices, getSnapshot)
}

// --- formatting ---

/** Decimal places worth showing for a price. */
export function pricePrecision(price: number): number {
  const abs = Math.abs(price)
  return abs >= 1000 ? 2 : abs >= 1 ? 4 : abs >= 0.01 ? 5 : 8
}

export function fmtPrice(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—"
  const digits = pricePrecision(n)
  return n.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: digits,
  })
}

export function fmtPct(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—"
  return `${n > 0 ? "+" : ""}${n.toFixed(2)}%`
}

export function fmtCompact(n: number): string {
  return n.toLocaleString("en-US", {
    notation: "compact",
    maximumFractionDigits: 2,
  })
}
