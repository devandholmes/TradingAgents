import { useSyncExternalStore } from "react"

import { apiFetch, describe } from "@/lib/api"

// --- GET /api/health: on load, then every 60 s. The only polled endpoint in the app. ---

export type Health = {
  ok: boolean
  engine?: {
    alive: boolean
    restarts: number
    last_bar_at: string | null
    lag_s: number | null
  }
  db?: boolean
  streams?: { price?: string; account?: string }
}

export type HealthState = { health: Health | null; error?: string }

const EVERY_MS = 60_000
/** A bar this late means the engine is behind: 1m bars should land within a minute or two. */
export const LAG_WARN_S = 120

let state: HealthState = { health: null }
const listeners = new Set<() => void>()
let timer: ReturnType<typeof setInterval> | undefined

function check() {
  if (document.hidden) return // a hidden tab doesn't need it; the next tick catches up
  apiFetch<Health>("/api/health").then(
    (health) => set({ health }),
    (err) => set({ health: null, error: describe(err) })
  )
}

function set(next: HealthState) {
  state = next
  listeners.forEach((l) => l())
}

function subscribe(cb: () => void) {
  listeners.add(cb)
  if (!timer) {
    check()
    timer = setInterval(check, EVERY_MS)
  }
  return () => {
    listeners.delete(cb)
    if (listeners.size) return
    clearInterval(timer)
    timer = undefined
  }
}

export function useHealth(): HealthState {
  return useSyncExternalStore(subscribe, () => state)
}
