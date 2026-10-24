import { memo, useCallback, useEffect, useMemo, useState } from "react"
import { DatabaseIcon, RefreshCwIcon, XIcon } from "lucide-react"

import { ActivateDialog } from "@/components/strategies/ActivateDialog"
import { BotPanel } from "@/components/strategies/BotPanel"
import {
  CompareChart,
  type CompareLine,
  type HoverValues,
} from "@/components/strategies/CompareChart"
import { DetailSheet } from "@/components/strategies/DetailSheet"
import {
  MAX_COMPARE,
  SERIES_COLORS,
  fmtIndexed,
  fmtWhen,
} from "@/components/strategies/format"
import {
  Leaderboard,
  ROW_CAP,
  sortRows,
  type Sort,
  type SortKey,
} from "@/components/strategies/Leaderboard"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { Skeleton } from "@/components/ui/skeleton"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { useBot } from "@/lib/bot"
import {
  STRATEGY_INTERVALS,
  WINDOWS,
  isOffline,
  reloadBoard,
  useCatalog,
  useEquity,
  useLeaderboard,
  useStrategiesSocket,
  type LeaderRow,
  type Point,
  type StrategyWindow,
} from "@/lib/strategies"
import { cn } from "@/lib/utils"

const WINDOW_TEXT: Record<StrategyWindow, string> = {
  "7d": "over 7 days",
  "30d": "over 30 days",
  "90d": "over 90 days",
  "1y": "over 1 year",
  live: "since going live",
}
const SORT_TEXT: Record<SortKey, string> = {
  name: "name",
  return_pct: "return",
  sharpe: "Sharpe",
  sortino: "Sortino",
  max_drawdown_pct: "smallest drawdown",
  win_rate: "win rate",
  trades: "trade count",
  exposure: "exposure",
}
const SPARK_POINTS = 48
const COMPARE_POINTS = 300
const IMPLICIT_COMPARE = 3

/** Compare slots: index = colour, so removing one line never repaints the others. */
type Slots = (string | null)[]
const EMPTY_SLOTS: Slots = Array(MAX_COMPARE).fill(null)

type View = {
  win: StrategyWindow
  symbol: string
  interval: string
  sort: Sort
  slots: Slots
}
// The tab unmounts when hidden; keep the user's view for when they come back.
let savedView: View = {
  win: "30d",
  symbol: "all",
  interval: "all",
  sort: { key: "sharpe", desc: true },
  slots: EMPTY_SLOTS,
}

export default function StrategiesWorkspace() {
  const [win, setWin] = useState(savedView.win)
  const [symbol, setSymbol] = useState(savedView.symbol)
  const [interval, setIv] = useState(savedView.interval)
  const [sort, setSort] = useState(savedView.sort)
  const [slots, setSlots] = useState(savedView.slots)
  const [showAll, setShowAll] = useState(false)
  const [detailId, setDetailId] = useState<string | null>(null)
  const [activating, setActivating] = useState<LeaderRow | null>(null)

  useEffect(() => {
    savedView = { win, symbol, interval, sort, slots }
  }, [win, symbol, interval, sort, slots])

  const board = useLeaderboard(win)
  const bot = useBot()
  const botId = bot.status?.active?.instance_id ?? null

  const symbols = useMemo(
    () => [...new Set((board.rows ?? []).map((r) => r.symbol))].sort(),
    [board.rows]
  )
  const rows = useMemo(
    () =>
      sortRows(
        (board.rows ?? []).filter(
          (r) =>
            (symbol === "all" || r.symbol === symbol) &&
            (interval === "all" || r.interval === interval)
        ),
        sort
      ),
    [board.rows, symbol, interval, sort]
  )
  const byId = useMemo(
    () => new Map((board.rows ?? []).map((r) => [r.instance_id, r])),
    [board.rows]
  )

  // Nothing ticked: compare the top of the current ranking, so the chart is never empty.
  const explicit = slots.some(Boolean)
  const shownSlots = useMemo(
    () =>
      explicit
        ? slots
        : [
            ...rows.slice(0, IMPLICIT_COMPARE).map((r) => r.instance_id),
            ...EMPTY_SLOTS,
          ].slice(0, MAX_COMPARE),
    [explicit, slots, rows]
  )
  const slotMap = useMemo(() => {
    const m = new Map<string, number>()
    shownSlots.forEach((id, i) => {
      if (id && byId.has(id)) m.set(id, i)
    })
    return m
  }, [shownSlots, byId])
  const lines: CompareLine[] = useMemo(
    () => [...slotMap].map(([id, i]) => ({ id, color: SERIES_COLORS[i] })),
    [slotMap]
  )
  const compareIds = useMemo(() => lines.map((l) => l.id), [lines])
  const compareCurves = useEquity(compareIds, win, COMPARE_POINTS)

  const visibleIds = useMemo(
    () => (showAll ? rows : rows.slice(0, ROW_CAP)).map((r) => r.instance_id),
    [rows, showAll]
  )
  const sparkCurves = useEquity(visibleIds, win, SPARK_POINTS)

  // Ticking a row while the implicit top 3 shows starts from those 3, so the chart doesn't jump.
  const onToggle = useCallback(
    (id: string) => {
      const at = shownSlots.indexOf(id)
      const free = shownSlots.indexOf(null)
      if (at < 0 && free < 0) return
      setSlots(
        shownSlots.map((x, i) =>
          at >= 0 ? (i === at ? null : x) : i === free ? id : x
        )
      )
    },
    [shownSlots]
  )
  const onSort = useCallback(
    (key: SortKey) =>
      setSort((s) => ({ key, desc: s.key === key ? !s.desc : key !== "name" })),
    []
  )
  const onRun = useCallback((r: LeaderRow) => setActivating(r), [])
  const onOpen = useCallback((id: string) => setDetailId(id), [])
  const onClear = useCallback(() => setSlots(EMPTY_SLOTS), [])

  const detailRow = detailId ? (byId.get(detailId) ?? null) : null

  if (board.error && isOffline(board.error) && !board.rows)
    return <OfflineState onRetry={() => reloadBoard(win)} />

  return (
    <div className="grid flex-1 gap-2 p-2 lg:min-h-0 lg:grid-rows-[minmax(300px,2fr)_minmax(0,3fr)]">
      <div className="grid gap-2 lg:min-h-0 lg:grid-cols-[minmax(0,1fr)_minmax(320px,380px)]">
        <ComparePanel
          lines={lines}
          curves={compareCurves}
          byId={byId}
          win={win}
          implicit={!explicit}
          sortKey={sort.key}
          onRemove={onToggle}
          onClear={onClear}
          loading={!board.rows}
        />
        <BotPanel onOpen={onOpen} />
      </div>

      <Card size="sm" className="min-h-[480px] gap-0 pb-0 lg:min-h-0">
        <CardHeader className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b">
          <CardTitle className="flex items-center gap-3">
            <h2>Leaderboard</h2>
            <LiveDot />
          </CardTitle>
          <div className="flex flex-wrap items-center gap-2 sm:ml-auto">
            <Filter
              label="Window"
              value={win}
              options={WINDOWS.map((w) => [w, w === "live" ? "Live" : w])}
              onChange={(v) => {
                setWin(v as StrategyWindow)
                setShowAll(false)
              }}
            />
            <Filter
              label="Symbol"
              value={symbol}
              options={[
                ["all", "All"],
                ...symbols.map((s): [string, string] => [s, s.replace(/USDT$/, "")]),
              ]}
              onChange={(v) => {
                setSymbol(v)
                setShowAll(false)
              }}
            />
            <Filter
              label="Interval"
              value={interval}
              options={[
                ["all", "All"],
                ...STRATEGY_INTERVALS.map((i): [string, string] => [i, i]),
              ]}
              onChange={(v) => {
                setIv(v)
                setShowAll(false)
              }}
            />
          </div>
        </CardHeader>
        <CardContent className="flex min-h-0 flex-1 flex-col px-0">
          {board.error && (
            <Alert variant="destructive" className="m-3 w-auto">
              <AlertTitle>
                Leaderboard {board.rows ? "may be out of date" : "unavailable"}
              </AlertTitle>
              <AlertDescription className="flex flex-wrap items-center gap-2">
                {board.error}
                <Button size="xs" variant="outline" onClick={() => reloadBoard(win)}>
                  Retry
                </Button>
              </AlertDescription>
            </Alert>
          )}
          {!board.rows ? (
            board.error ? null : (
              <div className="flex flex-col gap-2 p-3" aria-busy>
                {Array.from({ length: 8 }, (_, i) => (
                  <Skeleton key={i} className="h-6 w-full" />
                ))}
              </div>
            )
          ) : board.rows.length === 0 ? (
            <NoResults />
          ) : (
            <Leaderboard
              rows={rows}
              sort={sort}
              onSort={onSort}
              slots={slotMap}
              compareFull={slotMap.size >= MAX_COMPARE}
              onToggle={onToggle}
              onOpen={onOpen}
              onRun={onRun}
              botId={botId}
              curves={sparkCurves}
              showAll={showAll}
              onShowAll={() => setShowAll(true)}
            />
          )}
        </CardContent>
      </Card>

      <DetailSheet
        row={detailRow}
        benchmark={
          detailRow
            ? (byId.get(`buy_and_hold:${detailRow.symbol}:${detailRow.interval}`) ?? null)
            : null
        }
        windowLabel={WINDOW_TEXT[win]}
        onClose={() => setDetailId(null)}
        onRun={onRun}
      />
      <ActivateDialog
        row={activating}
        windowLabel={WINDOW_TEXT[win]}
        onClose={() => setActivating(null)}
      />
    </div>
  )
}

function Filter({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: string
  options: [string, string][]
  onChange: (v: string) => void
}) {
  return (
    <ToggleGroup
      type="single"
      size="sm"
      variant="outline"
      spacing={0}
      value={value}
      onValueChange={(v) => v && onChange(v)}
      aria-label={label}
    >
      {options.map(([v, text]) => (
        <ToggleGroupItem
          key={v}
          value={v}
          className="px-2 font-mono text-xs data-[state=on]:border-primary/60 data-[state=on]:text-primary"
        >
          {text}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  )
}

function LiveDot() {
  const status = useStrategiesSocket()
  return (
    <span
      role="status"
      className="flex items-center gap-1.5 text-2xs font-normal text-muted-foreground"
      title="Rows update as each bar closes"
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

const ComparePanel = memo(function ComparePanel({
  lines,
  curves,
  byId,
  win,
  implicit,
  sortKey,
  onRemove,
  onClear,
  loading,
}: {
  lines: CompareLine[]
  curves: Record<string, Point[] | undefined>
  byId: Map<string, LeaderRow>
  win: StrategyWindow
  implicit: boolean
  sortKey: SortKey
  onRemove: (id: string) => void
  onClear: () => void
  loading: boolean
}) {
  const [hover, setHover] = useState<HoverValues>(null)
  const label = `Equity ${WINDOW_TEXT[win]} for ${lines
    .map((l) => byId.get(l.id)?.name ?? l.id)
    .join(", ")}`

  return (
    <Card size="sm" className="min-h-[340px] gap-2 lg:min-h-0">
      <CardHeader>
        <CardTitle>
          <h2>Compare</h2>
        </CardTitle>
        <CardDescription>
          Equity {WINDOW_TEXT[win]}, every line starting at 100.{" "}
          {implicit
            ? `Showing the top ${lines.length} by ${SORT_TEXT[sortKey]}; tick rows to pick up to ${MAX_COMPARE}.`
            : `${lines.length} of ${MAX_COMPARE} picked.`}
        </CardDescription>
        {!implicit && (
          <CardAction>
            <Button variant="ghost" size="sm" onClick={onClear}>
              Clear
            </Button>
          </CardAction>
        )}
      </CardHeader>
      <CardContent className="flex min-h-0 flex-1 flex-col gap-2">
        <ul
          className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs"
          aria-label="Compared strategies"
        >
          {lines.map(({ id, color }) => {
            const r = byId.get(id)
            const pts = curves[id]
            const v = hover?.values.get(id) ?? pts?.[pts.length - 1]?.v
            return (
              <li key={id} className="flex items-center gap-1.5">
                <span
                  aria-hidden
                  className="h-0.5 w-3 rounded-full"
                  style={{ background: color }}
                />
                <span className="font-medium">{r?.name ?? id}</span>
                <span className="font-mono text-muted-foreground">
                  {r ? `${r.symbol.replace(/USDT$/, "")} ${r.interval}` : ""}
                </span>
                <span className="w-14 font-mono">
                  {v == null ? "—" : fmtIndexed(v)}
                </span>
                {!implicit && (
                  <button
                    type="button"
                    onClick={() => onRemove(id)}
                    aria-label={`Remove ${r?.name ?? id} from compare`}
                    className="-ml-1 rounded-sm p-0.5 text-muted-foreground hover:text-foreground"
                  >
                    <XIcon aria-hidden className="size-3" />
                  </button>
                )}
              </li>
            )
          })}
          {hover && (
            <li className="ml-auto font-mono text-2xs text-muted-foreground">
              {fmtWhen(new Date(hover.time * 1000).toISOString())}
            </li>
          )}
        </ul>
        {loading ? (
          <Skeleton className="min-h-40 flex-1" />
        ) : lines.length === 0 ? (
          <p className="m-auto text-xs text-muted-foreground">
            Tick strategies in the leaderboard to compare them here.
          </p>
        ) : (
          <CompareChart
            lines={lines}
            curves={curves}
            onHover={setHover}
            label={label}
            className="min-h-40 flex-1"
          />
        )}
      </CardContent>
    </Card>
  )
})

function NoResults() {
  const catalog = useCatalog()
  return (
    <Empty>
      <EmptyHeader>
        <EmptyTitle>No results yet</EmptyTitle>
        <EmptyDescription>
          {catalog?.length
            ? `${catalog.length} strategies are set up. The engine is still backfilling candles and running their backtests; rows appear here as each one finishes.`
            : "The engine hasn't written any strategy results yet. Rows appear here as backtests finish."}
        </EmptyDescription>
      </EmptyHeader>
    </Empty>
  )
}

function OfflineState({ onRetry }: { onRetry: () => void }) {
  return (
    <Empty className="m-2 w-auto border">
      <EmptyHeader>
        <EmptyMedia variant="icon">
          <DatabaseIcon />
        </EmptyMedia>
        <EmptyTitle>Strategy database offline</EmptyTitle>
        <EmptyDescription>
          The leaderboard, backtests and the bot live in Postgres, and the server can't reach it.
          Start it from the repository folder, then retry.
        </EmptyDescription>
      </EmptyHeader>
      <EmptyContent>
        <code className="rounded-md bg-surface px-3 py-1.5 font-mono text-xs">
          docker compose up -d postgres
        </code>
        <Button size="sm" variant="outline" onClick={onRetry}>
          <RefreshCwIcon data-icon="inline-start" />
          Retry
        </Button>
      </EmptyContent>
    </Empty>
  )
}
