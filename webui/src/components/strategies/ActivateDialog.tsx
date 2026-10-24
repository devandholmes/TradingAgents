import { useId, useState, type ReactNode } from "react"

import { fmtUsdt, tone } from "@/components/strategies/format"
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
import {
  SYNC_TOLERANCE,
  activateBot,
  useBot,
  useBotPreview,
  type BotPreview,
} from "@/lib/bot"
import { fmtPct } from "@/lib/market"
import {
  MIN_NOTIONAL_USDT,
  REFERENCE_CAPITAL,
  type LeaderRow,
} from "@/lib/strategies"
import { useTradingStatus } from "@/lib/tradingStatus"
import { cn } from "@/lib/utils"

/** The last size used this session, so re-activating doesn't start from blank. */
let lastSize = ""

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
  const valid = entered && size > 0 && !tooBig && !symbolBlocked && !!trading
  const testnet = trading?.mode !== "live"
  const replacing =
    bot?.active && bot.active.instance_id !== row.instance_id ? bot.active : null

  const equity = row.equity ?? REFERENCE_CAPITAL * (1 + (row.return_pct ?? 0) / 100)
  const scaled = entered && size > 0 ? (size * equity) / REFERENCE_CAPITAL : null

  async function start() {
    setError(undefined)
    try {
      await activateBot(row.instance_id, size)
      lastSize = raw
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
          On every closed {row.interval} bar the bot moves its {row.symbol} position to the
          strategy's target, between nothing and your full size. Spot only: it buys and sells,
          never shorts.
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
            className={cn("text-2xs", sizeError ? "text-destructive" : "text-muted-foreground")}
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
            <dd className="font-mono text-sm">{fmtUsdt(REFERENCE_CAPITAL, 0)} USDT</dd>
            <dd className="font-mono text-2xs text-muted-foreground">
              {fmtUsdt(equity)} today,{" "}
              <span className={tone(row.return_pct)}>{fmtPct(row.return_pct)}</span>{" "}
              {windowLabel}
            </dd>
          </div>
          <div className="flex flex-col gap-0.5 bg-surface p-3">
            <dt className="text-muted-foreground">At your size</dt>
            <dd className="font-mono text-sm">
              {entered && size > 0 ? fmtUsdt(size) : "—"} USDT
            </dd>
            <dd className="font-mono text-2xs text-muted-foreground">
              {scaled == null ? "—" : fmtUsdt(scaled, scaled < 10 ? 4 : 2)} today on the same
              run
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
            Binance's smallest Spot order is {MIN_NOTIONAL_USDT} USDT. Below that the bot
            paper-mirrors every trade: virtual fills at real prices, labelled paper. At{" "}
            {MIN_NOTIONAL_USDT} USDT or more it places market orders through the trading guard; a
            step smaller than {MIN_NOTIONAL_USDT} USDT is still paper-mirrored. It only ever sells
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

        {!trading ? (
          <Skeleton className="h-9 w-full" />
        ) : (
          <ul className="flex flex-col gap-1 text-xs">
            <Check ok={trading.enabled} label={`Trading is ${trading.enabled ? "on" : "off"}`}>
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
              The bot can start, but every live order is rejected while the kill switch is off. No
              paper fill replaces a rejected order.
            </AlertDescription>
          </Alert>
        )}
        {replacing && (
          <p className="text-xs text-muted-foreground">
            One strategy runs at a time: this replaces {replacing.name} on {replacing.symbol}{" "}
            {replacing.interval}.
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
      <Badge variant="outline" className="font-mono tracking-wide text-muted-foreground">
        OFF
      </Badge>
    )
  return (
    <Badge
      variant="outline"
      className={cn(
        "font-mono tracking-wide",
        mode === "live" ? "border-destructive/50 text-destructive" : "border-info/50 text-info"
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
        className={cn("mt-1.5 size-1.5 shrink-0 rounded-full", ok ? "bg-up" : "bg-warn")}
      />
      <span>
        {label}
        {children ? <span className="text-muted-foreground">. {children}.</span> : null}
      </span>
    </li>
  )
}
