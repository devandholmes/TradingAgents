import {
  Suspense,
  lazy,
  memo,
  useCallback,
  useState,
  useSyncExternalStore,
} from "react"

import { Assistant } from "@/components/Assistant"
import { PriceChart } from "@/components/PriceChart"
import { TradingControl } from "@/components/TradingControl"
import { Trends } from "@/components/Trends"
import { Watchlist } from "@/components/Watchlist"
import { Spinner } from "@/components/ui/spinner"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { useStreamStatus, type Interval } from "@/lib/market"
import { cn } from "@/lib/utils"

// Lazy chunks keep viem and the portfolio code out of the main bundle.
const loadPortfolio = () => import("@/components/Portfolio")
const loadWallets = () => import("@/components/Wallets")
const Portfolio = lazy(loadPortfolio)
const Wallets = lazy(loadWallets)

const TABS = [
  { value: "dashboard", label: "Dashboard" },
  { value: "portfolio", label: "Portfolio", preload: loadPortfolio },
  { value: "wallets", label: "Wallets", preload: loadWallets },
] as const
type Tab = (typeof TABS)[number]["value"]

// --- hash routing: #/ (dashboard), #/portfolio, #/wallets ---

function readTab(): Tab {
  const h = window.location.hash.replace(/^#\/?/, "")
  return h === "portfolio" || h === "wallets" ? h : "dashboard"
}

function subscribeHash(cb: () => void) {
  window.addEventListener("hashchange", cb)
  return () => window.removeEventListener("hashchange", cb)
}

function navigate(tab: string) {
  window.location.hash = tab === "dashboard" ? "/" : `/${tab}`
}

export default function App() {
  const tab = useSyncExternalStore(subscribeHash, readTab)
  const status = useStreamStatus()

  return (
    <Tabs
      value={tab}
      onValueChange={navigate}
      className="flex min-h-svh flex-col gap-0 lg:h-svh"
    >
      <header className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b px-4 py-2">
        <span className="font-mono text-sm font-semibold tracking-tight text-primary">
          TradingAgents
        </span>
        <TabsList variant="line" aria-label="Sections">
          {TABS.map((t) => (
            <TabsTrigger
              key={t.value}
              value={t.value}
              className="px-2"
              // Fetch the lazy chunk on intent so the switch feels instant.
              onPointerEnter={
                "preload" in t ? () => void t.preload() : undefined
              }
              onFocus={"preload" in t ? () => void t.preload() : undefined}
            >
              {t.label}
            </TabsTrigger>
          ))}
        </TabsList>
        <div className="ml-auto flex items-center gap-4">
          <TradingControl />
          <span
            className="flex items-center gap-2 text-xs text-muted-foreground"
            role="status"
            title="Binance market-data WebSocket"
          >
            <span
              className={cn(
                "size-2 rounded-full",
                status === "open"
                  ? "bg-up"
                  : status === "closed"
                    ? "bg-down"
                    : "animate-pulse bg-primary"
              )}
            />
            {status === "open"
              ? "Live"
              : status === "closed"
                ? "Disconnected — retrying"
                : "Connecting…"}
          </span>
        </div>
      </header>
      {/* forceMount keeps the chart, chat and analysis alive on other tabs. */}
      <TabsContent
        value="dashboard"
        forceMount
        className="flex min-h-0 flex-col data-[state=inactive]:hidden"
      >
        <Dashboard />
      </TabsContent>
      <TabsContent value="portfolio" className="min-h-0 overflow-y-auto">
        <Suspense fallback={<TabFallback />}>
          <Portfolio />
        </Suspense>
      </TabsContent>
      <TabsContent value="wallets" className="min-h-0 overflow-y-auto">
        <Suspense fallback={<TabFallback />}>
          <Wallets />
        </Suspense>
      </TabsContent>
    </Tabs>
  )
}

function TabFallback() {
  return (
    <div className="flex items-center justify-center gap-2 p-8 text-muted-foreground">
      <Spinner /> Loading…
    </div>
  )
}

// --- Dashboard ---

const DEFAULT_SYMBOLS = [
  "BTCUSDT",
  "ETHUSDT",
  "SOLUSDT",
  "BNBUSDT",
  "XRPUSDT",
  "DOGEUSDT",
]
const STORAGE_KEY = "watchlist.v1"

function loadSymbols(): string[] {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "null")
    if (Array.isArray(saved) && saved.every((s) => typeof s === "string"))
      return saved
  } catch {
    /* storage unavailable or corrupt */
  }
  return DEFAULT_SYMBOLS
}

function saveSymbols(symbols: string[]) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(symbols))
  } catch {
    /* storage unavailable */
  }
}

const Dashboard = memo(function Dashboard() {
  const [symbols, setSymbols] = useState(loadSymbols)
  const [selected, setSelected] = useState(() => symbols[0] ?? "BTCUSDT")
  const [interval, setChartInterval] = useState<Interval>("1h")

  const updateSymbols = useCallback(
    (fn: (s: string[]) => string[]) =>
      setSymbols((s) => {
        const next = fn(s)
        saveSymbols(next)
        return next
      }),
    []
  )
  const addSymbol = useCallback(
    (s: string) => updateSymbols((list) => [...list, s]),
    [updateSymbols]
  )
  const removeSymbol = useCallback(
    (s: string) => updateSymbols((list) => list.filter((x) => x !== s)),
    [updateSymbols]
  )

  return (
    <main className="grid flex-1 gap-2 p-2 lg:min-h-0 lg:grid-cols-[minmax(220px,260px)_minmax(0,1fr)_minmax(320px,400px)]">
      <Watchlist
        symbols={symbols}
        selected={selected}
        onSelect={setSelected}
        onAdd={addSymbol}
        onRemove={removeSymbol}
      />
      <PriceChart
        symbol={selected}
        interval={interval}
        onIntervalChange={setChartInterval}
      />
      <div className="grid gap-2 lg:min-h-0 lg:grid-rows-[minmax(0,2fr)_minmax(0,3fr)]">
        <Trends onSelect={setSelected} />
        <Assistant symbol={selected} interval={interval} />
      </div>
    </main>
  )
})
