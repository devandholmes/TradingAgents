import { memo, useState } from "react"
import { BotIcon, ShieldAlertIcon, TriangleAlertIcon } from "lucide-react"

import { ModeBadge, RiskFields } from "@/components/strategies/ActivateDialog"
import {
  fmtSigned,
  fmtUsdt,
  fmtWhen,
  tone,
} from "@/components/strategies/format"
import {
  Alert,
  AlertAction,
  AlertDescription,
  AlertTitle,
} from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardAction,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import {
  RISK_EXIT_LABEL,
  deactivateBot,
  setBotRisk,
  syncBot,
  useBot,
  type BotRisk,
  type BotStatus,
  type BotTrade,
} from "@/lib/bot"
import { fmtPct, fmtPrice } from "@/lib/market"
import { draftOf, isOffline, parseRisk } from "@/lib/strategies"
import { cn } from "@/lib/utils"

const RECENT = 8

/** The strategy bot: what it follows, its position and PnL, its last trades, and the stop control. */
export const BotPanel = memo(function BotPanel({
  onOpen,
}: {
  onOpen: (instanceId: string) => void
}) {
  const { status, error, pending } = useBot()
  const active = status?.active ?? null

  return (
    <Card size="sm" className="min-h-0 gap-3">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <h2>Bot</h2>
          {status && <ModeBadge mode={active?.mode ?? null} />}
        </CardTitle>
        {active && (
          <CardAction>
            <StopControl
              hasPosition={(status?.position.qty ?? 0) > 0}
              pending={pending}
            />
          </CardAction>
        )}
      </CardHeader>
      <CardContent className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto">
        {!status ? (
          error ? (
            <Alert variant={isOffline(error) ? "default" : "destructive"}>
              <AlertDescription>
                Bot status unavailable: {error}
              </AlertDescription>
            </Alert>
          ) : (
            <Skeleton className="h-24 w-full" />
          )
        ) : active ? (
          <>
            <button
              type="button"
              onClick={() => onOpen(active.instance_id)}
              className="flex flex-col items-start rounded-md text-left hover:underline hover:underline-offset-2"
            >
              <span className="text-title font-medium">{active.name}</span>
              <span className="font-mono text-xs text-muted-foreground">
                {active.symbol} {active.interval}, {fmtUsdt(active.size_usdt)}{" "}
                USDT at full size,
                {active.execution === "maker_first"
                  ? " maker-first orders,"
                  : ""}{" "}
                since {fmtWhen(active.activated_at)}
              </span>
            </button>
            {!status.in_sync && <OutOfSync status={status} pending={pending} />}
            <Stats status={status} />
            {status.risk && (
              <RiskLevels
                key={active.instance_id}
                risk={status.risk}
                status={status}
                pending={pending}
              />
            )}
          </>
        ) : (
          <div className="flex items-start gap-3 text-xs text-muted-foreground">
            <BotIcon aria-hidden className="mt-0.5 size-4 shrink-0" />
            <p>
              Not running. Pick a strategy in the table and choose Run with the
              bot; below 5 USDT it paper-trades, so it is safe to try with 0.10.
            </p>
          </div>
        )}
        {status && status.trades.length > 0 && (
          <Trades trades={status.trades} />
        )}
      </CardContent>
    </Card>
  )
})

function Stats({ status }: { status: BotStatus }) {
  const { position: p, pnl } = status
  const items: [string, string, string?][] = [
    ["In market", `${Math.round(p.exposure * 100)}%`],
    ["Value", `${fmtUsdt(p.value, p.value < 10 ? 4 : 2)} USDT`],
    ["Avg price", p.avg_price ? fmtPrice(p.avg_price) : "—"],
    ["Unrealized", fmtSigned(pnl.unrealized, 4), tone(pnl.unrealized)],
    ["Realized", fmtSigned(pnl.realized, 4), tone(pnl.realized)],
    ["Total PnL", fmtSigned(pnl.total, 4), tone(pnl.total)],
  ]
  return (
    <dl className="grid grid-cols-3 gap-x-3 gap-y-2">
      {items.map(([label, value, cls]) => (
        <div key={label} className="min-w-0">
          <dt className="text-2xs text-muted-foreground">{label}</dt>
          <dd className={cn("truncate font-mono text-xs", cls)}>{value}</dd>
        </div>
      ))}
      {pnl.paper_total !== 0 && pnl.paper_total !== pnl.total && (
        <p className="col-span-3 text-2xs text-muted-foreground">
          {fmtSigned(pnl.paper_total, 4)} USDT of the total is from paper fills.
        </p>
      )}
    </dl>
  )
}

const LEVELS: [string, keyof BotRisk, keyof BotRisk][] = [
  ["Stop-loss", "stop_loss_pct", "stop_price"],
  ["Take-profit", "take_profit_pct", "take_profit_price"],
  ["Trailing stop", "trailing_pct", "trailing_stop_price"],
]

/** Where the risk exits sit, the latch after one fired, and an editor for them. */
function RiskLevels({
  risk,
  status,
  pending,
}: {
  risk: BotRisk
  status: BotStatus
  pending: boolean
}) {
  const [editing, setEditing] = useState(false)
  const p = status.position
  const price = p.qty > 0 ? p.value / p.qty : null
  const set = LEVELS.filter(([, pct]) => risk[pct] != null)
  const name = status.active?.name ?? "the strategy"

  return (
    <section aria-labelledby="bot-risk" className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <h3 id="bot-risk" className="text-2xs text-muted-foreground">
          Risk exits
        </h3>
        {!editing && (
          <Button size="xs" variant="ghost" onClick={() => setEditing(true)}>
            {set.length ? "Edit" : "Add"}
          </Button>
        )}
      </div>

      {risk.latched && (
        <Alert className="border-warn/50">
          <ShieldAlertIcon className="text-warn" />
          <AlertTitle>
            {(risk.reason && RISK_EXIT_LABEL[risk.reason]) || "A risk exit"}{" "}
            sold the position
          </AlertTitle>
          <AlertDescription className="text-xs/relaxed">
            The bot is staying out on purpose, even though {name} still wants to
            be in the market. Once the strategy exits by itself, its next buy
            signal brings the bot back in as normal.
          </AlertDescription>
        </Alert>
      )}

      {editing ? (
        <RiskEditor
          instanceId={status.active?.instance_id ?? ""}
          risk={risk}
          pending={pending}
          onDone={() => setEditing(false)}
        />
      ) : set.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          None set: the bot sells only when the strategy does.
        </p>
      ) : (
        <>
          <dl className="grid grid-cols-3 gap-x-3 gap-y-2">
            {set.map(([label, pct, at]) => {
              const level = risk[at] as number | null
              return (
                <div key={label} className="min-w-0">
                  <dt className="text-2xs text-muted-foreground">
                    {label} {risk[pct] as number}%
                  </dt>
                  <dd className="truncate font-mono text-xs">
                    {level == null ? "—" : fmtPrice(level)}
                  </dd>
                  {level != null && price != null && (
                    <dd className="truncate font-mono text-2xs text-muted-foreground">
                      {fmtPct((level / price - 1) * 100)} away
                    </dd>
                  )}
                </div>
              )
            })}
          </dl>
          {price == null && (
            <p className="text-2xs text-muted-foreground">
              No position now: the prices are set when the bot next buys.
            </p>
          )}
          {risk.trailing_pct != null &&
            risk.peak_price != null &&
            price != null && (
              <p className="text-2xs text-muted-foreground">
                The trailing stop follows the highest price since buying:{" "}
                {fmtPrice(risk.peak_price)} so far.
              </p>
            )}
        </>
      )}
    </section>
  )
}

function RiskEditor({
  instanceId,
  risk,
  pending,
  onDone,
}: {
  instanceId: string
  risk: BotRisk
  pending: boolean
  onDone: () => void
}) {
  const [draft, setDraft] = useState(() => draftOf(risk))
  const [error, setError] = useState<string>()
  const parsed = parseRisk(draft)
  const save = () => {
    setError(undefined)
    setBotRisk(parsed.params).then(onDone, (err: Error) =>
      setError(err.message)
    )
  }
  return (
    <div className="flex flex-col gap-2">
      <RiskFields instanceId={instanceId} value={draft} onChange={setDraft} />
      {error && (
        <p role="alert" className="text-2xs text-destructive">
          {error}
        </p>
      )}
      <div className="flex justify-end gap-1.5">
        <Button size="sm" variant="ghost" disabled={pending} onClick={onDone}>
          Cancel
        </Button>
        <Button size="sm" disabled={pending || !parsed.valid} onClick={save}>
          {pending && <Spinner data-icon="inline-start" />}
          Save exits
        </Button>
      </div>
    </div>
  )
}

const fmtFee = (n: number) =>
  n.toLocaleString("en-US", { maximumSignificantDigits: 3 })

/** "Stop-loss exit, fee 0.0012 USDT": the line under a trade, or null when there is nothing to add. */
function tradeNote(t: BotTrade): string | null {
  const exit = t.reason ? RISK_EXIT_LABEL[t.reason] : undefined
  const fee =
    t.fee != null ? `fee ${fmtFee(t.fee)} ${t.fee_asset ?? "USDT"}` : undefined
  const parts = [exit && `${exit} exit`, fee].filter(Boolean)
  return parts.length ? parts.join(", ") : null
}

/** The bot's position is off its strategy's target, e.g. a rejected order or trading switched off: retry. */
function OutOfSync({
  status,
  pending,
}: {
  status: BotStatus
  pending: boolean
}) {
  const [error, setError] = useState<string>()
  const target = status.active?.target ?? 0
  const pct = (n: number) => `${Math.round(n * 100)}%`
  const last = status.trades[0]
  const why =
    last && (last.status === "rejected" || last.status === "error")
      ? `The last order was rejected: ${last.reason ?? "no reason given"}.`
      : "It missed a change of the target."
  const sync = () => {
    setError(undefined)
    syncBot().catch((err: Error) => setError(err.message))
  }
  return (
    <Alert className="border-warn/50">
      <TriangleAlertIcon className="text-warn" />
      <AlertTitle>Out of sync</AlertTitle>
      <AlertDescription>
        The bot is {pct(status.position.exposure)} in the market, the strategy{" "}
        {pct(target)}. {why}
        {error && (
          <span role="alert" className="block text-destructive">
            {error}
          </span>
        )}
      </AlertDescription>
      <AlertAction>
        <Button size="sm" variant="outline" disabled={pending} onClick={sync}>
          {pending && <Spinner data-icon="inline-start" />}
          Sync
        </Button>
      </AlertAction>
    </Alert>
  )
}

function StopControl({
  hasPosition,
  pending,
}: {
  hasPosition: boolean
  pending: boolean
}) {
  const [asking, setAsking] = useState(false)
  const [error, setError] = useState<string>()

  const stop = (close: boolean) =>
    deactivateBot(close).then(
      () => setAsking(false),
      (err: Error) => setError(err.message)
    )

  if (!asking)
    return (
      <Button
        variant="outline"
        size="sm"
        onClick={() => (hasPosition ? setAsking(true) : void stop(false))}
        disabled={pending}
      >
        {pending && <Spinner data-icon="inline-start" />}
        Stop bot
      </Button>
    )
  return (
    <div
      role="group"
      aria-label="Stop the bot"
      className="flex flex-wrap items-center justify-end gap-1.5"
    >
      <Button
        size="sm"
        variant="outline"
        disabled={pending}
        onClick={() => void stop(false)}
      >
        Keep position
      </Button>
      <Button
        size="sm"
        variant="destructive"
        disabled={pending}
        onClick={() => void stop(true)}
      >
        {pending && <Spinner data-icon="inline-start" />}
        Sell position
      </Button>
      <Button
        size="sm"
        variant="ghost"
        disabled={pending}
        onClick={() => setAsking(false)}
      >
        Cancel
      </Button>
      {error && (
        <span
          role="alert"
          className="basis-full text-right text-2xs text-destructive"
        >
          {error}
        </span>
      )}
    </div>
  )
}

function Trades({ trades }: { trades: BotTrade[] }) {
  const recent = trades.toSorted((a, b) => b.id - a.id).slice(0, RECENT)
  return (
    <section aria-labelledby="bot-trades" className="flex flex-col gap-1">
      <h3 id="bot-trades" className="text-2xs text-muted-foreground">
        Recent bot trades
      </h3>
      <ol className="flex flex-col">
        {recent.map((t) => {
          const failed = t.status === "rejected" || t.status === "error"
          const note = failed ? null : tradeNote(t)
          return (
            <li
              key={t.id}
              className="grid grid-cols-[auto_1fr_auto_auto] items-center gap-x-2 border-t border-border/60 py-1 text-xs"
              title={t.reason ?? undefined}
            >
              <span
                className={cn(
                  "w-8 font-medium",
                  t.side === "BUY" ? "text-up" : "text-down"
                )}
              >
                {t.side === "BUY" ? "Buy" : "Sell"}
              </span>
              <span className="truncate font-mono">
                {fmtUsdt(t.quote, t.quote < 10 ? 4 : 2)} USDT at{" "}
                {fmtPrice(t.price)}
              </span>
              <time
                dateTime={t.ts}
                className="font-mono text-2xs text-muted-foreground"
              >
                {fmtWhen(t.ts)}
              </time>
              {failed ? (
                <Badge variant="destructive" className="h-4 px-1.5 text-2xs">
                  rejected
                </Badge>
              ) : (
                <Badge
                  variant="outline"
                  className="h-4 px-1.5 text-2xs text-muted-foreground"
                >
                  {t.real ? "live" : "paper"}
                </Badge>
              )}
              {note && (
                <span className="col-span-3 col-start-2 truncate text-2xs text-muted-foreground">
                  {note}
                </span>
              )}
            </li>
          )
        })}
      </ol>
    </section>
  )
}
