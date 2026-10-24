import { useSyncExternalStore } from "react"

// --- hash routing: #/ (dashboard), #/assistant?run=<id>, #/portfolio, #/wallets ---

export const TABS = ["dashboard", "assistant", "portfolio", "wallets"] as const
export type Tab = (typeof TABS)[number]

function parse(hash: string): { tab: Tab; query: string } {
  const [path, query = ""] = hash.replace(/^#\/?/, "").split("?", 2)
  const tab = (TABS as readonly string[]).includes(path)
    ? (path as Tab)
    : "dashboard"
  return { tab, query }
}

export function navigate(tab: Tab, params?: Record<string, string>): void {
  const query = params ? new URLSearchParams(params).toString() : ""
  window.location.hash = `/${tab === "dashboard" ? "" : tab}${query ? `?${query}` : ""}`
}

function subscribeHash(cb: () => void) {
  window.addEventListener("hashchange", cb)
  return () => window.removeEventListener("hashchange", cb)
}

// Cached per hash so useSyncExternalStore sees a stable snapshot.
let lastHash: string | undefined
let lastRoute: { tab: Tab; params: URLSearchParams }

function getRoute() {
  const hash = window.location.hash
  if (hash !== lastHash) {
    const { tab, query } = parse(hash)
    lastHash = hash
    lastRoute = { tab, params: new URLSearchParams(query) }
  }
  return lastRoute
}

export function useRoute(): { tab: Tab; params: URLSearchParams } {
  return useSyncExternalStore(subscribeHash, getRoute)
}

// --- "/" focuses the visible chat input ---
//
// Chat inputs register here (dock and workspace); the shortcut picks the first one that is
// currently rendered, so it follows the active tab without knowing about it.

const chatInputs = new Set<HTMLElement>()

/** Register a chat input for the "/" shortcut; returns the unregister function (fits a React 19 ref callback). */
export function registerChatInput(el: HTMLElement | null): () => void {
  if (!el) return () => {}
  chatInputs.add(el)
  return () => chatInputs.delete(el)
}

function isTyping(target: EventTarget | null) {
  return (
    target instanceof HTMLElement &&
    (target.isContentEditable ||
      /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))
  )
}

/** Installs the global "/" shortcut; returns its cleanup. */
export function installChatShortcut(): () => void {
  const onKey = (e: KeyboardEvent) => {
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return
    if (isTyping(e.target)) return
    const el = [...chatInputs].find((x) => x.checkVisibility())
    if (!el) return
    e.preventDefault()
    el.focus()
  }
  window.addEventListener("keydown", onKey)
  return () => window.removeEventListener("keydown", onKey)
}
