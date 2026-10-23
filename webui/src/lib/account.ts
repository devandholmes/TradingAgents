import { useEffect, useState, useSyncExternalStore } from "react"

import { apiFetch, describe, useTradingStatus } from "@/lib/api"

// --- API shapes (see contract: /api/account, /api/orders*, /api/trades/recent, /ws/account) ---

export type Balance = { asset: string; free: number; locked: number }
export type Order = {
  symbol: string
  order_id: number
  side: "BUY" | "SELL"
  type: string
  price: number
  orig_qty: number
  executed_qty: number
  status: string
  time: number
}
export type AuditEntry = {
  ts: string
  mode: "testnet" | "live"
  source: "user" | "agent"
  request: {
    symbol?: string
    side?: string
    type?: string
    quantity?: number | null
    quote_amount?: number | null
    price?: number | null
  }
  status: "rejected" | "filled" | "new" | "error"
  reason?: string
  order?: Partial<Order>
}

type AccountSnapshot = {
  mode: "testnet" | "live"
  balances: Balance[]
  update_time: number
}
type AccountMessage =
  | { type: "snapshot" | "balances"; balances: Balance[] }
  | { type: "order"; order: Order }
  | { type: "status"; state?: string }

export type AccountStatus = "loading" | "live" | "offline" | "unconfigured"

const EMPTY: Balance[] = []
const held = (b: Balance) => b.free + b.locked > 0

function wsUrl(path: string): string {
  return `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${path}`
}

type Live = {
  mode: "testnet" | "live" | null
  balances: Balance[]
  status: AccountStatus
  error?: string
  orderEvents: number
}

// --- /ws/account: ONE socket shared by every useBinanceAccount consumer ---
//
// Started by the first subscriber and kept open for IDLE_CLOSE_MS after the last one leaves, so
// switching tabs and back reuses both the socket and the cached snapshot (instant render, no
// refetch). Reconnects with exponential backoff (1s → 30s). `orderEvents` bumps on every order
// push (and every snapshot) so order/trade lists know when to refetch.

const IDLE_CLOSE_MS = 60_000
let account: Live = {
  mode: null,
  balances: EMPTY,
  status: "loading",
  orderEvents: 0,
}
const accountListeners = new Set<() => void>()
let accountSocket: WebSocket | undefined
let connection: AbortController | undefined // set while the account stream is wanted
let accountAttempt = 0
let accountRetry: ReturnType<typeof setTimeout> | undefined
let idleTimer: ReturnType<typeof setTimeout> | undefined

function updateAccount(fn: (s: Live) => Live) {
  account = fn(account)
  accountListeners.forEach((l) => l())
}

function retryAccount(error: string) {
  updateAccount((s) => ({ ...s, status: "offline", error }))
  const delay =
    Math.min(30_000, 1000 * 2 ** accountAttempt++) *
    (0.75 + Math.random() * 0.5)
  accountRetry = setTimeout(connectAccount, delay)
}

function onAccountMessage(msg: AccountMessage) {
  if (msg.type === "snapshot") {
    accountAttempt = 0
    updateAccount((s) => ({
      ...s,
      balances: msg.balances.filter(held),
      status: "live",
      error: undefined,
      orderEvents: s.orderEvents + 1,
    }))
  } else if (msg.type === "balances") {
    updateAccount((s) => {
      const byAsset = new Map(s.balances.map((b) => [b.asset, b]))
      for (const b of msg.balances) {
        if (held(b)) byAsset.set(b.asset, b)
        else byAsset.delete(b.asset)
      }
      return { ...s, balances: [...byAsset.values()] }
    })
  } else if (msg.type === "order") {
    updateAccount((s) => ({ ...s, orderEvents: s.orderEvents + 1 }))
  } else if (msg.type === "status" && msg.state === "offline") {
    // The server lost Binance's stream; its reconnect sends a fresh snapshot.
    updateAccount((s) => ({
      ...s,
      status: "offline",
      error: "Binance account stream reconnecting",
    }))
  }
}

async function connectAccount() {
  accountRetry = undefined
  const ctrl = (connection = new AbortController())
  try {
    const [snapshot, session] = await Promise.all([
      apiFetch<AccountSnapshot>("/api/account", { signal: ctrl.signal }),
      apiFetch<{ token: string }>("/api/session", { signal: ctrl.signal }),
    ])
    if (ctrl.signal.aborted) return
    updateAccount((s) => ({
      ...s,
      mode: snapshot.mode,
      balances: snapshot.balances.filter(held),
    }))
    const sock = (accountSocket = new WebSocket(wsUrl("/ws/account")))
    sock.onopen = () => sock.send(JSON.stringify({ token: session.token }))
    sock.onmessage = (ev) => {
      try {
        onAccountMessage(JSON.parse(ev.data))
      } catch {
        /* ignore malformed frame */
      }
    }
    sock.onclose = () => {
      if (accountSocket !== sock) return
      accountSocket = undefined
      retryAccount("Account stream disconnected — retrying")
    }
  } catch (err) {
    if (!ctrl.signal.aborted) retryAccount(describe(err))
  }
}

function disconnectAccount() {
  idleTimer = undefined
  connection?.abort()
  connection = undefined
  clearTimeout(accountRetry)
  accountRetry = undefined
  accountAttempt = 0
  const sock = accountSocket
  accountSocket = undefined // its onclose sees this and does not retry
  sock?.close()
  account = { ...account, status: "loading" }
}

function subscribeAccount(cb: () => void) {
  clearTimeout(idleTimer)
  idleTimer = undefined
  if (!connection) void connectAccount()
  accountListeners.add(cb)
  return () => {
    accountListeners.delete(cb)
    if (!accountListeners.size)
      idleTimer = setTimeout(disconnectAccount, IDLE_CLOSE_MS)
  }
}

const subscribeNothing = () => () => {}
const getAccount = () => account

/** Binance Spot balances: GET /api/account snapshot, then live updates over the shared /ws/account socket. */
export function useBinanceAccount(): Live {
  const { status: trading, error: tradingError } = useTradingStatus()
  const configured = trading?.configured
  const live = useSyncExternalStore(
    configured ? subscribeAccount : subscribeNothing,
    getAccount
  )

  if (configured === false)
    return {
      mode: trading?.mode ?? null,
      balances: EMPTY,
      status: "unconfigured",
      orderEvents: 0,
    }
  if (!trading)
    return tradingError
      ? { ...live, status: "offline", error: tradingError }
      : { ...live, status: "loading" }
  return { ...live, mode: live.mode ?? trading.mode }
}

/** GET a session-protected JSON endpoint whenever `path` or `version` changes; keeps the last data while refetching. */
export function useAccountQuery<T>(path: string | null, version: number) {
  const key = path ? `${path}#${version}` : ""
  const [state, setState] = useState<{ key: string; data?: T; error?: string }>(
    { key: "" }
  )

  useEffect(() => {
    if (!path) return
    const ctrl = new AbortController()
    apiFetch<T>(path, { signal: ctrl.signal }).then(
      (data) => setState({ key, data }),
      (err: Error) => {
        if (!ctrl.signal.aborted)
          setState((s) => ({ key, data: s.data, error: err.message }))
      }
    )
    return () => ctrl.abort()
  }, [key, path])

  return {
    data: path ? state.data : undefined,
    error: path && state.key === key ? state.error : undefined,
    loading: !!path && state.key !== key,
  }
}
