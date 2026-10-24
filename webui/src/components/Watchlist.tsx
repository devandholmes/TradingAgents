import { memo, useState } from "react"
import { PlusIcon, XIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  Card,
  CardAction,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
} from "@/components/ui/input-group"
import { Skeleton } from "@/components/ui/skeleton"
import { fmtPct, fmtPrice, useLivePrices } from "@/lib/market"
import { cn } from "@/lib/utils"

const SYMBOL_RE = /^[A-Z0-9]{5,20}$/

export function Watchlist({
  symbols,
  selected,
  onSelect,
  onAdd,
  onRemove,
}: {
  symbols: string[]
  selected: string
  onSelect: (s: string) => void
  onAdd: (s: string) => void
  onRemove: (s: string) => void
}) {
  const [draft, setDraft] = useState("")
  const candidate = draft.trim().toUpperCase()
  const valid = SYMBOL_RE.test(candidate) && !symbols.includes(candidate)

  return (
    <Card size="sm" className="min-h-0 gap-2">
      <CardHeader>
        <CardTitle>
          <h2>Watchlist</h2>
        </CardTitle>
        <CardAction className="text-2xs text-muted-foreground">
          24h change
        </CardAction>
      </CardHeader>
      <CardContent className="flex min-h-0 flex-1 flex-col gap-2 px-0">
        <form
          className="px-(--card-spacing)"
          onSubmit={(e) => {
            e.preventDefault()
            if (!valid) return
            onAdd(candidate)
            setDraft("")
          }}
        >
          <InputGroup>
            <InputGroupInput
              aria-label="Add symbol"
              placeholder="Add symbol…"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              className="font-mono uppercase placeholder:normal-case"
            />
            <InputGroupAddon align="inline-end">
              <InputGroupButton
                type="submit"
                size="icon-xs"
                disabled={!valid}
                aria-label="Add symbol"
              >
                <PlusIcon />
              </InputGroupButton>
            </InputGroupAddon>
          </InputGroup>
        </form>
        <ul className="min-h-0 flex-1 overflow-y-auto">
          {symbols.map((s) => (
            <WatchRow
              key={s}
              symbol={s}
              selected={s === selected}
              onSelect={onSelect}
              onRemove={onRemove}
            />
          ))}
        </ul>
      </CardContent>
    </Card>
  )
}

/** One row; subscribes to its own symbol so a tick re-renders only this row. */
const WatchRow = memo(function WatchRow({
  symbol: s,
  selected,
  onSelect,
  onRemove,
}: {
  symbol: string
  selected: boolean
  onSelect: (s: string) => void
  onRemove: (s: string) => void
}) {
  const t = useLivePrices([s])[s]
  // Flash direction is derived from the previous price seen by this row.
  const [last, setLast] = useState(t?.price)
  const [flash, setFlash] = useState("")
  if (t && t.price !== last) {
    setLast(t.price)
    setFlash(last == null ? "" : t.price > last ? "flash-up" : "flash-down")
  }

  return (
    <li className="group/row relative">
      <button
        type="button"
        onClick={() => onSelect(s)}
        aria-current={selected}
        className={cn(
          "grid w-full grid-cols-[1fr_auto] items-center gap-x-3 border-l-2 border-transparent py-2 pr-9 pl-[calc(var(--card-spacing)-2px)] text-left transition-colors hover:bg-surface/70",
          selected && "border-primary bg-surface"
        )}
      >
        <span className="row-span-2 font-mono text-sm font-medium">
          {s.replace(/USDT$/, "")}
          <span className="text-2xs font-normal text-muted-foreground">
            {s.endsWith("USDT") ? "/USDT" : ""}
          </span>
        </span>
        {t ? (
          // Re-keyed on every price change so the flash animation restarts.
          <span
            key={t.price}
            className={cn(
              "rounded px-1 text-right font-mono text-sm",
              flash
            )}
          >
            {fmtPrice(t.price)}
          </span>
        ) : (
          <Skeleton className="h-4 w-16" />
        )}
        {t ? (
          <span
            className={cn(
              "px-1 text-right font-mono text-2xs",
              t.pct >= 0 ? "text-up" : "text-down"
            )}
          >
            {fmtPct(t.pct)}
          </span>
        ) : (
          <Skeleton className="mt-1 h-3 w-10 justify-self-end" />
        )}
      </button>
      <Button
        variant="ghost"
        size="icon-xs"
        aria-label={`Remove ${s}`}
        onClick={() => onRemove(s)}
        className="absolute top-1/2 right-2 -translate-y-1/2 opacity-0 group-hover/row:opacity-100 focus-visible:opacity-100 pointer-coarse:opacity-100"
      >
        <XIcon />
      </Button>
    </li>
  )
})
