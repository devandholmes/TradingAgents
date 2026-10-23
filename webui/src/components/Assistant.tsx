import { Suspense, lazy, useRef, useState } from "react"
import {
  ArrowUpIcon,
  CheckIcon,
  ChevronRightIcon,
  PlayIcon,
  SquareIcon,
  TriangleAlertIcon,
  WrenchIcon,
  XIcon,
} from "lucide-react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Bubble, BubbleContent } from "@/components/ui/bubble"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import { Input } from "@/components/ui/input"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupTextarea,
} from "@/components/ui/input-group"
import { Marker, MarkerContent, MarkerIcon } from "@/components/ui/marker"
import { Message, MessageContent } from "@/components/ui/message"
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "@/components/ui/message-scroller"
import { Spinner } from "@/components/ui/spinner"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  Trace,
  TraceDescription,
  TraceIcon,
  TraceLine,
  TraceTitle,
} from "@/components/ui/trace"
import { refreshTradingStatus } from "@/lib/api"
import { fmtPrice, streamSSE } from "@/lib/market"
import { cn } from "@/lib/utils"

// Compact markdown styling (no typography plugin).
const MD =
  "text-sm leading-relaxed [&_a]:underline [&_code]:rounded [&_code]:bg-background/60 [&_code]:px-1 [&_code]:font-mono [&_code]:text-xs [&_h1]:font-semibold [&_h2]:font-semibold [&_h3]:font-semibold [&_li]:my-0.5 [&_ol]:list-decimal [&_ol]:pl-5 [&_p]:my-1.5 [&_pre]:overflow-x-auto [&_pre]:rounded [&_pre]:bg-background/60 [&_pre]:p-2 [&_table]:my-2 [&_table]:text-xs [&_td]:border [&_td]:px-1.5 [&_th]:border [&_th]:px-1.5 [&_ul]:list-disc [&_ul]:pl-5 [&>*:first-child]:mt-0 [&>*:last-child]:mb-0"

// react-markdown (+ unified/remark) is ~120 kB; load it only once there is something to render.
const loadMarkdown = () => import("react-markdown")
const Markdown = lazy(loadMarkdown)

function Md({ children }: { children: string }) {
  return (
    <Suspense fallback={<p className="whitespace-pre-wrap">{children}</p>}>
      <Markdown>{children}</Markdown>
    </Suspense>
  )
}

export function Assistant({
  symbol,
  interval,
}: {
  symbol: string
  interval: string
}) {
  return (
    <Card size="sm" className="min-h-[520px] gap-0 py-0 lg:min-h-0">
      <Tabs defaultValue="chat" className="min-h-0 flex-1 gap-0">
        <div className="border-b p-(--card-spacing) pb-2">
          <TabsList className="w-full">
            <TabsTrigger value="chat">Chat</TabsTrigger>
            <TabsTrigger value="analyze">Analyze</TabsTrigger>
          </TabsList>
        </div>
        {/* forceMount keeps a running chat/analysis alive while the other tab is shown. */}
        <TabsContent
          value="chat"
          forceMount
          className="flex min-h-0 flex-col data-[state=inactive]:hidden"
        >
          <Chat symbol={symbol} interval={interval} />
        </TabsContent>
        <TabsContent
          value="analyze"
          forceMount
          className="min-h-0 overflow-y-auto data-[state=inactive]:hidden"
        >
          <Analyze symbol={symbol} />
        </TabsContent>
      </Tabs>
    </Card>
  )
}

type ToolActivity = {
  kind: "tool"
  name: string
  args: Record<string, unknown>
  status: "running" | "ok" | "error"
  result?: unknown
}
type TradeActivity = {
  kind: "trade"
  status: "filled" | "new" | "rejected" | "error"
  order?: Record<string, unknown>
  reason?: string
  /** Args of the place_order call that produced it (rejections carry no order). */
  request?: Record<string, unknown>
}
type Activity = ToolActivity | TradeActivity

type ChatMessage = {
  id: number
  role: "user" | "assistant"
  content: string
  activity?: Activity[]
  error?: string
}

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v)

/** A finished tool event replaces its own "running" row; anything else appends. */
function withTool(list: Activity[], raw: Record<string, unknown>): Activity[] {
  const tool: ToolActivity = {
    kind: "tool",
    name: String(raw.name ?? "tool"),
    args: isRecord(raw.args) ? raw.args : {},
    status:
      raw.status === "ok" || raw.status === "error" ? raw.status : "running",
    result: raw.result,
  }
  if (tool.status !== "running") {
    const i = list.findLastIndex(
      (a) => a.kind === "tool" && a.name === tool.name && a.status === "running"
    )
    if (i !== -1) return list.with(i, tool)
  }
  return [...list, tool]
}

function withTrade(list: Activity[], raw: Record<string, unknown>): Activity[] {
  const status: TradeActivity["status"] =
    raw.status === "filled" || raw.status === "new" || raw.status === "rejected"
      ? raw.status
      : "error"
  const call = list.findLast(
    (a): a is ToolActivity => a.kind === "tool" && a.name === "place_order"
  )
  return [
    ...list,
    {
      kind: "trade",
      status,
      order: isRecord(raw.order) ? raw.order : undefined,
      reason: typeof raw.reason === "string" ? raw.reason : undefined,
      request: call?.args,
    },
  ]
}

function Chat({ symbol, interval }: { symbol: string; interval: string }) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [draft, setDraft] = useState("")
  const [busy, setBusy] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const nextId = useRef(0)

  const patch = (id: number, fn: (m: ChatMessage) => ChatMessage) =>
    setMessages((ms) => ms.map((m) => (m.id === id ? fn(m) : m)))

  async function send() {
    const text = draft.trim()
    if (!text || busy) return
    void loadMarkdown() // warm the chunk while the model thinks
    const history = [
      ...messages
        .filter((m) => !m.error && m.content)
        .map(({ role, content }) => ({ role, content })),
      { role: "user" as const, content: text },
    ]
    const userId = nextId.current++
    const replyId = nextId.current++
    setMessages((ms) => [
      ...ms,
      { id: userId, role: "user", content: text },
      { id: replyId, role: "assistant", content: "" },
    ])
    setDraft("")
    setBusy(true)
    const ctrl = (abortRef.current = new AbortController())
    try {
      await streamSSE(
        "/api/chat",
        { messages: history, symbol, interval },
        (ev) => {
          if (typeof ev.delta === "string")
            patch(replyId, (m) => ({ ...m, content: m.content + ev.delta }))
          else if (isRecord(ev.tool)) {
            const tool = ev.tool
            patch(replyId, (m) => ({
              ...m,
              activity: withTool(m.activity ?? [], tool),
            }))
          } else if (isRecord(ev.trade)) {
            const trade = ev.trade
            patch(replyId, (m) => ({
              ...m,
              activity: withTrade(m.activity ?? [], trade),
            }))
            void refreshTradingStatus() // used_today_usdt moved
          } else if (typeof ev.error === "string")
            patch(replyId, (m) => ({ ...m, error: ev.error as string }))
        },
        ctrl.signal
      )
    } catch (err) {
      if (!ctrl.signal.aborted)
        patch(replyId, (m) => ({ ...m, error: (err as Error).message }))
    } finally {
      setBusy(false)
      abortRef.current = null
    }
  }

  return (
    <>
      <div className="min-h-0 flex-1">
        <MessageScrollerProvider>
          <MessageScroller>
            <MessageScrollerViewport>
              <MessageScrollerContent className="gap-4 p-(--card-spacing)">
                {messages.length === 0 ? (
                  <MessageScrollerItem>
                    <Marker>
                      <MarkerContent className="text-muted-foreground">
                        Ask about {symbol} on the {interval} chart. The
                        assistant sees a live snapshot of price and indicators.
                        Not financial advice. It places orders only when you
                        explicitly ask, within the server&apos;s trading limits.
                      </MarkerContent>
                    </Marker>
                  </MessageScrollerItem>
                ) : null}
                {messages.map((m) => (
                  <MessageScrollerItem
                    key={m.id}
                    messageId={String(m.id)}
                    scrollAnchor={m.role === "user"}
                  >
                    <Message align={m.role === "user" ? "end" : "start"}>
                      <MessageContent>
                        {m.activity?.length ? (
                          <ActivityList items={m.activity} />
                        ) : null}
                        {m.role === "user" ? (
                          <Bubble variant="secondary">
                            <BubbleContent className="whitespace-pre-wrap">
                              {m.content}
                            </BubbleContent>
                          </Bubble>
                        ) : m.content ? (
                          <Bubble variant="ghost">
                            <BubbleContent className={MD}>
                              <Md>{m.content}</Md>
                            </BubbleContent>
                          </Bubble>
                        ) : !m.error ? (
                          <Marker role="status">
                            <MarkerIcon>
                              <Spinner />
                            </MarkerIcon>
                            <MarkerContent>Thinking…</MarkerContent>
                          </Marker>
                        ) : null}
                        {m.error ? (
                          <Bubble variant="destructive">
                            <BubbleContent>{m.error}</BubbleContent>
                          </Bubble>
                        ) : null}
                      </MessageContent>
                    </Message>
                  </MessageScrollerItem>
                ))}
              </MessageScrollerContent>
            </MessageScrollerViewport>
            <MessageScrollerButton />
          </MessageScroller>
        </MessageScrollerProvider>
      </div>
      <form
        className="border-t p-(--card-spacing)"
        onSubmit={(e) => {
          e.preventDefault()
          void send()
        }}
      >
        <InputGroup>
          <InputGroupTextarea
            aria-label="Message"
            placeholder={`Ask about ${symbol}…`}
            className="max-h-32 min-h-10"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault()
                void send()
              }
            }}
          />
          <InputGroupAddon align="block-end" className="p-2">
            <span className="font-mono text-xs text-muted-foreground">
              {symbol} · {interval}
            </span>
            {busy ? (
              <InputGroupButton
                size="icon-sm"
                type="button"
                className="ml-auto"
                onClick={() => abortRef.current?.abort()}
              >
                <SquareIcon />
                <span className="sr-only">Stop</span>
              </InputGroupButton>
            ) : (
              <InputGroupButton
                variant="default"
                size="icon-sm"
                type="submit"
                className="ml-auto"
                disabled={!draft.trim()}
              >
                <ArrowUpIcon />
                <span className="sr-only">Send</span>
              </InputGroupButton>
            )}
          </InputGroupAddon>
        </InputGroup>
      </form>
    </>
  )
}

/** Tool calls as compact status rows (one Trace per consecutive run); trades as cards. */
function ActivityList({ items }: { items: Activity[] }) {
  const groups: Activity[][] = []
  for (const a of items) {
    const last = groups.at(-1)
    if (a.kind === "tool" && last?.[0].kind === "tool") last.push(a)
    else groups.push([a])
  }
  return (
    <div className="flex flex-col gap-2">
      {groups.map((g, i) =>
        g[0].kind === "trade" ? (
          <TradeCard key={i} trade={g[0]} />
        ) : (
          <Trace key={i}>
            {(g as ToolActivity[]).map((t, j) => (
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

function ToolRow({ tool }: { tool: ToolActivity }) {
  const args = Object.values(tool.args)
    .filter((v) => v != null && v !== "")
    .join(" ")
  const detail =
    tool.status === "error" && tool.result != null
      ? isRecord(tool.result)
        ? JSON.stringify(tool.result)
        : String(tool.result)
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
        <TraceDescription className="truncate font-mono text-xs">
          {detail.slice(0, 120)}
        </TraceDescription>
      ) : null}
    </TraceLine>
  )
}

const num = (v: unknown) => {
  const n = v == null || v === "" ? NaN : Number(v)
  return Number.isFinite(n) && n !== 0 ? n : undefined
}

function TradeCard({ trade }: { trade: TradeActivity }) {
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
      "Qty",
      baseQty != null
        ? String(baseQty)
        : quote != null
          ? `${quote} USDT`
          : undefined,
    ],
    ["Price", price != null ? fmtPrice(price) : "market"],
    ["Order", o.order_id != null ? `#${String(o.order_id)}` : undefined],
  ]
  return (
    <div
      role="status"
      className={cn(
        "flex flex-col gap-1.5 rounded-lg border p-2.5 text-xs",
        ok ? "border-up/40 bg-up/5" : "border-down/40 bg-down/5"
      )}
    >
      <div className="flex items-center gap-2">
        <Badge
          variant="outline"
          className={cn(
            "font-mono uppercase",
            ok ? "border-up/40 text-up" : "border-down/40 text-down"
          )}
        >
          {trade.status}
        </Badge>
        <span className="font-mono font-medium">
          {String(o.symbol ?? r.symbol ?? "—")}
        </span>
        {side ? (
          <span
            className={cn(
              "font-mono font-medium",
              side === "BUY" ? "text-up" : "text-down"
            )}
          >
            {side}
          </span>
        ) : null}
      </div>
      <dl className="flex flex-wrap gap-x-4 gap-y-1 font-mono tabular-nums">
        {fields.map(([k, v]) =>
          v ? (
            <div key={k} className="flex gap-1.5">
              <dt className="text-muted-foreground">{k}</dt>
              <dd>{v}</dd>
            </div>
          ) : null
        )}
      </dl>
      {trade.reason ? (
        <p className={ok ? "text-muted-foreground" : "text-down"}>
          {trade.reason}
        </p>
      ) : null}
    </div>
  )
}

type Progress = { agent: string; message: string; at: number }
type Run = {
  symbol: string
  progress: Progress[]
  reports: { section: string; content: string }[]
  decision?: { rating: string; content: string }
  error?: string
  done: boolean
}

function ratingTone(rating: string) {
  const r = rating.toUpperCase()
  if (r.includes("BUY") || r.includes("OVERWEIGHT"))
    return "text-up border-up/40"
  if (r.includes("SELL") || r.includes("UNDERWEIGHT"))
    return "text-down border-down/40"
  return ""
}

function Analyze({ symbol }: { symbol: string }) {
  const [date, setDate] = useState("")
  const [run, setRun] = useState<Run | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const running = run != null && !run.done

  async function start() {
    const ctrl = (abortRef.current = new AbortController())
    const update = (fn: (r: Run) => Run) => setRun((r) => (r ? fn(r) : r))
    setRun({ symbol, progress: [], reports: [], done: false })
    try {
      await streamSSE(
        "/api/analyze",
        { symbol, date: date || null },
        (ev) => {
          switch (ev.type) {
            case "progress":
              update((r) => ({
                ...r,
                progress: [
                  ...r.progress,
                  {
                    agent: String(ev.agent),
                    message: String(ev.message),
                    at: Date.now(),
                  },
                ],
              }))
              break
            case "report":
              update((r) => ({
                ...r,
                reports: [
                  ...r.reports.filter((x) => x.section !== ev.section),
                  { section: String(ev.section), content: String(ev.content) },
                ],
              }))
              break
            case "decision":
              update((r) => ({
                ...r,
                decision: {
                  rating: String(ev.rating),
                  content: String(ev.content),
                },
              }))
              break
            case "error":
              update((r) => ({ ...r, error: String(ev.message) }))
              break
            case "done":
              update((r) => ({ ...r, done: true }))
              break
          }
        },
        ctrl.signal
      )
    } catch (err) {
      update((r) => ({
        ...r,
        error: ctrl.signal.aborted ? "Cancelled" : (err as Error).message,
      }))
    } finally {
      update((r) => ({ ...r, done: true }))
      abortRef.current = null
    }
  }

  return (
    <div className="flex flex-col gap-3 p-(--card-spacing)">
      <Alert>
        <TriangleAlertIcon />
        <AlertTitle>Full multi-agent analysis</AlertTitle>
        <AlertDescription>
          Runs the TradingAgents pipeline on {symbol}. This takes several
          minutes and uses LLM credits.
        </AlertDescription>
      </Alert>
      <div className="flex flex-wrap items-center gap-2">
        <Input
          type="date"
          aria-label="Analysis date (defaults to today)"
          value={date}
          onChange={(e) => setDate(e.target.value)}
          disabled={running}
          className="w-40 font-mono"
        />
        {running ? (
          <Button variant="outline" onClick={() => abortRef.current?.abort()}>
            <SquareIcon data-icon="inline-start" />
            Cancel
          </Button>
        ) : (
          <Button onClick={() => void start()}>
            <PlayIcon data-icon="inline-start" />
            Analyze {symbol}
          </Button>
        )}
        {running ? <Spinner className="text-muted-foreground" /> : null}
      </div>

      {run?.decision ? (
        <div className="flex flex-col gap-2 rounded-lg border p-3">
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground">
              Decision · {run.symbol}
            </span>
            <Badge
              variant="outline"
              className={cn(
                "font-mono uppercase",
                ratingTone(run.decision.rating)
              )}
            >
              {run.decision.rating}
            </Badge>
          </div>
          <div className={MD}>
            <Md>{run.decision.content}</Md>
          </div>
        </div>
      ) : null}

      {run?.error ? (
        <Alert variant="destructive">
          <AlertTitle>Analysis stopped</AlertTitle>
          <AlertDescription>{run.error}</AlertDescription>
        </Alert>
      ) : null}

      {run?.reports.length ? (
        <div className="flex flex-col gap-1">
          <span className="text-xs font-medium text-muted-foreground">
            Reports
          </span>
          {run.reports.map((r) => (
            <Collapsible key={r.section} className="rounded-md border">
              <CollapsibleTrigger className="group/trigger flex w-full items-center gap-2 px-2 py-1.5 text-left text-sm hover:bg-muted/60">
                <ChevronRightIcon className="size-4 text-muted-foreground transition-transform group-data-[state=open]/trigger:rotate-90" />
                <span className="capitalize">
                  {r.section.replace(/_/g, " ")}
                </span>
              </CollapsibleTrigger>
              <CollapsibleContent className={cn(MD, "border-t px-3 py-2")}>
                <Md>{r.content}</Md>
              </CollapsibleContent>
            </Collapsible>
          ))}
        </div>
      ) : null}

      {run?.progress.length ? (
        <div className="flex flex-col gap-1">
          <span className="text-xs font-medium text-muted-foreground">
            Progress
          </span>
          <ol className="flex flex-col border-l pl-3">
            {run.progress.map((p, i) => (
              <li key={i} className="relative py-1 text-xs">
                <span
                  className={cn(
                    "absolute top-2 -left-[17px] size-2 rounded-full bg-muted-foreground",
                    i === run.progress.length - 1 &&
                      running &&
                      "animate-pulse bg-primary"
                  )}
                />
                <span className="font-mono text-muted-foreground">
                  {new Date(p.at).toLocaleTimeString()}
                </span>{" "}
                <span className="font-medium">{p.agent}</span>{" "}
                <span className="text-muted-foreground">{p.message}</span>
              </li>
            ))}
          </ol>
        </div>
      ) : running ? (
        <Marker role="status">
          <MarkerIcon>
            <Spinner />
          </MarkerIcon>
          <MarkerContent>Starting agents…</MarkerContent>
        </Marker>
      ) : null}
    </div>
  )
}
