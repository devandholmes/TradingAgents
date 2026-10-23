import { useId } from "react"

import { Badge } from "@/components/ui/badge"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import { setTradingEnabled, useTradingStatus } from "@/lib/api"
import { cn } from "@/lib/utils"

/** Trading mode badge + kill switch. Rendered in the header on every tab and on the Wallets tab. */
export function TradingControl({ className }: { className?: string }) {
  const { status, error, pending } = useTradingStatus()
  const id = useId()

  if (!status) {
    return error ? (
      <Badge variant="outline" className={className} title={error}>
        Trading unavailable
      </Badge>
    ) : (
      <Skeleton className={cn("h-5 w-32", className)} />
    )
  }

  const live = status.mode === "live"
  return (
    <div className={cn("flex items-center gap-2 text-xs", className)}>
      <Badge
        variant={live ? "destructive" : "outline"}
        className={cn(
          "font-mono tracking-wide",
          !live && "border-amber-500/50 text-amber-500"
        )}
        title={live ? "Orders use real funds" : "Binance Spot testnet"}
      >
        {live ? "LIVE" : "TESTNET"}
      </Badge>
      {status.configured ? null : (
        <Badge variant="secondary" title="Set the Binance API key in .env">
          not configured
        </Badge>
      )}
      <Switch
        id={id}
        size="sm"
        checked={status.enabled}
        disabled={pending || !status.configured}
        onCheckedChange={(on) => void setTradingEnabled(on)}
        aria-describedby={error ? `${id}-error` : undefined}
      />
      <label
        htmlFor={id}
        className={cn(
          "text-muted-foreground",
          status.enabled && status.configured && "text-foreground"
        )}
      >
        Trading {status.enabled ? "on" : "off"}
      </label>
      {error ? (
        <span
          id={`${id}-error`}
          role="alert"
          className="text-destructive"
          title={error}
        >
          error
        </span>
      ) : null}
    </div>
  )
}
