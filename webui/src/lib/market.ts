import { useEffect, useEffectEvent, useState } from "react"

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

function describe(err: unknown): string {
  if (err instanceof TypeError)
    return "Backend unreachable — is it running on :8000?"
  return err instanceof Error ? err.message : String(err)
}

async function checkResponse(res: Response): Promise<Response> {
  if (res.ok) return res
  // Vite's proxy answers 500/502/504 with a non-JSON body when the backend is down.
  if (res.status >= 500 && !res.headers.get("content-type")?.includes("json"))
    throw new Error(
      `Backend unreachable (HTTP ${res.status}) — is it running on :8000?`
    )
  let detail = `HTTP ${res.status}`
  try {
    const body = await res.json()
    if (body?.detail)
      detail += `: ${typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)}`
  } catch {
    /* non-JSON error body */
  }
  throw new Error(detail)
}

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

/** POST `body` and call `onEvent` for every `data: {...}` server-sent event until the stream ends. */
export async function streamSSE(
  url: string,
  body: unknown,
  onEvent: (event: Record<string, unknown>) => void,
  signal: AbortSignal
): Promise<void> {
  let res: Response
  try {
    res = await checkResponse(
      await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "text/event-stream",
        },
        body: JSON.stringify(body),
        signal,
      })
    )
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

const WS_BASE = "wss://data-stream.binance.vision/stream?streams="
export type WsStatus = "connecting" | "open" | "closed"

/** Combined-stream socket with exponential-backoff reconnect (1s → 30s). */
export function useBinanceStream(
  streams: string,
  onData: (stream: string, data: unknown) => void
): WsStatus {
  const [state, setState] = useState<{ streams: string; status: WsStatus }>({
    streams,
    status: "connecting",
  })
  const handle = useEffectEvent(onData)

  useEffect(() => {
    let ws: WebSocket | undefined
    let timer: ReturnType<typeof setTimeout> | undefined
    let attempt = 0
    const set = (status: WsStatus) => setState({ streams, status })

    const connect = () => {
      ws = new WebSocket(WS_BASE + streams)
      ws.onopen = () => {
        attempt = 0
        set("open")
      }
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data)
          if (msg?.stream) handle(msg.stream, msg.data)
        } catch {
          /* ignore malformed frame */
        }
      }
      ws.onclose = () => {
        set("closed")
        const delay =
          Math.min(30_000, 1000 * 2 ** attempt++) * (0.75 + Math.random() * 0.5)
        timer = setTimeout(() => {
          set("connecting")
          connect()
        }, delay)
      }
    }
    connect()

    return () => {
      clearTimeout(timer)
      if (ws) {
        ws.onclose = null
        ws.close()
      }
    }
  }, [streams])

  return state.streams === streams ? state.status : "connecting"
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
