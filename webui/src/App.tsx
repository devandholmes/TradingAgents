import {
  Component,
  Suspense,
  lazy,
  memo,
  useCallback,
  useEffect,
  useState,
  type ReactNode,
} from "react"

import { Assistant } from "@/components/Assistant"
import { PriceChart } from "@/components/PriceChart"
import { Toaster } from "@/components/Toaster"
import { TradingControl } from "@/components/TradingControl"
import { Trends } from "@/components/Trends"
import { Watchlist } from "@/components/Watchlist"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { useStreamStatus, type Interval } from "@/lib/market"
import { installChatShortcut, navigate, useRoute, type Tab } from "@/lib/nav"
import { cn } from "@/lib/utils"

// Lazy chunks keep viem, react-markdown and the workspace code out of the main bundle.
const loadAssistant = () => import("@/components/assistant/AssistantWorkspace")
const loadPortfolio = () => import("@/components/Portfolio")
const loadWallets = () => import("@/components/Wallets")
const AssistantWorkspace = lazy(loadAssistant)
const Portfolio = lazy(loadPortfolio)
const Wallets = lazy(loadWallets)

const TABS: { value: Tab; label: string; preload?: () => Promise<unknown> }[] =
  [
    { value: "dashboard", label: "Dashboard" },
    { value: "assistant", label: "Assistant", preload: loadAssistant },
    { value: "portfolio", label: "Portfolio", preload: loadPortfolio },
    { value: "wallets", label: "Wallets", preload: loadWallets },
  ]

export default function App() {
  const { tab } = useRoute()
  useEffect(installChatShortcut, [])

  return (
    <>
      <Tabs
        value={tab}
        onValueChange={(t) => navigate(t as Tab)}
        className="flex min-h-svh flex-col gap-0 lg:h-svh"
      >
        <header className="flex flex-wrap items-center gap-x-6 border-b px-3 sm:flex-nowrap sm:px-4">
          <a
            href="#/"
            className="flex h-12 shrink-0 items-center gap-2 rounded-md"
            aria-label="TradingAgents, go to Dashboard"
          >
            <BrandMark />
            <span className="text-title font-semibold tracking-tight">
              TradingAgents
            </span>
          </a>
          <TabsList
            variant="line"
            aria-label="Sections"
            className="order-last -mx-3 w-[calc(100%+1.5rem)] [scrollbar-width:none] justify-start overflow-x-auto overflow-y-hidden px-2 group-data-horizontal/tabs:h-10 sm:order-none sm:mx-0 sm:w-auto sm:px-0 sm:group-data-horizontal/tabs:h-12"
          >
            {TABS.map((t) => (
              <TabsTrigger
                key={t.value}
                value={t.value}
                className="flex-none px-2.5"
                // Fetch the lazy chunk on intent so the switch feels instant.
                onPointerEnter={t.preload && (() => void t.preload?.())}
                onFocus={t.preload && (() => void t.preload?.())}
              >
                {t.label}
              </TabsTrigger>
            ))}
          </TabsList>
          <div className="ml-auto flex h-12 items-center gap-4">
            <MarketStatus />
            <TradingControl />
          </div>
        </header>
        {/* forceMount keeps the chart and the quick chat alive on other tabs. */}
        <TabsContent
          value="dashboard"
          tabIndex={-1}
          forceMount
          className="flex min-h-0 flex-col data-[state=inactive]:hidden"
        >
          <Dashboard />
        </TabsContent>
        <TabsContent value="assistant" tabIndex={-1} className="flex min-h-0 flex-col">
          <LazyTab>
            <AssistantWorkspace />
          </LazyTab>
        </TabsContent>
        <TabsContent value="portfolio" tabIndex={-1} className="min-h-0 overflow-y-auto">
          <LazyTab>
            <Portfolio />
          </LazyTab>
        </TabsContent>
        <TabsContent value="wallets" tabIndex={-1} className="min-h-0 overflow-y-auto">
          <LazyTab>
            <Wallets />
          </LazyTab>
        </TabsContent>
      </Tabs>
      <Toaster />
    </>
  )
}

/** A lazy tab whose chunk fails to load (e.g. stale after a rebuild) offers a reload instead of blanking the app. */
class TabBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  render() {
    if (!this.state.failed) return this.props.children
    return (
      <div role="alert" className="m-auto flex flex-col items-center gap-3 p-8 text-center text-sm text-muted-foreground">
        <p>This tab failed to load. The app was probably updated.</p>
        <Button size="sm" onClick={() => location.reload()}>
          Reload
        </Button>
      </div>
    )
  }
}

function LazyTab({ children }: { children: ReactNode }) {
  return (
    <TabBoundary>
      <Suspense fallback={<TabFallback />}>{children}</Suspense>
    </TabBoundary>
  )
}

/** Three candles: the product in one glyph. */
function BrandMark() {
  return (
    <svg
      viewBox="0 0 16 16"
      aria-hidden
      className="size-4 text-primary"
      fill="currentColor"
    >
      <rect x="1" y="6" width="3.5" height="6" rx="1" />
      <rect x="6.25" y="2" width="3.5" height="9" rx="1" />
      <rect x="11.5" y="4" width="3.5" height="10" rx="1" opacity=".45" />
    </svg>
  )
}

function MarketStatus() {
  const status = useStreamStatus()
  return (
    <span
      className="flex items-center gap-1.5 text-xs text-muted-foreground"
      role="status"
      title="Binance market-data stream"
    >
      <span
        className={cn(
          "size-1.5 rounded-full",
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
          ? "Reconnecting…"
          : "Connecting…"}
    </span>
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
