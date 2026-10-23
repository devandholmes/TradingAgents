import { useSyncExternalStore } from "react"

// --- errors ---

export function describe(err: unknown): string {
  if (err instanceof TypeError)
    return "Backend unreachable — is it running on :8000?"
  return err instanceof Error ? err.message : String(err)
}

export async function checkResponse(res: Response): Promise<Response> {
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

// --- session token (X-TA-Session) ---

let tokenPromise: Promise<string | null> | null = null

/** Per-process token from GET /api/session, cached in memory only. Resolves null when unavailable. */
function sessionToken(fresh = false): Promise<string | null> {
  if (fresh || !tokenPromise) {
    const p = fetch("/api/session")
      .then(checkResponse)
      .then((r) => r.json() as Promise<{ token?: unknown }>)
      .then((b) => (typeof b.token === "string" ? b.token : null))
      .catch(() => null)
    // Don't cache a failure: the next request asks again.
    void p.then((t) => {
      if (t === null && tokenPromise === p) tokenPromise = null
    })
    tokenPromise = p
  }
  return tokenPromise
}

async function withSession(
  path: string,
  init: RequestInit,
  fresh: boolean
): Promise<Response> {
  const token = await sessionToken(fresh)
  const headers = new Headers(init.headers)
  if (token) headers.set("X-TA-Session", token)
  return fetch(path, { ...init, headers })
}

/** fetch with X-TA-Session; a 403 retries once with a fresh token (e.g. backend restarted). */
export async function sessionFetch(
  path: string,
  init: RequestInit = {}
): Promise<Response> {
  let res = await withSession(path, init, false)
  if (res.status === 403) res = await withSession(path, init, true)
  return checkResponse(res)
}

/** JSON request with X-TA-Session. Throws Error(detail) on non-2xx. */
export async function apiFetch<T>(
  path: string,
  init: RequestInit = {}
): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body != null && !headers.has("Content-Type"))
    headers.set("Content-Type", "application/json")
  try {
    const res = await sessionFetch(path, { ...init, headers })
    return (res.status === 204 ? undefined : await res.json()) as T
  } catch (err) {
    if (init.signal?.aborted) throw err
    throw new Error(describe(err), { cause: err })
  }
}

// --- trading status (shared by the header and the Wallets tab) ---

export type TradingStatus = {
  mode: "testnet" | "live"
  enabled: boolean
  configured: boolean
  can_trade: boolean
  limits: {
    max_order_usdt: number
    daily_cap_usdt: number
    allowed_symbols: string[]
  }
  used_today_usdt: number
}

export type TradingState = {
  status?: TradingStatus
  error?: string
  /** A kill-switch change is in flight. */
  pending: boolean
}

let trading: TradingState = { pending: false }
const tradingListeners = new Set<() => void>()
let tradingTimer: ReturnType<typeof setInterval> | undefined

function setTrading(next: TradingState) {
  trading = next
  tradingListeners.forEach((l) => l())
}

export function refreshTradingStatus(): Promise<void> {
  return apiFetch<TradingStatus>("/api/trading/status").then(
    (status) => {
      if (!trading.pending) setTrading({ status, pending: false })
    },
    (err: Error) => setTrading({ ...trading, error: err.message })
  )
}

/** Kill switch: optimistic update, rolled back if the server refuses. */
export async function setTradingEnabled(enabled: boolean): Promise<void> {
  const before = trading.status
  if (!before || trading.pending) return
  setTrading({ status: { ...before, enabled }, pending: true })
  try {
    const status = await apiFetch<TradingStatus>("/api/trading/enabled", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    })
    setTrading({ status, pending: false })
  } catch (err) {
    setTrading({
      status: before,
      error: (err as Error).message,
      pending: false,
    })
  }
}

function subscribeTrading(cb: () => void) {
  if (!tradingListeners.size) {
    void refreshTradingStatus()
    tradingTimer = setInterval(refreshTradingStatus, 30_000)
  }
  tradingListeners.add(cb)
  return () => {
    tradingListeners.delete(cb)
    if (!tradingListeners.size) clearInterval(tradingTimer)
  }
}

/** GET /api/trading/status, shared across components and polled every 30s while anything is mounted. */
export function useTradingStatus(): TradingState {
  return useSyncExternalStore(subscribeTrading, () => trading)
}
