import { useId, useState, type ReactNode } from "react"

import { fmtNum, fmtUsdt, tone } from "@/components/strategies/format"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
  InputGroupText,
} from "@/components/ui/input-group"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import {
  SYNC_TOLERANCE,
  activateBot,
  useBot,
  useBotPreview,
  type BotPreview,
  type Execution,
} from "@/lib/bot"
import { fmtPct } from "@/lib/market"
import {
  MIN_NOTIONAL_USDT,
  NO_RISK,
  REFERENCE_CAPITAL,
  draftOf,
  parseRisk,
  presetParams,
  sameRisk,
  stopsPeriod,
  useStops,
  type LeaderRow,
  type RiskDraft,
  type RiskKey,
  type RiskParams,
  type Stops,
} from "@/lib/strategies"
import { useTradingStatus, type TradingStatus } from "@/lib/tradingStatus"
import { cn } from "@/lib/utils"

/** The last size, exits and execution used this session, so re-activating doesn't start from blank. */
let lastSize = ""
let lastRisk: RiskDraft = draftOf(NO_RISK)
let lastExecution: Execution = "market"

/** Point the bot at one instance. Shows what the size means, paper vs live, the guard's limits; needs a confirm. */
export function ActivateDialog({
  row,
  windowLabel,
  onClose,
}: {
  row: LeaderRow | null
  windowLabel: string
  onClose: () => void
}) {
  return (
    <Dialog open={!!row} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[calc(100svh-2rem)] overflow-y-auto sm:max-w-lg">
        {row && (
          <Activate
            key={row.instance_id}
            row={row}
            windowLabel={windowLabel}
            onDone={onClose}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}

function Activate({
  row,
  windowLabel,
  onDone,
}: {
  row: LeaderRow
  windowLabel: string
  onDone: () => void
}) {
  const id = useId()
  const { status: trading } = useTradingStatus()
  const { status: bot, pending } = useBot()
  const preview = useBotPreview(row.instance_id)
  const [raw, setRaw] = useState(lastSize)
  const [riskDraft, setRiskDraft] = useState(lastRisk)
  const [execution, setExecution] = useState(lastExecution)
  const [confirmed, setConfirmed] = useState(false)
  const [error, setError] = useState<string>()

  const size = Number(raw)
  const entered = raw.trim() !== "" && Number.isFinite(size)
  const live = entered && size >= MIN_NOTIONAL_USDT
  const limits = trading?.limits
  const tooBig = !!limits && entered && size > limits.max_order_usdt
  const symbolBlocked = !!limits && !limits.allowed_symbols.includes(row.symbol)
  const sizeError = !entered
    ? undefined
    : size <= 0
      ? "Enter an amount above 0."
      : tooBig
        ? `The per-order limit is ${fmtUsdt(limits.max_order_usdt)} USDT.`
        : undefined
  const risk = parseRisk(riskDraft)
  const valid =
    entered && size > 0 && !tooBig && !symbolBlocked && !!trading && risk.valid
  const testnet = trading?.mode !== "live"
  const replacing =
    bot?.active && bot.active.instance_id !== row.instance_id
      ? bot.active
      : null

  const equity =
    row.equity ?? REFERENCE_CAPITAL * (1 + (row.return_pct ?? 0) / 100)
  const scaled =
    entered && size > 0 ? (size * equity) / REFERENCE_CAPITAL : null

  async function start() {
    setError(undefined)
    try {
      await activateBot(row.instance_id, size, risk.params, execution)
      lastSize = raw
      lastRisk = riskDraft
      lastExecution = execution
      onDone()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle>Run {row.name} with the bot</DialogTitle>
        <DialogDescription>
          On every closed {row.interval} bar the bot moves its {row.symbol}{" "}
          position to the strategy's target, between nothing and your full size.
          Spot only: it buys and sells, never shorts.
        </DialogDescription>
      </DialogHeader>

      <div className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <label htmlFor={`${id}-size`} className="text-xs font-medium">
            Your size at full exposure
          </label>
          <InputGroup>
            <InputGroupInput
              id={`${id}-size`}
              inputMode="decimal"
              autoComplete="off"
              placeholder="0.10"
              value={raw}
              onChange={(e) => {
                setRaw(e.target.value.replace(",", "."))
                setConfirmed(false)
              }}
              aria-invalid={!!sizeError}
              aria-describedby={`${id}-size-hint`}
              className="font-mono"
            />
            <InputGroupAddon align="inline-end">
              <InputGroupText>USDT</InputGroupText>
            </InputGroupAddon>
          </InputGroup>
          <p
            id={`${id}-size-hint`}
            className={cn(
              "text-2xs",
              sizeError ? "text-destructive" : "text-muted-foreground"
            )}
          >
            {sizeError ??
              (limits
                ? `Up to ${fmtUsdt(limits.max_order_usdt)} USDT per order; ${fmtUsdt(trading.used_today_usdt)} of ${fmtUsdt(limits.daily_cap_usdt)} USDT of today's cap used.`
                : "Loading the trading limits…")}
          </p>
        </div>

        <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-lg bg-border text-xs">
          <div className="flex flex-col gap-0.5 bg-surface p-3">
            <dt className="text-muted-foreground">Strategy reference</dt>
            <dd className="font-mono text-sm">
              {fmtUsdt(REFERENCE_CAPITAL, 0)} USDT
            </dd>
            <dd className="font-mono text-2xs text-muted-foreground">
              {fmtUsdt(equity)} today,{" "}
              <span className={tone(row.return_pct)}>
                {fmtPct(row.return_pct)}
              </span>{" "}
              {windowLabel}
            </dd>
          </div>
          <div className="flex flex-col gap-0.5 bg-surface p-3">
            <dt className="text-muted-foreground">At your size</dt>
            <dd className="font-mono text-sm">
              {entered && size > 0 ? fmtUsdt(size) : "—"} USDT
            </dd>
            <dd className="font-mono text-2xs text-muted-foreground">
              {scaled == null ? "—" : fmtUsdt(scaled, scaled < 10 ? 4 : 2)}{" "}
              today on the same run
            </dd>
          </div>
        </dl>

        <div className="flex flex-col gap-1.5 rounded-lg border p-3">
          <div className="flex items-center gap-2">
            {entered && <ModeBadge mode={live ? "live" : "paper"} />}
            <span className="text-xs font-medium">
              {!entered
                ? "Paper or live depends on the size"
                : live
                  ? `Real orders on Binance${testnet ? " Spot testnet" : " Spot, with real funds"}`
                  : "Paper: no orders are sent"}
            </span>
          </div>
          <p className="text-xs/relaxed text-muted-foreground">
            Binance's smallest Spot order is {MIN_NOTIONAL_USDT} USDT. Below
            that the bot paper-mirrors every trade: virtual fills at real
            prices, labelled paper. At {MIN_NOTIONAL_USDT} USDT or more it
            places market orders through the trading guard; a step smaller than{" "}
            {MIN_NOTIONAL_USDT} USDT is still paper-mirrored. It only ever sells
            what it bought itself.
          </p>
        </div>

        <div className="flex flex-col gap-1 rounded-lg border p-3">
          <span className="text-xs font-medium">When it starts</span>
          {preview ? (
            <p className="text-xs/relaxed text-muted-foreground">
              {firstTrade(preview, entered && size > 0 ? size : null, live)}
            </p>
          ) : (
            <Skeleton className="h-4 w-3/4" />
          )}
        </div>

        <RiskFields
          instanceId={row.instance_id}
          value={riskDraft}
          onChange={(v) => {
            setRiskDraft(v)
            setConfirmed(false)
          }}
        />

        <ExecutionField
          value={execution}
          onChange={setExecution}
          fees={trading?.fees}
          loaded={!!trading}
          live={live}
        />

        {!trading ? (
          <Skeleton className="h-9 w-full" />
        ) : (
          <ul className="flex flex-col gap-1 text-xs">
            <Check
              ok={trading.enabled}
              label={`Trading is ${trading.enabled ? "on" : "off"}`}
            >
              {trading.enabled
                ? undefined
                : live
                  ? "Live orders are rejected until you turn trading on in the header"
                  : "Paper fills don't need it"}
            </Check>
            <Check
              ok={!symbolBlocked}
              label={`${row.symbol} is ${symbolBlocked ? "not " : ""}an allowed symbol`}
            >
              {symbolBlocked
                ? `Allowed: ${trading.limits.allowed_symbols.join(", ")}`
                : undefined}
            </Check>
          </ul>
        )}

        {live && trading?.enabled === false && (
          <Alert>
            <AlertTitle>Trading is off</AlertTitle>
            <AlertDescription>
              The bot can start, but every live order is rejected while the kill
              switch is off. No paper fill replaces a rejected order.
            </AlertDescription>
          </Alert>
        )}
        {replacing && (
          <p className="text-xs text-muted-foreground">
            One strategy runs at a time: this replaces {replacing.name} on{" "}
            {replacing.symbol} {replacing.interval}.
          </p>
        )}
        {error && (
          <Alert variant="destructive">
            <AlertTitle>The bot didn't start</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        <label className="flex items-start gap-2 text-xs/relaxed">
          <Checkbox
            checked={confirmed}
            onCheckedChange={(v) => setConfirmed(v === true)}
            disabled={!valid}
            className="mt-0.5"
          />
          <span>
            {live
              ? `I understand the bot places real orders on my Binance ${testnet ? "testnet" : "Spot"} account on its own, up to ${fmtUsdt(size)} USDT.`
              : "I understand the bot records paper trades for this strategy on its own."}
          </span>
        </label>
      </div>

      <DialogFooter>
        <Button
          variant={live && !testnet ? "destructive" : "default"}
          disabled={!valid || !confirmed || pending}
          onClick={() => void start()}
        >
          {pending && <Spinner data-icon="inline-start" />}
          {live ? "Start live bot" : "Start paper bot"}
        </Button>
      </DialogFooter>
    </>
  )
}

/** What activation trades at once: the bot enters the strategy's current position, by the bot's own rules. */
function firstTrade(p: BotPreview, size: number | null, live: boolean): string {
  if (p.target == null)
    return "No trade now: the strategy hasn't settled a position yet. The bot follows from its next signal."
  const pct = Math.round(p.target * 100)
  if (size == null)
    return p.target > 0
      ? `The strategy is ${pct}% in the market now: the bot buys that share of your size at once.`
      : "The strategy is flat now: no trade until its next entry signal."
  const delta = p.target * size - p.held_usdt
  if (Math.abs(delta) <= SYNC_TOLERANCE * size)
    return p.target > 0
      ? `No trade now: the bot already holds the strategy's current ${pct}% position.`
      : "No trade now: the strategy is flat. The bot buys on its next entry signal."
  const amount = Math.abs(delta)
  const kind = live && amount >= MIN_NOTIONAL_USDT ? "live" : "paper"
  return `${delta > 0 ? "Buys" : "Sells"} ~${fmtUsdt(amount, amount < 10 ? 4 : 2)} USDT of ${p.symbol.replace(/USDT$/, "")} (${kind}) to match the strategy's current ${pct}% position.`
}

export function ModeBadge({ mode }: { mode: "paper" | "live" | null }) {
  if (!mode)
    return (
      <Badge
        variant="outline"
        className="font-mono tracking-wide text-muted-foreground"
      >
        OFF
      </Badge>
    )
  return (
    <Badge
      variant="outline"
      className={cn(
        "font-mono tracking-wide",
        mode === "live"
          ? "border-destructive/50 text-destructive"
          : "border-info/50 text-info"
      )}
    >
      {mode === "live" ? "LIVE" : "PAPER"}
    </Badge>
  )
}

function Check({
  ok,
  label,
  children,
}: {
  ok: boolean
  label: string
  children?: ReactNode
}) {
  return (
    <li className="flex gap-2">
      <span
        aria-hidden
        className={cn(
          "mt-1.5 size-1.5 shrink-0 rounded-full",
          ok ? "bg-up" : "bg-warn"
        )}
      />
      <span>
        {label}
        {children ? (
          <span className="text-muted-foreground">. {children}.</span>
        ) : null}
      </span>
    </li>
  )
}

const RISK_FIELDS: { key: RiskKey; label: string }[] = [
  { key: "stop_loss_pct", label: "Stop-loss" },
  { key: "take_profit_pct", label: "Take-profit" },
  { key: "trailing_pct", label: "Trailing stop" },
]

/** One line per exit that is on, in plain words. */
function riskSentences(p: RiskParams): string[] {
  const out: string[] = []
  if (p.stop_loss_pct != null)
    out.push(
      `Sells everything if the price falls ${p.stop_loss_pct}% below the bot's average buy price.`
    )
  if (p.take_profit_pct != null)
    out.push(
      `Sells everything if the price rises ${p.take_profit_pct}% above it, locking in the gain.`
    )
  if (p.trailing_pct != null)
    out.push(
      `Sells everything if the price drops ${p.trailing_pct}% from its highest point since buying, so this exit climbs with a winning trade.`
    )
  return out
}

/** Stop-loss / take-profit / trailing inputs plus one-click backtested presets. Shared with the bot panel's editor. */
export function RiskFields({
  instanceId,
  value,
  onChange,
}: {
  instanceId: string
  value: RiskDraft
  onChange: (v: RiskDraft) => void
}) {
  const id = useId()
  const stops = useStops(instanceId)
  const { params, errors } = parseRisk(value)
  const sentences = riskSentences(params)

  return (
    <section
      aria-labelledby={`${id}-title`}
      className="flex flex-col gap-3 rounded-lg border p-3"
    >
      <div className="flex flex-col gap-1">
        <h3 id={`${id}-title`} className="text-xs font-medium">
          Risk exits{" "}
          <span className="font-normal text-muted-foreground">(optional)</span>
        </h3>
        <p className="text-xs/relaxed text-muted-foreground">
          Automatic sells that watch the live price every second, so they act in
          the middle of a bar instead of waiting for it to close. When one
          fires, the bot sells its whole position and stays out, even if the
          strategy still wants in. It buys again only after the strategy exits
          by itself and then gives a fresh buy signal.
        </p>
      </div>

      <Presets
        stops={stops}
        current={params}
        onPick={(p) => onChange(draftOf(p))}
      />

      <div className="grid grid-cols-3 gap-2">
        {RISK_FIELDS.map((f) => (
          <div key={f.key} className="flex min-w-0 flex-col gap-1">
            <label htmlFor={`${id}-${f.key}`} className="text-2xs font-medium">
              {f.label}
            </label>
            <InputGroup>
              <InputGroupInput
                id={`${id}-${f.key}`}
                inputMode="decimal"
                autoComplete="off"
                placeholder="Off"
                value={value[f.key]}
                onChange={(e) =>
                  onChange({
                    ...value,
                    [f.key]: e.target.value.replace(",", "."),
                  })
                }
                aria-invalid={!!errors[f.key]}
                aria-describedby={`${id}-summary`}
                className="font-mono"
              />
              <InputGroupAddon align="inline-end">
                <InputGroupText>%</InputGroupText>
              </InputGroupAddon>
            </InputGroup>
          </div>
        ))}
      </div>

      <div
        id={`${id}-summary`}
        className="flex flex-col gap-0.5 text-2xs/relaxed"
      >
        {RISK_FIELDS.filter((f) => errors[f.key]).map((f) => (
          <p key={f.key} className="text-destructive">
            {f.label}: {errors[f.key]}
          </p>
        ))}
        {sentences.length ? (
          sentences.map((t) => (
            <p key={t} className="text-muted-foreground">
              {t}
            </p>
          ))
        ) : (
          <p className="text-muted-foreground">
            No exits set: the bot sells only when the strategy does.
          </p>
        )}
      </div>
    </section>
  )
}

/** The backtested presets: one click fills the fields; the best one is marked. */
function Presets({
  stops,
  current,
  onPick,
}: {
  stops: { data?: Stops; error?: string }
  current: RiskParams
  onPick: (p: RiskParams) => void
}) {
  const id = useId()
  if (stops.error)
    return (
      <p className="text-2xs text-muted-foreground">
        The backtested presets didn't load ({stops.error}). You can still type
        your own.
      </p>
    )
  if (!stops.data) return <Skeleton className="h-16 w-full" />
  const period = stopsPeriod(stops.data.rows)
  if (!period)
    return (
      <p className="text-2xs text-muted-foreground">
        These exits haven't been backtested on this strategy yet. You can still
        type your own.
      </p>
    )
  const rows = stops.data.rows.filter((r) => r.period === period)
  const best = stops.data.best
  const picked =
    rows.find((r) => sameRisk(presetParams(r.preset), current))?.preset ?? ""

  return (
    <div className="@container flex flex-col gap-1.5">
      <p id={`${id}-hint`} className="text-2xs/relaxed text-muted-foreground">
        Presets, backtested on this strategy over the{" "}
        {period === "1y" ? "last year" : "last 90 days"}: the return and Sharpe
        it would have had with each rule. One click fills the fields.
      </p>
      <ToggleGroup
        type="single"
        variant="outline"
        size="sm"
        value={picked}
        onValueChange={(v) => v && onPick(presetParams(v))}
        aria-label="Risk exit presets"
        aria-describedby={`${id}-hint`}
        className="grid w-full grid-cols-2 gap-1.5 @sm:grid-cols-3"
      >
        {rows.map((r) => (
          <ToggleGroupItem
            key={r.preset}
            value={r.preset}
            className={cn(
              "h-auto min-w-0 flex-col items-stretch gap-0 px-2 py-1.5 text-left whitespace-normal data-[state=on]:border-primary/60 data-[state=on]:bg-primary/5",
              r.preset === best && "border-up/50"
            )}
          >
            <span className="flex items-start justify-between gap-1 text-xs font-medium">
              <span className="min-w-0">
                {r.preset === "none" ? "No exits" : r.label}
              </span>
              {r.preset === best && (
                <Badge
                  variant="outline"
                  className="h-4 shrink-0 border-up/50 px-1 text-2xs text-up"
                >
                  Best
                </Badge>
              )}
            </span>
            <span className="font-mono text-2xs font-normal">
              <span className={tone(r.return_pct)}>{fmtPct(r.return_pct)}</span>{" "}
              <span className="text-muted-foreground">
                Sharpe {fmtNum(r.sharpe)}
              </span>
            </span>
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
    </div>
  )
}

/** A commission rate as a percent; Binance reports fractions (0.001 = 0.1 %). */
const fmtFeeRate = (n: number | null | undefined) =>
  n == null || !Number.isFinite(Number(n))
    ? "—"
    : `${(Number(n) * 100).toFixed(3)}%`

function ExecutionField({
  value,
  onChange,
  fees,
  loaded,
  live,
}: {
  value: Execution
  onChange: (v: Execution) => void
  fees: TradingStatus["fees"]
  loaded: boolean
  live: boolean
}) {
  const id = useId()
  const taker = fees?.taker != null ? `, ${fmtFeeRate(fees.taker)} for you` : ""
  const maker = fees?.maker != null ? `, ${fmtFeeRate(fees.maker)} for you` : ""
  const bnb =
    fees?.bnb_discount == null
      ? "BNB fee discount: unknown here, the testnet doesn't report it."
      : fees.bnb_discount
        ? "BNB fee discount is on: fees are paid in BNB at a lower rate."
        : "BNB fee discount is off: turning on “Use BNB to pay for fees” in Binance lowers them."
  return (
    <section
      aria-labelledby={`${id}-title`}
      className="flex flex-col gap-2 rounded-lg border p-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id={`${id}-title`} className="text-xs font-medium">
          How orders are placed
        </h3>
        <ToggleGroup
          type="single"
          size="sm"
          variant="outline"
          spacing={0}
          value={value}
          onValueChange={(v) => v && onChange(v as Execution)}
          aria-labelledby={`${id}-title`}
        >
          <ToggleGroupItem
            value="market"
            className="text-xs data-[state=on]:border-primary/60 data-[state=on]:text-primary"
          >
            Market
          </ToggleGroupItem>
          <ToggleGroupItem
            value="maker_first"
            className="text-xs data-[state=on]:border-primary/60 data-[state=on]:text-primary"
          >
            Maker-first
          </ToggleGroupItem>
        </ToggleGroup>
      </div>
      <p className="text-xs/relaxed text-muted-foreground">
        {value === "market"
          ? `Buys and sells straight away at the best price on offer. Instant, but it pays the higher "taker" fee${taker}.`
          : `First posts a limit order at the best current price and waits about 15 seconds for someone to take it. Whatever fills pays the lower "maker" fee${maker}; anything left is finished with a market order. Cheaper, at the cost of a short wait.`}{" "}
        Risk exits always sell at market, because getting out fast matters more
        than the fee.
        {!live &&
          " Paper fills place no orders, so this only matters once the bot trades live."}
      </p>
      <p className="text-2xs/relaxed text-muted-foreground">
        {!loaded
          ? "Loading your fee rates…"
          : !fees
            ? "This server doesn't report your fee rates yet."
            : `Your Binance rates: maker ${fmtFeeRate(fees.maker)}, taker ${fmtFeeRate(fees.taker)}. ${bnb}`}
      </p>
    </section>
  )
}
