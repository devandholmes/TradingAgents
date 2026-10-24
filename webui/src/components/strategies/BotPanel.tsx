import { memo, useState } from "react"
import { BotIcon, TriangleAlertIcon } from "lucide-react"

import { ModeBadge } from "@/components/strategies/ActivateDialog"
import { fmtSigned, fmtUsdt, fmtWhen, tone } from "@/components/strategies/format"
import { Alert, AlertAction, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import {
  deactivateBot,
  syncBot,
  useBot,
  type BotStatus,
  type BotTrade,
} from "@/lib/bot"
import { fmtPrice } from "@/lib/market"
import { isOffline } from "@/lib/strategies"
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
            <StopControl hasPosition={(status?.position.qty ?? 0) > 0} pending={pending} />
          </CardAction>
        )}
      </CardHeader>
      <CardContent className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto">
        {!status ? (
          error ? (
            <Alert variant={isOffline(error) ? "default" : "destructive"}>
              <AlertDescription>Bot status unavailable: {error}</AlertDescription>
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
                {active.symbol} {active.interval}, {fmtUsdt(active.size_usdt)} USDT at full size,
                since {fmtWhen(active.activated_at)}
              </span>
            </button>
            {!status.in_sync && <OutOfSync status={status} pending={pending} />}
            <Stats status={status} />
          </>
        ) : (
          <div className="flex items-start gap-3 text-xs text-muted-foreground">
            <BotIcon aria-hidden className="mt-0.5 size-4 shrink-0" />
            <p>
              Not running. Pick a strategy in the table and choose Run with the bot; below 5 USDT
              it paper-trades, so it is safe to try with 0.10.
            </p>
          </div>
        )}
        {status && status.trades.length > 0 && <Trades trades={status.trades} />}
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

/** The bot's position is off its strategy's target, e.g. a rejected order or trading switched off: retry. */
function OutOfSync({ status, pending }: { status: BotStatus; pending: boolean }) {
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
        The bot is {pct(status.position.exposure)} in the market, the strategy {pct(target)}. {why}
        {error && <span role="alert" className="block text-destructive">{error}</span>}
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

function StopControl({ hasPosition, pending }: { hasPosition: boolean; pending: boolean }) {
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
      <Button size="sm" variant="outline" disabled={pending} onClick={() => void stop(false)}>
        Keep position
      </Button>
      <Button size="sm" variant="destructive" disabled={pending} onClick={() => void stop(true)}>
        {pending && <Spinner data-icon="inline-start" />}
        Sell position
      </Button>
      <Button size="sm" variant="ghost" disabled={pending} onClick={() => setAsking(false)}>
        Cancel
      </Button>
      {error && (
        <span role="alert" className="basis-full text-right text-2xs text-destructive">
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
          return (
            <li
              key={t.id}
              className="grid grid-cols-[auto_1fr_auto_auto] items-center gap-x-2 border-t border-border/60 py-1 text-xs"
              title={t.reason ?? undefined}
            >
              <span className={cn("w-8 font-medium", t.side === "BUY" ? "text-up" : "text-down")}>
                {t.side === "BUY" ? "Buy" : "Sell"}
              </span>
              <span className="truncate font-mono">
                {fmtUsdt(t.quote, t.quote < 10 ? 4 : 2)} USDT at {fmtPrice(t.price)}
              </span>
              <time dateTime={t.ts} className="font-mono text-2xs text-muted-foreground">
                {fmtWhen(t.ts)}
              </time>
              {failed ? (
                <Badge variant="destructive" className="h-4 px-1.5 text-2xs">
                  rejected
                </Badge>
              ) : (
                <Badge variant="outline" className="h-4 px-1.5 text-2xs text-muted-foreground">
                  {t.real ? "live" : "paper"}
                </Badge>
              )}
            </li>
          )
        })}
      </ol>
    </section>
  )
}
