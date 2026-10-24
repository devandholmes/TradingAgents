import { memo } from "react"
import { ArrowDownIcon, ArrowUpIcon, PlayIcon } from "lucide-react"

import {
  SERIES_COLORS,
  fmtDrawdown,
  fmtNum,
  fmtRate,
  tone,
} from "@/components/strategies/format"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { fmtPct } from "@/lib/market"
import {
  FAMILY_LABEL,
  type Family,
  type LeaderRow,
  type Point,
} from "@/lib/strategies"
import { cn } from "@/lib/utils"

export type SortKey =
  | "name"
  | "return_pct"
  | "sharpe"
  | "sortino"
  | "max_drawdown_pct"
  | "win_rate"
  | "trades"
  | "exposure"
export type Sort = { key: SortKey; desc: boolean }

/** Fewer trades than this in the window: its ratios and return say little. */
export const MIN_TRADES = 3
export const lowSample = (r: LeaderRow) => (r.trades ?? 0) < MIN_TRADES
/** Keys a handful of lucky trades can top: low-sample rows sort below the rest on these. */
const SAMPLE_SENSITIVE = new Set<SortKey>(["return_pct", "sharpe", "sortino"])

/** Sort value: bigger is better for every key (drawdown flipped), nulls always last. */
export function sortRows(rows: LeaderRow[], { key, desc }: Sort): LeaderRow[] {
  if (key === "name")
    return rows.toSorted(
      (a, b) =>
        (desc ? -1 : 1) * a.name.localeCompare(b.name) ||
        a.instance_id.localeCompare(b.instance_id)
    )
  const val = (r: LeaderRow) => {
    const v = r[key]
    if (v == null || !Number.isFinite(v)) return null
    return key === "max_drawdown_pct" ? -Math.abs(v) : v
  }
  const sampled = SAMPLE_SENSITIVE.has(key)
  return rows.toSorted((a, b) => {
    if (sampled && lowSample(a) !== lowSample(b)) return lowSample(a) ? 1 : -1
    const x = val(a)
    const y = val(b)
    if (x === y) return a.instance_id.localeCompare(b.instance_id)
    if (x === null) return 1
    if (y === null) return -1
    return desc ? y - x : x - y
  })
}

const COLUMNS: { key: SortKey; label: string; title?: string }[] = [
  { key: "return_pct", label: "Return" },
  {
    key: "sharpe",
    label: "Sharpe",
    title: "Return per unit of volatility, annualised",
  },
  {
    key: "sortino",
    label: "Sortino",
    title: "Like Sharpe, counting only downside volatility",
  },
  {
    key: "max_drawdown_pct",
    label: "Max DD",
    title: "Worst peak-to-trough fall",
  },
  {
    key: "win_rate",
    label: "Win rate",
    title: "Share of closed trades that made money",
  },
  { key: "trades", label: "Trades" },
  { key: "exposure", label: "In market", title: "Current target exposure" },
]

/** Rows rendered before "Show all": 208 instances is cheap, but most views fit well under this. */
export const ROW_CAP = 60

export function Leaderboard({
  rows,
  sort,
  onSort,
  slots,
  compareFull,
  onToggle,
  onOpen,
  onRun,
  botId,
  curves,
  showAll,
  onShowAll,
}: {
  rows: LeaderRow[]
  sort: Sort
  onSort: (key: SortKey) => void
  /** instance_id -> compare slot (its colour). */
  slots: Map<string, number>
  compareFull: boolean
  onToggle: (id: string) => void
  onOpen: (id: string) => void
  onRun: (row: LeaderRow) => void
  botId: string | null
  curves: Record<string, Point[] | undefined>
  showAll: boolean
  onShowAll: () => void
}) {
  const shown = showAll ? rows : rows.slice(0, ROW_CAP)

  return (
    <div className="min-h-0 flex-1 overflow-auto">
      <table className="w-full min-w-[860px] border-separate border-spacing-0 text-xs">
        <caption className="sr-only">
          Strategy leaderboard, {rows.length} instances. Select up to{" "}
          {SERIES_COLORS.length} to compare.
        </caption>
        <thead className="sticky top-0 z-10 bg-card text-2xs text-muted-foreground">
          <tr className="[&>th]:border-b [&>th]:py-1.5 [&>th]:font-normal">
            <th scope="col" className="w-9 pl-3">
              <span className="sr-only">Compare</span>
            </th>
            <SortHeader
              label="Strategy"
              col="name"
              sort={sort}
              onSort={onSort}
              align="left"
            />
            <th scope="col" className="px-2 text-left">
              Market
            </th>
            {COLUMNS.map((c) => (
              <SortHeader
                key={c.key}
                label={c.label}
                title={c.title}
                col={c.key}
                sort={sort}
                onSort={onSort}
              />
            ))}
            <th scope="col" className="px-2 text-left">
              Equity
            </th>
            <th scope="col" className="pr-3">
              <span className="sr-only">Bot</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {shown.map((r) => (
            <Row
              key={r.instance_id}
              row={r}
              slot={slots.get(r.instance_id)}
              compareFull={compareFull}
              onToggle={onToggle}
              onOpen={onOpen}
              onRun={onRun}
              isBot={botId === r.instance_id}
              curve={curves[r.instance_id]}
            />
          ))}
        </tbody>
      </table>
      {rows.length > shown.length && (
        <div className="flex justify-center p-3">
          <Button variant="outline" size="sm" onClick={onShowAll}>
            Show all {rows.length}
          </Button>
        </div>
      )}
    </div>
  )
}

function SortHeader({
  label,
  title,
  col,
  sort,
  onSort,
  align = "right",
}: {
  label: string
  title?: string
  col: SortKey
  sort: Sort
  onSort: (key: SortKey) => void
  align?: "left" | "right"
}) {
  const active = sort.key === col
  const Icon = sort.desc ? ArrowDownIcon : ArrowUpIcon
  return (
    <th
      scope="col"
      aria-sort={active ? (sort.desc ? "descending" : "ascending") : "none"}
      className={cn("px-2", align === "right" ? "text-right" : "text-left")}
    >
      <button
        type="button"
        title={title}
        onClick={() => onSort(col)}
        className={cn(
          "inline-flex items-center gap-1 rounded-sm hover:text-foreground",
          align === "right" && "flex-row-reverse",
          active && "text-foreground"
        )}
      >
        {label}
        <Icon
          aria-hidden
          className={cn("size-3", active ? "opacity-100" : "opacity-0")}
        />
      </button>
    </th>
  )
}

export function FamilyBadge({ family }: { family: Family }) {
  return (
    <Badge
      variant="outline"
      className={cn(
        "h-4 px-1.5 text-2xs font-normal text-muted-foreground",
        family === "ai" && "border-primary/40 text-primary",
        family === "benchmark" && "border-foreground/30 text-foreground"
      )}
    >
      {FAMILY_LABEL[family] ?? family}
    </Badge>
  )
}

/** Ratios stay neutral unless negative: a wall of green would hide the losers. */
const lossTone = (n: number | null) =>
  n != null && n < 0 ? "text-down" : undefined

const Row = memo(function Row({
  row: r,
  slot,
  compareFull,
  onToggle,
  onOpen,
  onRun,
  isBot,
  curve,
}: {
  row: LeaderRow
  slot: number | undefined
  compareFull: boolean
  onToggle: (id: string) => void
  onOpen: (id: string) => void
  onRun: (row: LeaderRow) => void
  isBot: boolean
  curve: Point[] | undefined
}) {
  const selected = slot !== undefined
  const label = `${r.name}, ${r.symbol} ${r.interval}`
  const low = lowSample(r)
  return (
    <tr
      onClick={() => onOpen(r.instance_id)}
      className={cn(
        "group cursor-pointer [&>td]:border-b [&>td]:border-border/60 [&>td]:py-1.5 hover:[&>td]:bg-surface/70",
        selected && "[&>td]:bg-surface/40",
        low && "text-muted-foreground [&>td]:opacity-70 hover:[&>td]:opacity-100",
        // The yardstick every strategy should beat: buy and hold on the same market.
        r.family === "benchmark" && "[&>td]:bg-muted/50"
      )}
    >
      <td className="pl-3" onClick={(e) => e.stopPropagation()}>
        <Checkbox
          checked={selected}
          disabled={!selected && compareFull}
          onCheckedChange={() => onToggle(r.instance_id)}
          aria-label={`Compare ${label}`}
          title={
            !selected && compareFull ? "Compare holds 6 at most" : undefined
          }
        />
      </td>
      <td className="px-2">
        <span className="flex items-center gap-2">
          <span
            aria-hidden
            className="size-2 shrink-0 rounded-full"
            style={{
              background: selected ? SERIES_COLORS[slot] : "transparent",
            }}
          />
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation()
              onOpen(r.instance_id)
            }}
            className="truncate text-left text-title font-medium group-hover:underline group-hover:underline-offset-2"
          >
            {r.name}
          </button>
          <FamilyBadge family={r.family} />
          {low && (
            <Badge
              variant="outline"
              className="h-4 border-dashed px-1.5 text-2xs font-normal text-muted-foreground"
              title={`Fewer than ${MIN_TRADES} trades in this window: the ratios and return say little`}
            >
              low sample
            </Badge>
          )}
          {isBot && (
            <Badge
              className="h-4 px-1.5 text-2xs"
              title="The bot follows this instance"
            >
              Bot
            </Badge>
          )}
        </span>
      </td>
      <td className="px-2 font-mono whitespace-nowrap">
        {r.symbol.replace(/USDT$/, "")}
        <span className="text-muted-foreground">/USDT {r.interval}</span>
      </td>
      <td className={cn("px-2 text-right font-mono", tone(r.return_pct))}>
        {fmtPct(r.return_pct)}
      </td>
      <td className={cn("px-2 text-right font-mono", lossTone(r.sharpe))}>
        {fmtNum(r.sharpe)}
      </td>
      <td className={cn("px-2 text-right font-mono", lossTone(r.sortino))}>
        {fmtNum(r.sortino)}
      </td>
      <td className="px-2 text-right font-mono text-muted-foreground">
        {fmtDrawdown(r.max_drawdown_pct)}
      </td>
      <td className="px-2 text-right font-mono">{fmtRate(r.win_rate)}</td>
      <td className="px-2 text-right font-mono">{r.trades ?? "—"}</td>
      <td className="px-2 text-right font-mono">
        {r.exposure == null ? "—" : `${Math.round(r.exposure * 100)}%`}
      </td>
      <td className="px-2">
        <Sparkline points={curve} />
      </td>
      <td className="pr-3 text-right" onClick={(e) => e.stopPropagation()}>
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={() => onRun(r)}
          aria-label={`Run ${label} with the bot`}
          title="Run with the bot"
          className="opacity-60 group-hover:opacity-100 focus-visible:opacity-100"
        >
          <PlayIcon />
        </Button>
      </td>
    </tr>
  )
})

const W = 80
const H = 22

/** Equity in the window; the dashed line is the start (100). Numbers live in the row, so it's decorative. */
const Sparkline = memo(function Sparkline({
  points,
}: {
  points: Point[] | undefined
}) {
  if (!points)
    return <span className="block h-[22px] w-20 rounded-sm bg-muted/50" />
  if (points.length < 2) return <span className="block h-[22px] w-20" />
  let lo = 100
  let hi = 100
  for (const p of points) {
    if (p.v < lo) lo = p.v
    if (p.v > hi) hi = p.v
  }
  const span = hi - lo || 1
  const t0 = points[0].t
  const dt = points[points.length - 1].t - t0 || 1
  const y = (v: number) => (H - 2 - ((v - lo) / span) * (H - 4)).toFixed(1)
  const d = points
    .map(
      (p, i) =>
        `${i ? "L" : "M"}${(((p.t - t0) / dt) * W).toFixed(1)} ${y(p.v)}`
    )
    .join("")
  const up = points[points.length - 1].v >= 100
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      width={W}
      height={H}
      aria-hidden
      className={cn("block overflow-visible", up ? "text-up" : "text-down")}
    >
      <line
        x1="0"
        x2={W}
        y1={y(100)}
        y2={y(100)}
        stroke="currentColor"
        strokeOpacity=".25"
        strokeDasharray="2 2"
      />
      <path
        d={d}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
    </svg>
  )
})
