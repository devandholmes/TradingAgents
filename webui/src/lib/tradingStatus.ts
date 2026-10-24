import { useSyncExternalStore } from "react"

import { apiFetch } from "@/lib/api"

// --- trading status: ONE shared store, no polling ---
//
// GET /api/trading/status once when the first consumer mounts; after that it changes only through
// the kill switch (POST returns the new status), /ws/account "status" events (lib/account.ts calls
// applyStatusEvent), and refreshes lib/account.ts asks for on reconnect or after an order fills.
// A failed load retries with backoff until one succeeds.

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
  status: TradingStatus | null
  error?: string
  /** A kill-switch change is in flight. */
  pending: boolean
  refresh(): void
}

const refresh = () => void refreshTradingStatus()
let trading: TradingState = { status: null, pending: false, refresh }
const listeners = new Set<() => void>()
let loaded = false
let inflight: Promise<void> | null = null
let retryTimer: ReturnType<typeof setTimeout> | undefined
let failures = 0

function setTrading(next: Omit<TradingState, "refresh">) {
  trading = { ...next, refresh }
  listeners.forEach((l) => l())
}

/** Refetch GET /api/trading/status; concurrent calls share one request. */
export function refreshTradingStatus(): Promise<void> {
  loaded = true
  clearTimeout(retryTimer)
  inflight ??= apiFetch<TradingStatus>("/api/trading/status")
    .then(
      (status) => {
        failures = 0
        if (!trading.pending) setTrading({ status, pending: false })
      },
      (err: Error) => {
        setTrading({ ...trading, error: err.message })
        retryTimer = setTimeout(
          refreshTradingStatus,
          Math.min(60_000, 5000 * 2 ** failures++)
        )
      }
    )
    .finally(() => (inflight = null))
  return inflight
}

/** Merge a /ws/account `{type: "status", mode, enabled}` event. */
export function applyStatusEvent(ev: { mode?: string; enabled?: unknown }) {
  const s = trading.status
  if (!s || trading.pending || typeof ev.enabled !== "boolean") return
  if (ev.mode && ev.mode !== s.mode) return void refreshTradingStatus()
  if (ev.enabled !== s.enabled)
    setTrading({ ...trading, status: { ...s, enabled: ev.enabled } })
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

function subscribe(cb: () => void) {
  if (!loaded) void refreshTradingStatus()
  listeners.add(cb)
  return () => listeners.delete(cb)
}

const getTrading = () => trading

export function useTradingStatus(): TradingState {
  return useSyncExternalStore(subscribe, getTrading)
}
