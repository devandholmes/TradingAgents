import { useState } from "react"

import { Assistant } from "@/components/Assistant"
import { PriceChart } from "@/components/PriceChart"
import { Trends } from "@/components/Trends"
import { Watchlist, type Tick } from "@/components/Watchlist"
import { useBinanceStream, type Interval, type WsStatus } from "@/lib/market"
import { cn } from "@/lib/utils"

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

type MiniTicker = { s: string; c: string; o: string }

export default function App() {
  const [symbols, setSymbols] = useState(loadSymbols)
  const [selected, setSelected] = useState(() => symbols[0] ?? "BTCUSDT")
  const [interval, setChartInterval] = useState<Interval>("1h")
  const [ticks, setTicks] = useState<Record<string, Tick>>({})
  const [klineStatus, setKlineStatus] = useState<WsStatus>("connecting")

  const tickerStatus = useBinanceStream("!miniTicker@arr", (_stream, data) => {
    if (!Array.isArray(data)) return
    const wanted = new Set(symbols)
    const hits = (data as MiniTicker[]).filter((t) => wanted.has(t.s))
    if (!hits.length) return
    setTicks((prev) => {
      const next = { ...prev }
      for (const t of hits) {
        const price = +t.c
        const open = +t.o
        next[t.s] = {
          price,
          prev: prev[t.s]?.price,
          pct: open ? ((price - open) / open) * 100 : 0,
        }
      }
      return next
    })
  })

  const updateSymbols = (fn: (s: string[]) => string[]) =>
    setSymbols((s) => {
      const next = fn(s)
      saveSymbols(next)
      return next
    })

  const statuses = [tickerStatus, klineStatus]
  const status: WsStatus = statuses.every((s) => s === "open")
    ? "open"
    : statuses.includes("closed")
      ? "closed"
      : "connecting"

  return (
    <div className="flex min-h-svh flex-col lg:h-svh">
      <header className="flex items-center gap-3 border-b px-4 py-2">
        <span className="font-mono text-sm font-semibold tracking-tight text-primary">
          TradingAgents
        </span>
        <span className="text-sm text-muted-foreground">Binance live</span>
        <span
          className="ml-auto flex items-center gap-2 text-xs text-muted-foreground"
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
      </header>
      <main className="grid flex-1 gap-2 p-2 lg:min-h-0 lg:grid-cols-[minmax(220px,260px)_minmax(0,1fr)_minmax(320px,400px)]">
        <Watchlist
          symbols={symbols}
          ticks={ticks}
          selected={selected}
          onSelect={setSelected}
          onAdd={(s) => updateSymbols((list) => [...list, s])}
          onRemove={(s) => updateSymbols((list) => list.filter((x) => x !== s))}
        />
        <PriceChart
          symbol={selected}
          interval={interval}
          onIntervalChange={setChartInterval}
          onStatus={setKlineStatus}
        />
        <div className="grid gap-2 lg:min-h-0 lg:grid-rows-[minmax(0,2fr)_minmax(0,3fr)]">
          <Trends onSelect={setSelected} />
          <Assistant symbol={selected} interval={interval} />
        </div>
      </main>
    </div>
  )
}
