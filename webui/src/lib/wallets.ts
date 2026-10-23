import { useSyncExternalStore } from "react"

export type ChainKey = "ethereum" | "bsc" | "base" | "arbitrum"
export type Wallet = {
  id: string
  address: `0x${string}`
  label: string
  kind: "browser" | "watch"
  provider?: string
}

const STORAGE_KEY = "wallets.v1"
const ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/

function isWallet(w: unknown): w is Wallet {
  const v = w as Wallet
  return (
    typeof v?.id === "string" &&
    typeof v.address === "string" &&
    ADDRESS_RE.test(v.address) &&
    typeof v.label === "string" &&
    (v.kind === "browser" || v.kind === "watch")
  )
}

function load(): Wallet[] {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "[]")
    if (Array.isArray(saved)) return saved.filter(isWallet)
  } catch {
    /* storage unavailable or corrupt */
  }
  return []
}

let wallets: Wallet[] = load()
const listeners = new Set<() => void>()
const notify = () => listeners.forEach((l) => l())

function commit(next: Wallet[]) {
  wallets = next
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
  } catch {
    /* storage unavailable: keep in memory */
  }
  notify()
}

// Another tab changed the list.
function onStorage(e: StorageEvent) {
  if (e.key !== STORAGE_KEY) return
  wallets = load()
  notify()
}

function subscribe(cb: () => void) {
  if (!listeners.size) window.addEventListener("storage", onStorage)
  listeners.add(cb)
  return () => {
    listeners.delete(cb)
    if (!listeners.size) window.removeEventListener("storage", onStorage)
  }
}

const getSnapshot = () => wallets

export function hasWallet(address: string): boolean {
  const a = address.toLowerCase()
  return wallets.some((w) => w.address.toLowerCase() === a)
}

/** Adds a wallet unless its address is already listed. */
function add(w: Omit<Wallet, "id">) {
  if (hasWallet(w.address)) return
  commit([...wallets, { ...w, id: crypto.randomUUID() }])
}

function remove(id: string) {
  commit(wallets.filter((w) => w.id !== id))
}

function rename(id: string, label: string) {
  commit(wallets.map((w) => (w.id === id ? { ...w, label } : w)))
}

/** Saved wallets (localStorage "wallets.v1"), shared by every component that uses this hook. */
export function useWallets(): {
  wallets: Wallet[]
  add(w: Omit<Wallet, "id">): void
  remove(id: string): void
  rename(id: string, label: string): void
} {
  const list = useSyncExternalStore(subscribe, getSnapshot)
  return { wallets: list, add, remove, rename }
}
