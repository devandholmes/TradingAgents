import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  fmtCompact,
  fmtPct,
  fmtPrice,
  useApi,
  type TrendRow,
  type Trends as TrendsData,
} from "@/lib/market"
import { cn } from "@/lib/utils"

const TABS = [
  ["gainers", "Gainers"],
  ["losers", "Losers"],
  ["volume", "Volume"],
] as const

export function Trends({ onSelect }: { onSelect: (s: string) => void }) {
  const { data, error, loading } = useApi<TrendsData>(
    "/api/trends?quote=USDT&limit=10",
    30_000
  )

  return (
    <Card size="sm" className="min-h-0 gap-2">
      <CardHeader>
        <CardTitle>Market trends · 24h</CardTitle>
      </CardHeader>
      <CardContent className="flex min-h-0 flex-1 flex-col px-0">
        <Tabs defaultValue="gainers" className="min-h-0 flex-1">
          <TabsList className="mx-(--card-spacing) w-[calc(100%-2*var(--card-spacing))]">
            {TABS.map(([value, label]) => (
              <TabsTrigger key={value} value={value}>
                {label}
              </TabsTrigger>
            ))}
          </TabsList>
          {TABS.map(([value]) => (
            <TabsContent
              key={value}
              value={value}
              className="min-h-0 overflow-y-auto"
            >
              {data ? (
                <TrendList
                  rows={data[value]}
                  showVolume={value === "volume"}
                  onSelect={onSelect}
                />
              ) : loading ? (
                <div className="flex flex-col gap-2 px-(--card-spacing) pt-1">
                  {Array.from({ length: 6 }, (_, i) => (
                    <Skeleton key={i} className="h-5" />
                  ))}
                </div>
              ) : null}
              {error ? (
                <Alert
                  variant="destructive"
                  className="mx-(--card-spacing) mt-2 w-auto"
                >
                  <AlertTitle>
                    {data ? "Refresh failed" : "Trends unavailable"}
                  </AlertTitle>
                  <AlertDescription>{error}</AlertDescription>
                </Alert>
              ) : null}
            </TabsContent>
          ))}
        </Tabs>
      </CardContent>
    </Card>
  )
}

function TrendList({
  rows,
  showVolume,
  onSelect,
}: {
  rows: TrendRow[]
  showVolume: boolean
  onSelect: (s: string) => void
}) {
  return (
    <ul className="font-mono text-xs">
      {rows.map((r) => (
        <li key={r.symbol}>
          <button
            type="button"
            onClick={() => onSelect(r.symbol)}
            className="grid w-full grid-cols-[1fr_auto_4.5rem] gap-3 px-(--card-spacing) py-1 text-left tabular-nums hover:bg-muted/60"
          >
            <span className="truncate font-medium">{r.symbol}</span>
            <span className="text-right text-muted-foreground">
              {showVolume
                ? `$${fmtCompact(r.quoteVolume)}`
                : fmtPrice(r.lastPrice)}
            </span>
            <span
              className={cn(
                "text-right",
                r.priceChangePercent >= 0 ? "text-up" : "text-down"
              )}
            >
              {fmtPct(r.priceChangePercent)}
            </span>
          </button>
        </li>
      ))}
    </ul>
  )
}
