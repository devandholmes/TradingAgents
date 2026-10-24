import { useMemo, useState } from "react"
import { PlayIcon } from "lucide-react"

import { CompareChart } from "@/components/strategies/CompareChart"
import {
  SERIES_COLORS,
  fmtDay,
  fmtDrawdown,
  fmtNum,
  fmtRate,
  fmtUsdt,
  fmtWhen,
  tone,
} from "@/components/strategies/format"
import { FamilyBadge } from "@/components/strategies/Leaderboard"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { fmtPct, fmtPrice } from "@/lib/market"
import {
  WINDOWS,
  toSec,
  useEquity,
  useInstance,
  type Fill,
  type LeaderRow,
  type Metrics,
} from "@/lib/strategies"
import { cn } from "@/lib/utils"

const METRIC_ROWS: [string, (m: Metrics) => string, ((m: Metrics) => string)?][] = [
  ["Return", (m) => fmtPct(m.return_pct), (m) => tone(m.return_pct)],
  ["Sharpe", (m) => fmtNum(m.sharpe)],
  ["Sortino", (m) => fmtNum(m.sortino)],
  ["Max drawdown", (m) => fmtDrawdown(m.max_drawdown_pct)],
  ["Win rate", (m) => fmtRate(m.win_rate)],
  ["Trades", (m) => (m.trades == null ? "—" : String(m.trades))],
  ["Time in market", (m) => (m.exposure_pct == null ? "—" : `${m.exposure_pct.toFixed(0)}%`)],
]

const WINDOW_LABEL: Record<string, string> = {
  "7d": "7 days",
  "30d": "30 days",
  "90d": "90 days",
  "1y": "1 year",
  live: "Live",
}

/** One instance: what it does, how it scored per window, the year's curve and its fills. */
export function DetailSheet({
  row,
  benchmark,
  windowLabel,
  onClose,
  onRun,
}: {
  row: LeaderRow | null
  /** Buy and hold on the same symbol and interval, in the leaderboard's window. */
  benchmark: LeaderRow | null
  windowLabel: string
  onClose: () => void
  onRun: (row: LeaderRow) => void
}) {
  return (
    <Sheet open={!!row} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full gap-0 overflow-y-auto data-[side=right]:sm:max-w-xl">
        {row && (
          <Detail row={row} benchmark={benchmark} windowLabel={windowLabel} onRun={onRun} />
        )}
      </SheetContent>
    </Sheet>
  )
}

function Detail({
  row,
  benchmark,
  windowLabel,
  onRun,
}: {
  row: LeaderRow
  benchmark: LeaderRow | null
  windowLabel: string
  onRun: (row: LeaderRow) => void
}) {
  const id = row.instance_id
  const detail = useInstance(id, row.updated_at)
  const ids = useMemo(() => [id], [id])
  const curves = useEquity(ids, "1y", 300)
  const lines = useMemo(() => [{ id, color: SERIES_COLORS[0] }], [id])
  const d = detail.data
  const liveSince = row.live_since ? toSec(row.live_since) : undefined

  return (
    <>
      <SheetHeader className="gap-2 border-b pr-12">
        <SheetTitle className="flex flex-wrap items-center gap-2 text-base">
          {row.name}
          <FamilyBadge family={row.family} />
        </SheetTitle>
        <SheetDescription className="font-mono text-xs">
          {row.symbol} {row.interval}
          {row.live_since
            ? `, paper trading live since ${fmtDay(row.live_since)}`
            : ", backtest only so far"}
        </SheetDescription>
      </SheetHeader>

      <div className="flex flex-col gap-5 p-4">
        {detail.error && (
          <Alert variant="destructive">
            <AlertDescription>Details unavailable: {detail.error}</AlertDescription>
          </Alert>
        )}

        <section aria-labelledby="detail-about" className="flex flex-col gap-2">
          <h3 id="detail-about" className="sr-only">
            About
          </h3>
          {d ? (
            <p className="max-w-[65ch] text-sm/relaxed text-pretty">{d.description}</p>
          ) : (
            <Skeleton className="h-10 w-full" />
          )}
          {d && Object.keys(d.params ?? {}).length > 0 && (
            <dl className="flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs">
              {Object.entries(d.params).map(([k, v]) => (
                <div key={k} className="flex gap-1.5">
                  <dt className="text-muted-foreground">{k}</dt>
                  <dd>{typeof v === "object" ? JSON.stringify(v) : String(v)}</dd>
                </div>
              ))}
            </dl>
          )}
        </section>

        <section aria-labelledby="detail-curve" className="flex flex-col gap-2">
          <div className="flex items-baseline justify-between gap-2">
            <h3 id="detail-curve" className="text-title font-semibold">
              Last year
            </h3>
            <span className="text-2xs text-muted-foreground">
              100 = a year ago{liveSince ? "; the arrow marks the switch to live" : ""}
            </span>
          </div>
          <CompareChart
            lines={lines}
            curves={curves}
            liveSince={liveSince}
            className="h-48"
            label={`${row.name} equity over the last year`}
          />
        </section>

        <section aria-labelledby="detail-metrics" className="flex flex-col gap-2">
          <h3 id="detail-metrics" className="text-title font-semibold">
            By window
          </h3>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-2xs text-muted-foreground">
                <tr className="[&>th]:pb-1 [&>th]:font-normal">
                  <th scope="col" className="text-left">
                    <span className="sr-only">Metric</span>
                  </th>
                  {WINDOWS.map((w) => (
                    <th
                      key={w}
                      scope="col"
                      className={cn("px-2 text-right", w === "live" && "text-foreground")}
                    >
                      {WINDOW_LABEL[w]}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="font-mono">
                {METRIC_ROWS.map(([label, fmt, cls]) => (
                  <tr key={label} className="[&>td]:border-t [&>td]:border-border/60 [&>td]:py-1">
                    <th scope="row" className="text-left font-sans font-normal text-muted-foreground">
                      {label}
                    </th>
                    {WINDOWS.map((w) => {
                      const m = d?.metrics?.[w]
                      return (
                        <td
                          key={w}
                          className={cn(
                            "px-2 text-right",
                            w === "live" && "bg-surface/60",
                            m && cls?.(m)
                          )}
                        >
                          {m ? fmt(m) : d ? "—" : <Skeleton className="ml-auto h-3 w-8" />}
                        </td>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {benchmark && benchmark.instance_id !== row.instance_id && (
            <p className="text-xs">
              {windowLabel[0].toUpperCase() + windowLabel.slice(1)}:{" "}
              <span className={cn("font-mono", tone(row.return_pct))}>{fmtPct(row.return_pct)}</span>{" "}
              against{" "}
              <span className={cn("font-mono", tone(benchmark.return_pct))}>
                {fmtPct(benchmark.return_pct)}
              </span>{" "}
              for buy and hold on the same market.
            </p>
          )}
          <p className="text-2xs text-muted-foreground">
            Calendar windows replay the backtest up to the live start and continue with live paper
            bars; Live counts only bars traded since then. Every instance starts from 1,000 USDT
            with 0.1% fees and fills at the next bar's open.
          </p>
        </section>

        <Fills fills={d?.recent_fills} />
      </div>

      <SheetFooter className="border-t">
        <Button onClick={() => onRun(row)}>
          <PlayIcon data-icon="inline-start" />
          Run with the bot
        </Button>
      </SheetFooter>
    </>
  )
}

function Fills({ fills }: { fills?: Fill[] }) {
  const [source, setSource] = useState<"live" | "backtest">("live")
  const counts = useMemo(() => {
    const c = { live: 0, backtest: 0 }
    for (const f of fills ?? []) c[f.source]++
    return c
  }, [fills])
  const shown = (fills ?? []).filter((f) => f.source === source)

  return (
    <section aria-labelledby="detail-fills" className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <h3 id="detail-fills" className="text-title font-semibold">
          Recent fills
        </h3>
        <ToggleGroup
          type="single"
          size="sm"
          variant="outline"
          value={source}
          onValueChange={(v) => v && setSource(v as "live" | "backtest")}
          aria-label="Fill source"
        >
          <ToggleGroupItem value="live" className="text-xs">
            Live paper {counts.live}
          </ToggleGroupItem>
          <ToggleGroupItem value="backtest" className="text-xs">
            Backtest {counts.backtest}
          </ToggleGroupItem>
        </ToggleGroup>
      </div>
      {!fills ? (
        <Skeleton className="h-20 w-full" />
      ) : shown.length === 0 ? (
        <p className="py-3 text-xs text-muted-foreground">
          {source === "live"
            ? "No live fills yet. The instance trades when a closed bar changes its target."
            : "No backtest fills in the recent history."}
        </p>
      ) : (
        <table className="w-full text-xs">
          <thead className="text-2xs text-muted-foreground">
            <tr className="[&>th]:px-1.5 [&>th]:pb-1 [&>th]:font-normal">
              <th scope="col" className="text-left">Time</th>
              <th scope="col" className="text-left">Side</th>
              <th scope="col" className="text-right">Price</th>
              <th scope="col" className="text-right">USDT</th>
              <th scope="col" className="text-right">Exposure after</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {shown.map((f, i) => (
              <tr key={`${f.ts}-${i}`} className="[&>td]:border-t [&>td]:border-border/60 [&>td]:px-1.5 [&>td]:py-1">
                <td>{fmtWhen(f.ts)}</td>
                <td>
                  <Badge
                    variant="outline"
                    className={cn("h-4 px-1.5 text-2xs", f.side === "BUY" ? "text-up" : "text-down")}
                  >
                    {f.side === "BUY" ? "Buy" : "Sell"}
                  </Badge>
                </td>
                <td className="text-right">{fmtPrice(f.price)}</td>
                <td className="text-right">{fmtUsdt(f.quote)}</td>
                <td className="text-right">{Math.round(f.exposure_after * 100)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
