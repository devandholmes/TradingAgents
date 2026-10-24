import { useEffect, useState, useSyncExternalStore } from "react"

import { apiFetch, describe } from "@/lib/api"
import { fmtPrice } from "@/lib/market"
import { retainStrategiesSocket } from "@/lib/strategies"
import { toast } from "@/lib/toast"
import { refreshTradingStatus } from "@/lib/tradingStatus"

// --- strategy bot: GET /api/bot once, then {type: "bot"} frames on /ws/strategies ---

export type BotTrade = {
  id: number
  ts: string
  instance_id: string
  side: "BUY" | "SELL"
  quote: number
  qty: number
  price: number
  /** false = paper-mirrored fill (below minNotional), true = a real Binance order. */
  real: boolean
  status: string
  order_id: number | null
  reason: string | null
}

export type BotActive = {
  instance_id: string
  name: string
  symbol: string
  interval: string
  size_usdt: number
  mode: "paper" | "live"
  activated_at: string
  /** The instance's current target exposure, 0..1; null until the engine has seeded it. */
  target: number | null
}

/** Exposure gaps up to this count as in sync (the server's TOLERANCE): never worth an order. */
export const SYNC_TOLERANCE = 0.005

export type BotStatus = {
  active: BotActive | null
  position: {
    exposure: number
    qty: number
    avg_price: number | null
    value: number
    unrealized_pnl: number
  }
  pnl: {
    realized: number
    unrealized: number
    total: number
    paper_total: number
  }
  trades: BotTrade[]
  /** False when the bot's exposure is off the target, e.g. after a rejected order: POST /api/bot/sync retries. */
  in_sync: boolean
  /** Target minus the bot's exposure, -1..1. */
  drift: number
}

export type BotState = {
  status: BotStatus | null
  error?: string
  /** An activate/deactivate request is in flight. */
  pending: boolean
}

let bot: BotState = { status: null, pending: false }
const listeners = new Set<() => void>()
let loading: Promise<void> | null = null
/** Trade ids already shown, so a status frame only toasts what is new. */
const seenTrades = new Set<number>()

function setBot(next: BotState) {
  bot = next
  listeners.forEach((l) => l())
}

function remember(s: BotStatus) {
  for (const t of s.trades ?? []) seenTrades.add(t.id)
}

function loadBot(): Promise<void> {
  loading ??= apiFetch<BotStatus>("/api/bot")
    .then(
      (status) => {
        remember(status)
        setBot({ ...bot, status, error: undefined })
      },
      (err) => setBot({ ...bot, error: describe(err) })
    )
    .finally(() => (loading = null))
  return loading
}

export function reloadBot() {
  void loadBot()
}

const fmtQty = (n: number) =>
  n.toLocaleString("en-US", { maximumFractionDigits: 8 })

function tradeToast(t: BotTrade) {
  const symbol = t.instance_id.split(":")[1] ?? ""
  const key = `bot:${t.id}`
  if (t.status === "rejected" || t.status === "error")
    return toast({
      kind: "error",
      key,
      title: `Bot ${t.side === "BUY" ? "buy" : "sell"} rejected`,
      body: t.reason ?? "The trading guard refused the order.",
    })
  toast({
    kind: "success",
    key,
    title: `Bot ${t.side === "BUY" ? "bought" : "sold"} ${fmtQty(t.qty)} ${symbol}`,
    body: `${t.real ? "Live order" : "Paper fill"} at ${fmtPrice(t.price)} for ${t.quote.toFixed(2)} USDT.`,
  })
}

function onBotFrame(msg: Record<string, unknown>) {
  if (msg.type === "reconnect") return void loadBot()
  const rest = { ...msg }
  delete rest.type
  const next = { ...bot.status, ...rest } as BotStatus
  for (const t of next.trades ?? [])
    if (!seenTrades.has(t.id)) {
      seenTrades.add(t.id)
      tradeToast(t)
      if (t.real) void refreshTradingStatus() // used_today_usdt moved
    }
  setBot({ ...bot, status: next })
}

// One socket retain for all consumers: the socket's listener set is keyed by function, so
// per-consumer retains of onBotFrame would let the first unmount drop everyone's frames.
let releaseSocket: (() => void) | undefined

function subscribe(cb: () => void) {
  if (!bot.status && !loading) void loadBot()
  listeners.add(cb)
  releaseSocket ??= retainStrategiesSocket(onBotFrame)
  return () => {
    listeners.delete(cb)
    if (listeners.size) return
    releaseSocket?.()
    releaseSocket = undefined
  }
}

/** Bot status, kept live over the shared /ws/strategies socket. */
export function useBot(): BotState {
  return useSyncExternalStore(subscribe, () => bot)
}

async function post(path: string, body: unknown): Promise<BotStatus> {
  setBot({ ...bot, pending: true })
  try {
    const status = await apiFetch<BotStatus>(path, {
      method: "POST",
      body: JSON.stringify(body),
    })
    remember(status)
    setBot({ status, pending: false })
    return status
  } catch (err) {
    setBot({ ...bot, pending: false })
    throw err
  }
}

/** Point the bot at one instance; the server picks paper or live from the size. Throws Error(detail). */
export async function activateBot(instanceId: string, sizeUsdt: number) {
  const s = await post("/api/bot/activate", {
    instance_id: instanceId,
    size_usdt: sizeUsdt,
  })
  const a = s.active
  toast({
    kind: "success",
    key: "bot:active",
    title: `Bot started in ${a?.mode === "live" ? "live" : "paper"} mode`,
    body: a
      ? `${a.name} on ${a.symbol} ${a.interval}, ${a.size_usdt} USDT at full exposure.`
      : undefined,
  })
}

/** Retry bringing the bot to its strategy's target, e.g. after a rejected order. Throws Error(detail). */
export async function syncBot() {
  const s = await post("/api/bot/sync", {})
  toast({
    kind: s.in_sync ? "success" : "error",
    key: "bot:sync",
    title: s.in_sync ? "Bot back in sync" : "Bot still out of sync",
    body: s.in_sync ? undefined : (s.trades[0]?.reason ?? undefined),
  })
}

/** What activating an instance starts from: its current target and what the bot already holds of it. */
export type BotPreview = {
  instance_id: string
  symbol: string
  target: number | null
  held_usdt: number
}

/** GET /api/bot/preview for the activate dialog; undefined while loading or when it failed. */
export function useBotPreview(instanceId: string): BotPreview | undefined {
  const [preview, setPreview] = useState<BotPreview>()
  useEffect(() => {
    const ctrl = new AbortController()
    apiFetch<BotPreview>(
      `/api/bot/preview?instance_id=${encodeURIComponent(instanceId)}`,
      { signal: ctrl.signal }
    ).then(setPreview, () => {})
    return () => ctrl.abort()
  }, [instanceId])
  return preview?.instance_id === instanceId ? preview : undefined
}

/** Stop the bot; `closePosition` sells what the bot holds. Throws Error(detail). */
export async function deactivateBot(closePosition: boolean) {
  await post("/api/bot/deactivate", { close_position: closePosition })
  toast({
    kind: "info",
    key: "bot:active",
    title: "Bot stopped",
    body: closePosition
      ? "Its position is being closed."
      : "Its position was left open.",
  })
}
