import { Suspense, lazy } from "react"
import {
  CheckIcon,
  CircleSlashIcon,
  TriangleAlertIcon,
  WrenchIcon,
  XIcon,
} from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Spinner } from "@/components/ui/spinner"
import {
  Trace,
  TraceDescription,
  TraceIcon,
  TraceLine,
  TraceTitle,
} from "@/components/ui/trace"
import { ratingTone, type AnalysisStatus } from "@/lib/analysis"
import type { Part, ToolPart, TradePart } from "@/lib/chat"
import { fmtPrice } from "@/lib/market"
import { cn } from "@/lib/utils"

// --- markdown (lazy, no raw HTML) ---

/** Compact markdown for chat bubbles (no typography plugin). */
export const MD =
  "text-sm leading-relaxed text-pretty [&_a]:text-primary [&_a]:underline [&_a]:underline-offset-3 [&_code]:rounded [&_code]:bg-background/60 [&_code]:px-1 [&_code]:font-mono [&_code]:text-xs [&_h1]:font-semibold [&_h2]:font-semibold [&_h3]:font-semibold [&_li]:my-0.5 [&_ol]:list-decimal [&_ol]:pl-5 [&_p]:my-1.5 [&_pre]:overflow-x-auto [&_pre]:rounded [&_pre]:bg-background/60 [&_pre]:p-2 [&_table]:text-xs [&_td]:border [&_td]:px-1.5 [&_td]:py-0.5 [&_th]:border [&_th]:px-1.5 [&_th]:py-0.5 [&_th]:text-left [&_ul]:list-disc [&_ul]:pl-5 [&>*:first-child]:mt-0 [&>*:last-child]:mb-0"

/** Long-form agent reports: a readable measure and a clear heading scale. */
export const MD_REPORT = cn(
  MD,
  "max-w-[72ch] leading-[1.65] [&_h1]:mt-5 [&_h1]:mb-2 [&_h1]:text-base [&_h1]:text-balance [&_h2]:mt-5 [&_h2]:mb-1.5 [&_h2]:text-[0.95rem] [&_h2]:text-balance [&_h3]:mt-4 [&_h3]:mb-1 [&_h3]:text-sm [&_hr]:my-4 [&_li]:my-1 [&_p]:my-2 [&_strong]:text-foreground [&_table]:border-collapse [&_table]:tabular-nums [&_td]:px-2 [&_td]:py-1 [&_td]:align-top [&_th]:bg-muted/50 [&_th]:px-2 [&_th]:py-1 [&_th]:font-medium"
)

const loadMarkdown = () => import("./markdown")
const Markdown = lazy(loadMarkdown)
/** Warm the markdown chunk ahead of the first render that needs it. */
export const preloadMarkdown = () => void loadMarkdown()

export function Md({ children }: { children: string }) {
  return (
    <Suspense fallback={<p className="whitespace-pre-wrap">{children}</p>}>
      <Markdown>{children}</Markdown>
    </Suspense>
  )
}

// --- rating and run status ---

const TONE = {
  up: "border-up/40 bg-up/10 text-up",
  down: "border-down/40 bg-down/10 text-down",
  hold: "border-primary/40 bg-primary/10 text-primary",
  none: "",
} as const

export function ratingClass(rating: string | null | undefined) {
  return TONE[ratingTone(rating)]
}

export function RatingBadge({
  rating,
  className,
}: {
  rating: string
  className?: string
}) {
  return (
    <Badge variant="outline" className={cn(ratingClass(rating), className)}>
      {rating}
    </Badge>
  )
}

export function RunStatus({
  status,
  rating,
}: {
  status: AnalysisStatus
  rating?: string | null
}) {
  if (rating) return <RatingBadge rating={rating} />
  if (status === "running")
    return (
      <Badge variant="outline" className="text-muted-foreground">
        <Spinner data-icon="inline-start" />
        Running
      </Badge>
    )
  if (status === "cancelled")
    return (
      <Badge variant="outline" className="text-muted-foreground">
        <CircleSlashIcon data-icon="inline-start" />
        Cancelled
      </Badge>
    )
  if (status === "error")
    return (
      <Badge variant="destructive">
        <TriangleAlertIcon data-icon="inline-start" />
        Failed
      </Badge>
    )
  return <Badge variant="outline">Done</Badge>
}

// --- assistant message parts: tool calls and trades ---

/** Tool calls as compact status rows (one Trace per consecutive run); trades as cards. */
export function PartsList({ parts }: { parts: Part[] }) {
  const groups: Part[][] = []
  for (const p of parts) {
    const last = groups.at(-1)
    if (p.kind === "tool" && last?.[0].kind === "tool") last.push(p)
    else groups.push([p])
  }
  return (
    <div className="flex flex-col gap-2">
      {groups.map((g, i) =>
        g[0].kind === "trade" ? (
          <TradeCard key={i} trade={g[0]} />
        ) : (
          <Trace key={i}>
            {(g as ToolPart[]).map((t, j) => (
              <ToolRow key={j} tool={t} />
            ))}
          </Trace>
        )
      )}
    </div>
  )
}

const humanize = (name: string) =>
  name.charAt(0).toUpperCase() + name.slice(1).replace(/_/g, " ")

function ToolRow({ tool }: { tool: ToolPart }) {
  const args = Object.values(tool.args)
    .filter((v) => v != null && v !== "")
    .join(" ")
  const result = tool.result as Record<string, unknown> | undefined
  const detail =
    tool.status === "error" && result != null
      ? typeof result.error === "string"
        ? result.error
        : JSON.stringify(result)
      : args
  return (
    <TraceLine
      status={
        tool.status === "running"
          ? "running"
          : tool.status === "error"
            ? "error"
            : undefined
      }
    >
      <TraceIcon>
        {tool.status === "running" ? (
          <WrenchIcon />
        ) : tool.status === "ok" ? (
          <CheckIcon />
        ) : (
          <XIcon />
        )}
      </TraceIcon>
      <TraceTitle>{humanize(tool.name)}</TraceTitle>
      {detail ? (
        <TraceDescription className="max-w-[40ch] truncate font-mono text-xs">
          {detail}
        </TraceDescription>
      ) : null}
    </TraceLine>
  )
}

const num = (v: unknown) => {
  const n = v == null || v === "" ? NaN : Number(v)
  return Number.isFinite(n) && n !== 0 ? n : undefined
}

const TRADE_TITLE: Record<TradePart["status"], string> = {
  filled: "Order filled",
  new: "Order placed",
  rejected: "Order rejected",
  error: "Order failed",
}

export function TradeCard({ trade }: { trade: TradePart }) {
  const ok = trade.status === "filled" || trade.status === "new"
  const o = trade.order ?? {}
  const r = trade.request ?? {}
  const side = String(o.side ?? r.side ?? "").toUpperCase()
  const executed = num(o.executed_qty)
  const baseQty = executed ?? num(o.orig_qty) ?? num(r.quantity)
  const quote = num(r.quote_amount)
  // Average fill price for market orders, else the limit price.
  const filledQuote = num(o.quote_qty)
  const price =
    executed && filledQuote
      ? filledQuote / executed
      : (num(o.price) ?? num(r.price))
  const fields: [string, string | undefined][] = [
    [
      "Quantity",
      baseQty != null
        ? String(baseQty)
        : quote != null
          ? `${quote} USDT`
          : undefined,
    ],
    ["Price", price != null ? fmtPrice(price) : "Market"],
    ["Order", o.order_id != null ? `#${String(o.order_id)}` : undefined],
  ]
  return (
    <div
      role="status"
      className={cn(
        "flex max-w-md flex-col gap-2 rounded-lg border p-3 text-xs",
        ok ? "border-up/30 bg-up/5" : "border-down/30 bg-down/5"
      )}
    >
      <div className="flex items-center gap-2">
        <span
          className={cn(
            "flex size-5 items-center justify-center rounded-full",
            ok ? "bg-up/15 text-up" : "bg-down/15 text-down"
          )}
          aria-hidden
        >
          {ok ? <CheckIcon className="size-3" /> : <XIcon className="size-3" />}
        </span>
        <span className="text-sm font-medium">{TRADE_TITLE[trade.status]}</span>
        <span className="ml-auto flex items-center gap-1.5 font-mono">
          {side ? (
            <span className={side === "BUY" ? "text-up" : "text-down"}>
              {side}
            </span>
          ) : null}
          <span>{String(o.symbol ?? r.symbol ?? "")}</span>
        </span>
      </div>
      <dl className="grid grid-cols-3 gap-2 font-mono tabular-nums">
        {fields.map(([k, v]) => (
          <div key={k} className="flex min-w-0 flex-col gap-0.5">
            <dt className="font-sans text-muted-foreground">{k}</dt>
            <dd className="truncate">{v ?? "—"}</dd>
          </div>
        ))}
      </dl>
      {trade.reason ? (
        <p
          className={cn(
            "text-pretty",
            ok ? "text-muted-foreground" : "text-down"
          )}
        >
          {trade.reason}
        </p>
      ) : null}
    </div>
  )
}
