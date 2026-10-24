import { memo, useMemo, useState } from "react"
import { RefreshCwIcon, WalletIcon } from "lucide-react"

import { Activity } from "@/components/portfolio/Activity"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardAction,
  CardContent,
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
import { Spinner } from "@/components/ui/spinner"
import { Switch } from "@/components/ui/switch"
import { useBinanceAccount, type AccountStatus } from "@/lib/account"
import {
  fmtPct,
  fmtPrice,
  pricePrecision,
  useLivePrices,
  type LivePrice,
} from "@/lib/market"
import { CHAINS, useOnchainBalances } from "@/lib/onchain"
import { useWallets } from "@/lib/wallets"
import { cn } from "@/lib/utils"

/** Priced at $1; everything else needs a live `<BASE>USDT` ticker. */
const STABLES = new Set([
  "USDT",
  "USDC",
  "FDUSD",
  "DAI",
  "TUSD",
  "BUSD",
  "USDP",
])
const STABLE_PRICE: LivePrice = { price: 1, pct: 0 }
const ALLOCATION_COLORS = [
  "bg-chart-1",
  "bg-chart-2",
  "bg-chart-3",
  "bg-chart-4",
  "bg-chart-5",
]

type Holding = {
  id: string
  asset: string
  /** "Binance" or a chain label. */
  source: string
  /** Account mode or wallet label. */
  detail: string
  amount: number
  /** Binance base asset used for pricing, null when unpriced. */
  base: string | null
}
type Valued = Holding & {
  price?: number
  pct?: number
  value?: number
  prevValue?: number
}

/** Below this many USDT a row is dust; so is an unpriced Binance asset (on-chain tokens are curated). */
const DUST_USDT = 1
const isDust = (r: Valued) =>
  r.value == null ? r.source === "Binance" : r.value < DUST_USDT
const SHOW_SMALL_KEY = "portfolio.showSmall.v1"

function loadShowSmall(): boolean {
  try {
    return localStorage.getItem(SHOW_SMALL_KEY) === "1"
  } catch {
    return false // storage unavailable
  }
}

function saveShowSmall(show: boolean) {
  try {
    localStorage.setItem(SHOW_SMALL_KEY, show ? "1" : "0")
  } catch {
    /* storage unavailable */
  }
}

const usd = (n: number) =>
  n.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
const fmtAmount = (n: number) =>
  n.toLocaleString("en-US", { maximumFractionDigits: pricePrecision(n) })

export default function Portfolio() {
  const { wallets } = useWallets()
  const account = useBinanceAccount()
  const onchain = useOnchainBalances(wallets)

  const holdings = useMemo(() => {
    const labels = new Map(
      wallets.map((w) => [w.address.toLowerCase(), w.label])
    )
    const rows: Holding[] = account.balances.map((b) => ({
      id: `binance:${b.asset}`,
      asset: b.asset,
      source: "Binance",
      detail: account.mode === "live" ? "Spot" : "Spot · testnet",
      amount: b.free + b.locked,
      base: b.asset,
    }))
    for (const h of onchain.holdings)
      rows.push({
        id: `${h.chain}:${h.address}:${h.asset}`,
        asset: h.asset,
        source: CHAINS[h.chain].label,
        detail: labels.get(h.address.toLowerCase()) ?? h.address,
        amount: h.amount,
        base: h.base,
      })
    return rows
  }, [account.balances, account.mode, onchain.holdings, wallets])

  // Sorted, so the price store's per-symbol view keeps its identity across renders.
  const symbols = useMemo(
    () =>
      [
        ...new Set(
          holdings.flatMap((h) =>
            h.base && !STABLES.has(h.base) ? [`${h.base}USDT`] : []
          )
        ),
      ].sort(),
    [holdings]
  )
  const prices = useLivePrices(symbols)

  const { rows, total, change, subtotals, allocation } = useMemo(() => {
    const rows: Valued[] = holdings.map((h) => {
      const p = h.base
        ? STABLES.has(h.base)
          ? STABLE_PRICE
          : prices[`${h.base}USDT`]
        : undefined
      if (!p) return h
      const value = h.amount * p.price
      return {
        ...h,
        price: p.price,
        pct: p.pct,
        value,
        prevValue: value / (1 + p.pct / 100),
      }
    })
    rows.sort(
      (a, b) =>
        (b.value ?? -1) - (a.value ?? -1) || a.asset.localeCompare(b.asset)
    )

    let total = 0
    let prev = 0
    const subtotals = new Map<string, number>()
    const byAsset = new Map<string, number>()
    for (const r of rows) {
      if (r.value == null) continue
      total += r.value
      prev += r.prevValue ?? r.value
      subtotals.set(r.source, (subtotals.get(r.source) ?? 0) + r.value)
      const key = r.base && !STABLES.has(r.base) ? r.base : r.asset
      byAsset.set(key, (byAsset.get(key) ?? 0) + r.value)
    }
    const ranked = [...byAsset].sort((a, b) => b[1] - a[1])
    const allocation = ranked.slice(0, ALLOCATION_COLORS.length)
    const other = ranked
      .slice(ALLOCATION_COLORS.length)
      .reduce((s, [, v]) => s + v, 0)
    if (other > 0) allocation.push(["Other", other])
    return {
      rows,
      total,
      change: {
        abs: total - prev,
        pct: prev ? ((total - prev) / prev) * 100 : 0,
      },
      subtotals: [...subtotals].sort((a, b) => b[1] - a[1]),
      allocation,
    }
  }, [holdings, prices])

  const [showSmall, setShowSmall] = useState(loadShowSmall)
  const toggleShowSmall = (show: boolean) => {
    setShowSmall(show)
    saveShowSmall(show)
  }
  const { shown, dustCount } = useMemo(() => {
    const meaningful = rows.filter((r) => !isDust(r))
    return {
      shown: showSmall ? rows : meaningful,
      dustCount: rows.length - meaningful.length,
    }
  }, [rows, showSmall])

  const binanceLoading = account.status === "loading"
  const nothing = !rows.length && !binanceLoading && !onchain.loading

  return (
    <div className="flex flex-col gap-2 p-2">
      <Card size="sm">
        <CardHeader>
          <CardTitle>
            <h2>Portfolio</h2>
          </CardTitle>
          <CardAction className="flex items-center gap-2">
            <BinanceBadge status={account.status} mode={account.mode} />
            <Button
              variant="outline"
              size="sm"
              onClick={onchain.refresh}
              disabled={!wallets.length || onchain.loading}
            >
              {onchain.loading ? (
                <Spinner data-icon="inline-start" />
              ) : (
                <RefreshCwIcon data-icon="inline-start" />
              )}
              Refresh on-chain
            </Button>
          </CardAction>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex flex-wrap items-end gap-x-10 gap-y-3">
            <div>
              <div className="text-2xs text-muted-foreground">Total value</div>
              {binanceLoading && !rows.length ? (
                <Skeleton className="mt-1 h-9 w-56" />
              ) : (
                <div className="font-mono text-display font-semibold tracking-tight">
                  {usd(total)}{" "}
                  <span className="text-sm font-normal text-muted-foreground">
                    USDT
                  </span>
                </div>
              )}
            </div>
            <div>
              <div className="text-2xs text-muted-foreground">24h change</div>
              <div
                className={cn(
                  "font-mono text-sm",
                  change.abs > 0
                    ? "text-up"
                    : change.abs < 0
                      ? "text-down"
                      : "text-muted-foreground"
                )}
              >
                {change.abs >= 0 ? "+" : "−"}
                {usd(Math.abs(change.abs))} ({fmtPct(change.pct)})
              </div>
            </div>
            {subtotals.map(([source, value]) => (
              <div key={source}>
                <div className="text-2xs text-muted-foreground">{source}</div>
                <div className="font-mono text-sm">
                  {usd(value)}
                </div>
              </div>
            ))}
          </div>
          {total > 0 && <AllocationBar allocation={allocation} total={total} />}
        </CardContent>
      </Card>

      {account.status === "unconfigured" && (
        <Alert>
          <AlertTitle>Binance not configured</AlertTitle>
          <AlertDescription>
            Add{" "}
            {account.mode === "live"
              ? "BINANCE_API_KEY / BINANCE_API_SECRET"
              : "BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET"}{" "}
            to <code>.env</code> and restart the server to include Spot
            balances, orders and trades.
          </AlertDescription>
        </Alert>
      )}
      {account.status === "offline" && account.error && (
        <Alert variant="destructive">
          <AlertTitle>Binance account offline</AlertTitle>
          <AlertDescription>{account.error}</AlertDescription>
        </Alert>
      )}
      {onchain.error && (
        <Alert variant="destructive">
          <AlertTitle>Some chains could not be read</AlertTitle>
          <AlertDescription>{onchain.error}</AlertDescription>
        </Alert>
      )}

      <Card size="sm" className="gap-2">
        <CardHeader>
          <CardTitle>
            <h2>Holdings</h2>
          </CardTitle>
          {(dustCount > 0 || (!wallets.length && rows.length > 0)) && (
            <CardAction className="flex flex-wrap items-center justify-end gap-x-3 gap-y-1">
              {!wallets.length && rows.length > 0 && (
                <Button variant="link" size="sm" asChild>
                  <a href="#/wallets">
                    Add a wallet to include on-chain balances
                  </a>
                </Button>
              )}
              {dustCount > 0 && (
                <label className="flex items-center gap-2 text-xs whitespace-nowrap text-muted-foreground">
                  <Switch
                    size="sm"
                    checked={showSmall}
                    onCheckedChange={toggleShowSmall}
                  />
                  Show small balances ({dustCount})
                </label>
              )}
            </CardAction>
          )}
        </CardHeader>
        <CardContent className="px-0">
          {nothing ? (
            <Empty>
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <WalletIcon />
                </EmptyMedia>
                <EmptyTitle>No holdings yet</EmptyTitle>
                <EmptyDescription>
                  Connect a browser wallet or add a watch-only address in the
                  Wallets tab, or configure Binance API keys.
                </EmptyDescription>
              </EmptyHeader>
              <EmptyContent>
                <Button size="sm" asChild>
                  <a href="#/wallets">Go to Wallets</a>
                </Button>
              </EmptyContent>
            </Empty>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-2xs text-muted-foreground">
                  <tr className="border-b">
                    <th className="py-1.5 pl-(--card-spacing) text-left font-normal">
                      Asset
                    </th>
                    <th className="px-3 py-1.5 text-left font-normal">
                      Source
                    </th>
                    <th className="px-3 py-1.5 text-right font-normal">
                      Amount
                    </th>
                    <th className="px-3 py-1.5 text-right font-normal">
                      Price
                    </th>
                    <th className="px-3 py-1.5 text-right font-normal">
                      Value
                    </th>
                    <th className="py-1.5 pr-(--card-spacing) text-right font-normal">
                      24h
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((r) => (
                    <HoldingRow
                      key={r.id}
                      asset={r.asset}
                      source={r.source}
                      detail={r.detail}
                      amount={r.amount}
                      price={r.price}
                      pct={r.pct}
                      value={r.value}
                    />
                  ))}
                  {(binanceLoading ||
                    (onchain.loading && !onchain.holdings.length)) && (
                    <LoadingRows />
                  )}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <Activity
        enabled={account.status !== "unconfigured"}
        version={account.orderEvents}
      />
    </div>
  )
}

function BinanceBadge({
  status,
  mode,
}: {
  status: AccountStatus
  mode: "testnet" | "live" | null
}) {
  const label = mode === "live" ? "LIVE" : "TESTNET"
  if (status === "unconfigured")
    return <Badge variant="outline">Binance · not configured</Badge>
  if (status === "loading")
    return <Badge variant="outline">Binance · loading…</Badge>
  if (status === "offline")
    return <Badge variant="destructive">Binance {label} · offline</Badge>
  return (
    <Badge variant={mode === "live" ? "destructive" : "secondary"}>
      Binance {label} · live
    </Badge>
  )
}

function AllocationBar({
  allocation,
  total,
}: {
  allocation: [string, number][]
  total: number
}) {
  const color = (i: number) => ALLOCATION_COLORS[i] ?? "bg-muted-foreground/40"
  return (
    <div className="flex flex-col gap-1.5">
      <div
        className="flex h-2 overflow-hidden rounded-full bg-muted"
        role="img"
        aria-label="Allocation by asset"
      >
        {allocation.map(([asset, value], i) => (
          <div
            key={asset}
            className={color(i)}
            style={{ width: `${(value / total) * 100}%` }}
          />
        ))}
      </div>
      <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {allocation.map(([asset, value], i) => (
          <li key={asset} className="flex items-center gap-1.5">
            <span className={cn("size-2 rounded-full", color(i))} />
            <span className="font-mono text-foreground">{asset}</span>
            <span className="font-mono">
              {((value / total) * 100).toFixed(1)}%
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

// Primitive props: a row re-renders only when its own price or balance changes.
const HoldingRow = memo(function HoldingRow({
  asset,
  source,
  detail,
  amount,
  price,
  pct,
  value,
}: {
  asset: string
  source: string
  detail: string
  amount: number
  price?: number
  pct?: number
  value?: number
}) {
  return (
    <tr className="border-b transition-colors last:border-0 hover:bg-surface/70">
      <td className="py-1.5 pl-(--card-spacing) font-mono font-medium">
        {asset}
      </td>
      <td className="max-w-48 px-3 py-1.5">
        <div>{source}</div>
        <div className="truncate text-xs text-muted-foreground" title={detail}>
          {detail}
        </div>
      </td>
      <td className="px-3 py-1.5 text-right font-mono">
        {fmtAmount(amount)}
      </td>
      <td className="px-3 py-1.5 text-right font-mono">
        {price == null ? "—" : fmtPrice(price)}
      </td>
      <td className="px-3 py-1.5 text-right font-mono">
        {value == null ? "—" : usd(value)}
      </td>
      <td
        className={cn(
          "py-1.5 pr-(--card-spacing) text-right font-mono",
          pct == null || pct === 0
            ? "text-muted-foreground"
            : pct > 0
              ? "text-up"
              : "text-down"
        )}
      >
        {pct == null ? "—" : fmtPct(pct)}
      </td>
    </tr>
  )
})

function LoadingRows() {
  return Array.from({ length: 3 }, (_, i) => (
    <tr key={i} className="border-b last:border-0">
      <td colSpan={6} className="px-(--card-spacing) py-2">
        <Skeleton className="h-4 w-full" />
      </td>
    </tr>
  ))
}
