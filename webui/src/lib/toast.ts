import { useSyncExternalStore } from "react"

// --- toasts: a tiny in-memory store rendered by components/Toaster.tsx ---

export type ToastKind = "success" | "error" | "info" | "warn"
export type Toast = {
  id: number
  kind: ToastKind
  title: string
  body?: string
  /** Same key replaces the earlier toast instead of stacking a duplicate. */
  key?: string
}

const MAX = 4
let toasts: Toast[] = []
let nextId = 1
const listeners = new Set<() => void>()

function set(next: Toast[]) {
  toasts = next
  listeners.forEach((l) => l())
}

export function toast(t: {
  kind: ToastKind
  title: string
  body?: string
  key?: string
}): void {
  const kept = t.key ? toasts.filter((x) => x.key !== t.key) : toasts
  set([...kept, { ...t, id: nextId++ }].slice(-MAX))
}

export function dismissToast(id: number): void {
  set(toasts.filter((t) => t.id !== id))
}

function subscribe(cb: () => void) {
  listeners.add(cb)
  return () => listeners.delete(cb)
}

export function useToasts(): Toast[] {
  return useSyncExternalStore(subscribe, () => toasts)
}
