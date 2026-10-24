import { memo, useEffect, useRef, useState } from "react"
import type {
  IChartApi,
  ISeriesApi,
  ISeriesMarkersPluginApi,
  LineData,
  Time,
  UTCTimestamp,
} from "lightweight-charts"

import { fmtIndexed } from "@/components/strategies/format"
import type { Point } from "@/lib/strategies"
import { cn } from "@/lib/utils"

// Mirrors the theme tokens (canvas can't read CSS variables), same as PriceChart.
const GRID = "rgba(255,255,255,0.05)"
const BASELINE = "rgba(255,255,255,0.28)"
const TEXT = "#9299a2" // --muted-foreground

// Shared with PriceChart: one lightweight-charts chunk, fetched when this module loads.
const chartLib = import("lightweight-charts")

export type CompareLine = { id: string; color: string }
export type HoverValues = { time: number; values: Map<string, number> } | null

type Entry = {
  series: ISeriesApi<"Line">
  data: Point[]
  markers?: ISeriesMarkersPluginApi<Time>
}

/** Strictly ascending times; the chart throws on anything else. */
function toLine(points: Point[]): LineData<UTCTimestamp>[] {
  const out: LineData<UTCTimestamp>[] = []
  let last = -Infinity
  for (const p of points) {
    if (!(p.t > last) || !Number.isFinite(p.v)) continue
    out.push({ time: p.t as UTCTimestamp, value: p.v })
    last = p.t
  }
  return out
}

/** Overlaid normalised equity curves (100 = window start), one line per instance. */
export const CompareChart = memo(function CompareChart({
  lines,
  curves,
  liveSince,
  onHover,
  className,
  label,
}: {
  lines: CompareLine[]
  curves: Record<string, Point[] | undefined>
  /** Unix seconds: mark where backtest ends and live paper trading starts (single-line use). */
  liveSince?: number
  onHover?: (h: HoverValues) => void
  className?: string
  label: string
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const entries = useRef(new Map<string, Entry>())
  const hoverRef = useRef(onHover)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    hoverRef.current = onHover
  }, [onHover])

  // Create the chart once.
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    let cancelled = false
    const series = entries.current
    void chartLib.then(({ ColorType, CrosshairMode, createChart }) => {
      if (cancelled) return
      const chart = (chartRef.current = createChart(el, {
        autoSize: true,
        layout: {
          background: { type: ColorType.Solid, color: "transparent" },
          textColor: TEXT,
          fontFamily:
            "'Geist Mono Variable', ui-monospace, Consolas, monospace",
          fontSize: 11,
          attributionLogo: false,
        },
        grid: { vertLines: { visible: false }, horzLines: { color: GRID } },
        crosshair: { mode: CrosshairMode.Magnet },
        rightPriceScale: { borderVisible: false },
        timeScale: { borderVisible: false, timeVisible: true },
        handleScroll: false,
        handleScale: false,
      }))
      chart.subscribeCrosshairMove((param) => {
        const cb = hoverRef.current
        if (!cb) return
        if (param.time === undefined) return cb(null)
        const values = new Map<string, number>()
        for (const [id, e] of series) {
          const d = param.seriesData.get(e.series) as LineData | undefined
          if (d?.value != null) values.set(id, d.value)
        }
        cb({ time: param.time as number, values })
      })
      setReady(true)
    })
    return () => {
      cancelled = true
      series.clear()
      chartRef.current?.remove()
      chartRef.current = null
    }
  }, [])

  // Sync series with the selection and their data: a live append or a moved last point uses
  // update(), anything else setData.
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !ready) return
    void chartLib.then(({ LineSeries, LineStyle, createSeriesMarkers }) => {
      if (chartRef.current !== chart) return
      const map = entries.current
      const wanted = new Set(lines.map((l) => l.id))
      let reshaped = false
      for (const [id, e] of map)
        if (!wanted.has(id)) {
          chart.removeSeries(e.series)
          map.delete(id)
          reshaped = true
        }
      for (const { id, color } of lines) {
        let e = map.get(id)
        if (!e) {
          const series = chart.addSeries(LineSeries, {
            color,
            lineWidth: 2,
            priceLineVisible: false,
            crosshairMarkerRadius: 4,
            crosshairMarkerBorderColor: "#1b1f25",
            priceFormat: {
              type: "custom",
              formatter: fmtIndexed,
              minMove: 0.01,
            },
          })
          series.createPriceLine({
            price: 100,
            color: BASELINE,
            lineWidth: 1,
            lineStyle: LineStyle.Dashed,
            axisLabelVisible: false,
          })
          e = { series, data: [] }
          map.set(id, e)
          reshaped = true
        } else {
          e.series.applyOptions({ color })
        }
        const pts = curves[id] ?? []
        if (pts === e.data) continue
        const appended =
          e.data.length > 0 &&
          pts.length === e.data.length + 1 &&
          pts[pts.length - 2] === e.data[e.data.length - 1]
        const moved =
          pts.length > 1 &&
          pts.length === e.data.length &&
          pts[pts.length - 2] === e.data[e.data.length - 2] &&
          pts[pts.length - 1].t === e.data[e.data.length - 1].t
        if (appended || moved) {
          const p = pts[pts.length - 1]
          e.series.update({ time: p.t as UTCTimestamp, value: p.v })
        } else {
          e.series.setData(toLine(pts))
          reshaped = true
        }
        e.data = pts
        if (liveSince && lines.length === 1) {
          const at = pts.find((p) => p.t >= liveSince)
          e.markers ??= createSeriesMarkers(e.series, [])
          e.markers.setMarkers(
            at
              ? [
                  {
                    time: at.t as UTCTimestamp,
                    position: "aboveBar",
                    shape: "arrowDown",
                    color: TEXT,
                    text: "Live",
                  },
                ]
              : []
          )
        }
      }
      if (reshaped) chart.timeScale().fitContent()
    })
  }, [lines, curves, liveSince, ready])

  return (
    <div
      ref={containerRef}
      role="img"
      aria-label={label}
      className={cn("relative min-h-0", className)}
    />
  )
})
