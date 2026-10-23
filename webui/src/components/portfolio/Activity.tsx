import { useState } from "react"
import { XIcon } from "lucide-react"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { useAccountQuery, type AuditEntry, type Order } from "@/lib/account"
import { apiFetch } from "@/lib/api"
import { fmtPrice } from "@/lib/market"
import { cn } from "@/lib/utils"

const time = (ms: number) =>
  new Date(ms).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  })

/** Open orders (cancellable) and the trade audit log; refetched whenever `version` bumps. */
export function Activity({
  enabled,
  version,
}: {
  enabled: boolean
  version: number
}) {
  // Local bump so a cancel shows up even before the account stream reports it.
  const [bump, setBump] = useState(0)
  const orders = useAccountQuery<Order[]>(
    enabled ? "/api/orders/open" : null,
    version + bump
  )
  const trades = useAccountQuery<AuditEntry[]>(
    "/api/trades/recent?limit=20",
    version + bump
  )
  const refetch = () => setBump((b) => b + 1)

  return (
    <div className="grid gap-2 lg:grid-cols-2">
      <Card size="sm" className="gap-2">
        <CardHeader>
          <CardTitle>Open orders</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2 px-0">
          {orders.error && <InlineError message={orders.error} />}
          {!enabled ? (
            <Muted>Configure Binance API keys to see open orders.</Muted>
          ) : !orders.data ? (
            orders.loading && <Rows />
          ) : !orders.data.length ? (
            <Muted>No open orders.</Muted>
          ) : (
            <ul>
              {orders.data.map((o) => (
                <OrderRow key={o.order_id} order={o} onCancelled={refetch} />
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card size="sm" className="gap-2">
        <CardHeader>
          <CardTitle>Recent trades</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2 px-0">
          {trades.error && <InlineError message={trades.error} />}
          {!trades.data ? (
            trades.loading && <Rows />
          ) : !trades.data.length ? (
            <Muted>
              No trades yet — orders placed here or by the assistant appear in
              this log.
            </Muted>
          ) : (
            <ul className="max-h-80 overflow-y-auto">
              {trades.data.map((t, i) => (
                <TradeRow key={`${t.ts}:${i}`} entry={t} />
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function OrderRow({
  order: o,
  onCancelled,
}: {
  order: Order
  onCancelled: () => void
}) {
  const [state, setState] = useState<{ pending: boolean; error?: string }>({
    pending: false,
  })
  const cancel = () => {
    setState({ pending: true })
    apiFetch(`/api/orders/${encodeURIComponent(o.symbol)}/${o.order_id}`, {
      method: "DELETE",
    }).then(
      () => onCancelled(),
      (err: Error) => setState({ pending: false, error: err.message })
    )
  }
  return (
    <li className="flex items-center gap-3 border-b px-(--card-spacing) py-1.5 last:border-0">
      <Side side={o.side} />
      <div className="min-w-0 flex-1">
        <div className="font-mono font-medium">{o.symbol}</div>
        <div className="text-xs text-muted-foreground">
          {o.type} · {time(o.time)}
          {state.error && (
            <span className="text-destructive"> · {state.error}</span>
          )}
        </div>
      </div>
      <div className="text-right font-mono text-xs tabular-nums">
        <div>
          {o.executed_qty}/{o.orig_qty}
        </div>
        <div className="text-muted-foreground">@ {fmtPrice(o.price)}</div>
      </div>
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label={`Cancel order ${o.order_id}`}
        disabled={state.pending}
        onClick={cancel}
      >
        {state.pending ? <Spinner /> : <XIcon />}
      </Button>
    </li>
  )
}

function TradeRow({ entry: t }: { entry: AuditEntry }) {
  const r = t.request
  const qty = t.order?.executed_qty ?? r.quantity
  const amount =
    qty != null
      ? `${qty}`
      : r.quote_amount != null
        ? `${r.quote_amount} USDT`
        : ""
  return (
    <li className="flex items-center gap-3 border-b px-(--card-spacing) py-1.5 last:border-0">
      {r.side === "BUY" || r.side === "SELL" ? <Side side={r.side} /> : null}
      <div className="min-w-0 flex-1">
        <div className="font-mono font-medium">
          {r.symbol ?? "—"}{" "}
          <span className="font-sans text-xs font-normal text-muted-foreground">
            {amount}
          </span>
        </div>
        <div
          className="truncate text-xs text-muted-foreground"
          title={t.reason}
        >
          {time(Date.parse(t.ts))} · {t.source} · {t.mode}
          {t.reason && ` · ${t.reason}`}
        </div>
      </div>
      <Badge
        variant={
          t.status === "rejected" || t.status === "error"
            ? "destructive"
            : "secondary"
        }
      >
        {t.status}
      </Badge>
    </li>
  )
}

function Side({ side }: { side: "BUY" | "SELL" }) {
  return (
    <span
      className={cn(
        "w-9 font-mono text-xs font-semibold",
        side === "BUY" ? "text-up" : "text-down"
      )}
    >
      {side}
    </span>
  )
}

function Muted({ children }: { children: React.ReactNode }) {
  return (
    <p className="px-(--card-spacing) text-sm text-muted-foreground">
      {children}
    </p>
  )
}

function InlineError({ message }: { message: string }) {
  return (
    <Alert variant="destructive" className="mx-(--card-spacing) w-auto">
      <AlertDescription>{message}</AlertDescription>
    </Alert>
  )
}

function Rows() {
  return (
    <div className="flex flex-col gap-2 px-(--card-spacing)">
      <Skeleton className="h-8 w-full" />
      <Skeleton className="h-8 w-full" />
    </div>
  )
}
