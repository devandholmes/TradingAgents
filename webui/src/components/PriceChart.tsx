import { useEffect, useRef, useState } from "react"
import type { IChartApi, ISeriesApi, UTCTimestamp } from "lightweight-charts"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardAction,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import {
  INTERVALS,
  fmtPct,
  fmtPrice,
  pricePrecision,
  useApi,
  useBinanceStream,
  type Indicators,
  type Interval,
  type Kline,
} from "@/lib/market"
import { cn } from "@/lib/utils"

// Canvas colours can't read CSS variables, so mirror the theme tokens here.
const UP = "#2fcf8d" // --up
const DOWN = "#fb575d" // --down
const GRID = "rgba(255,255,255,0.05)"
const TEXT = "#9299a2" // --muted-foreground

type Charts = {
  candles: ISeriesApi<"Candlestick">
  volume: ISeriesApi<"Histogram">
  precision: number
}

type BinanceKline = {
  t: number
  o: string
  h: string
  l: string
  c: string
  v: string
}

// Split out of the entry chunk (~160 kB); the fetch starts as soon as this module loads.
const chartLib = import("lightweight-charts")

const volumeColor = (open: number, close: number) =>
  close >= open ? `${UP}55` : `${DOWN}55`

export function PriceChart({
  symbol,
  interval,
  onIntervalChange,
}: {
  symbol: string
  interval: Interval
  onIntervalChange: (i: Interval) => void
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const seriesRef = useRef<Charts | null>(null)
  const key = `${symbol}@${interval}`
  const [live, setLive] = useState<{
    key: string
    close: number
    open: number
  } | null>(null)

  const history = useApi<Kline[]>(
    `/api/klines?symbol=${symbol}&interval=${interval}&limit=500`
  )
  const indicators = useApi<Indicators>(
    `/api/indicators?symbol=${symbol}&interval=${interval}`,
    60_000
  )

  const [chartReady, setChartReady] = useState(false)

  // Create the chart once.
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    let chart: IChartApi | undefined
    let cancelled = false
    void chartLib.then(
      ({
        CandlestickSeries,
        ColorType,
        CrosshairMode,
        HistogramSeries,
        createChart,
      }) => {
        if (cancelled) return
        chart = createChart(el, {
          autoSize: true,
          layout: {
            background: { type: ColorType.Solid, color: "transparent" },
            textColor: TEXT,
            fontFamily: "'Geist Mono Variable', ui-monospace, Consolas, monospace",
            fontSize: 11,
          },
          grid: { vertLines: { color: GRID }, horzLines: { color: GRID } },
          crosshair: { mode: CrosshairMode.Normal },
          rightPriceScale: { borderColor: GRID },
          timeScale: {
            borderColor: GRID,
            timeVisible: true,
            secondsVisible: false,
          },
        })
        const candles = chart.addSeries(CandlestickSeries, {
          upColor: UP,
          downColor: DOWN,
          borderVisible: false,
          wickUpColor: UP,
          wickDownColor: DOWN,
        })
        const volume = chart.addSeries(HistogramSeries, {
          priceFormat: { type: "volume" },
          priceScaleId: "",
          lastValueVisible: false,
          priceLineVisible: false,
        })
        volume
          .priceScale()
          .applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } })
        seriesRef.current = { candles, volume, precision: 2 }
        setChartReady(true)
      }
    )
    return () => {
      cancelled = true
      seriesRef.current = null
      chart?.remove()
    }
  }, [])

  // Replace the dataset when history for a new symbol/interval arrives; clear it while loading so
  // live updates for the new stream never collide with the previous series' timestamps.
  useEffect(() => {
    const s = seriesRef.current
    if (!s) return
    const rows = history.data ?? []
    s.candles.setData(
      rows.map((c) => ({
        time: c.time as UTCTimestamp,
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      }))
    )
    s.volume.setData(
      rows.map((c) => ({
        time: c.time as UTCTimestamp,
        value: c.volume,
        color: volumeColor(c.open, c.close),
      }))
    )
    if (rows.length) setPrecision(s, rows[rows.length - 1].close)
  }, [history.data, chartReady])

  useBinanceStream(
    `${symbol.toLowerCase()}@kline_${interval}`,
    (_stream, data) => {
      const k = (data as { k?: BinanceKline }).k
      const s = seriesRef.current
      if (!k || !s) return
      const bar = {
        time: (k.t / 1000) as UTCTimestamp,
        open: +k.o,
        high: +k.h,
        low: +k.l,
        close: +k.c,
      }
      try {
        s.candles.update(bar)
        s.volume.update({
          time: bar.time,
          value: +k.v,
          color: volumeColor(bar.open, bar.close),
        })
      } catch {
        return // frame older than the loaded history — ignore
      }
      setPrecision(s, bar.close)
      setLive({ key, close: bar.close, open: bar.open })
    }
  )

  const last = live?.key === key ? live : null

  return (
    <Card size="sm" className="min-h-[460px] gap-2 lg:min-h-0">
      <CardHeader className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <CardTitle className="flex items-baseline gap-3 font-mono">
          <h2 className="text-base">{symbol}</h2>
          {last ? (
            <span
              className={cn(
                "tabular-nums",
                last.close >= last.open ? "text-up" : "text-down"
              )}
            >
              {fmtPrice(last.close)}
            </span>
          ) : (
            <Skeleton className="h-4 w-20" />
          )}
        </CardTitle>
        <CardAction>
          <ToggleGroup
            type="single"
            size="sm"
            variant="outline"
            value={interval}
            onValueChange={(v) => v && onIntervalChange(v as Interval)}
            aria-label="Chart interval"
          >
            {INTERVALS.map((i) => (
              <ToggleGroupItem
                key={i}
                value={i}
                className="font-mono text-xs data-[state=on]:border-primary/60 data-[state=on]:text-primary"
              >
                {i}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </CardAction>
      </CardHeader>
      <CardContent className="relative min-h-0 flex-1">
        <div
          ref={containerRef}
          className="absolute inset-0 mx-(--card-spacing)"
        />
        {history.loading ? (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center gap-2 text-muted-foreground">
            <Spinner /> Loading candles…
          </div>
        ) : null}
        {history.error ? (
          <Alert
            variant="destructive"
            className="absolute top-2 left-1/2 w-auto max-w-[90%] -translate-x-1/2"
          >
            <AlertDescription>
              History unavailable: {history.error} Showing live candles only.
            </AlertDescription>
          </Alert>
        ) : null}
      </CardContent>
      <IndicatorStrip
        data={indicators.data}
        error={indicators.error}
        loading={indicators.loading}
      />
    </Card>
  )
}

function setPrecision(s: Charts, price: number) {
  const precision = pricePrecision(price)
  if (s.precision === precision) return
  s.precision = precision
  s.candles.applyOptions({
    priceFormat: { type: "price", precision, minMove: 10 ** -precision },
  })
}

function IndicatorStrip({
  data,
  error,
  loading,
}: {
  data?: Indicators
  error?: string
  loading: boolean
}) {
  if (!data) {
    return (
      <div className="flex min-h-9 items-center gap-3 border-t px-(--card-spacing) pt-2 text-xs text-muted-foreground">
        {loading ? (
          <Skeleton className="h-4 w-full" />
        ) : (
          `Indicators unavailable: ${error ?? "no data"}`
        )}
      </div>
    )
  }
  const trendTone =
    data.trend === "up"
      ? "text-up border-up/40"
      : data.trend === "down"
        ? "text-down border-down/40"
        : ""
  const rsiTone =
    data.rsi14 == null
      ? ""
      : data.rsi14 >= 70
        ? "text-down"
        : data.rsi14 <= 30
          ? "text-up"
          : ""
  const items: [string, string, string?][] = [
    ["RSI14", data.rsi14?.toFixed(1) ?? "—", rsiTone],
    ["MACD", fmtPrice(data.macd)],
    ["Signal", fmtPrice(data.macd_signal)],
    [
      "Hist",
      fmtPrice(data.macd_hist),
      (data.macd_hist ?? 0) >= 0 ? "text-up" : "text-down",
    ],
    ["SMA20", fmtPrice(data.sma20)],
    ["SMA50", fmtPrice(data.sma50)],
    ["EMA20", fmtPrice(data.ema20)],
    [
      "vs SMA20",
      data.sma20 ? fmtPct(((data.price - data.sma20) / data.sma20) * 100) : "—",
    ],
  ]
  return (
    <div className="flex min-h-9 flex-wrap items-center gap-x-4 gap-y-1 border-t px-(--card-spacing) pt-2 font-mono text-xs">
      <Badge variant="outline" className={cn("font-sans", trendTone)}>
        Trend {data.trend}
      </Badge>
      {items.map(([label, value, tone]) => (
        <span key={label} className="flex gap-1.5">
          <span className="text-muted-foreground">{label}</span>
          <span className={tone}>{value}</span>
        </span>
      ))}
      {error ? <span className="text-down">stale: {error}</span> : null}
    </div>
  )
}
