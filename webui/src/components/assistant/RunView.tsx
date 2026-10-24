import { memo, useState } from "react"
import {
  ArrowDownIcon,
  CheckIcon,
  ChevronDownIcon,
  CircleSlashIcon,
  MessageSquareTextIcon,
  SquareIcon,
  TriangleAlertIcon,
  XIcon,
} from "lucide-react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import {
  STAGES,
  cancelAnalysis,
  cleanError,
  fmtDuration,
  ratingTone,
  useAnalysis,
  useNow,
  type AgentActivity,
  type Report,
  type RunState,
  type StageKey,
  type StageState,
} from "@/lib/analysis"
import { prefill } from "@/lib/chat"
import { toast } from "@/lib/toast"
import { cn } from "@/lib/utils"

import { MD_REPORT, Md, RunStatus } from "./parts"

// The Portfolio Manager's report is the decision itself: it renders in the decision card,
// not a second time under the risk debate.
const DECISION_SECTION = "final_trade_decision"

/** The selected run: pipeline stepper, full reports and the final decision. */
export function RunView({
  id,
  onClose,
  onAct,
}: {
  id: string
  onClose: () => void
  /** Called after the chat input was pre-filled, so a narrow layout can reveal the thread. */
  onAct: () => void
}) {
  const run = useAnalysis(id)
  if (!run) return null
  const symbol = run.summary?.symbol
  const running = run.status === "running" && !run.ended
  const started = run.summary?.started_at
    ? Date.parse(run.summary.started_at)
    : run.firstAt
  const finished = run.summary?.finished_at
    ? Date.parse(run.summary.finished_at)
    : run.ended
      ? run.lastAt
      : undefined
  const loading = run.lastId == null && !run.ended && !run.loadError

  return (
    <section
      aria-label={symbol ? `Analysis of ${symbol}` : "Analysis"}
      className="flex min-h-0 flex-1 flex-col bg-background"
    >
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b px-4 py-3">
        <div className="flex min-w-0 flex-col">
          <h2 className="flex items-baseline gap-2 text-base font-semibold">
            {symbol ? (
              <span className="font-mono tracking-tight">{symbol}</span>
            ) : (
              <Skeleton className="h-5 w-24" />
            )}
            <span className="text-sm font-normal text-muted-foreground">
              Full analysis
            </span>
          </h2>
          <p className="text-xs text-muted-foreground tabular-nums">
            {run.summary?.date ? `Market date ${run.summary.date}` : null}
            {run.summary?.date && started ? ", " : null}
            {started ? (
              running ? (
                <Elapsed from={started} prefix="running for " />
              ) : finished ? (
                `took ${fmtDuration(finished - started)}`
              ) : null
            ) : null}
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <RunStatus
            status={running ? "running" : run.status}
            rating={run.decision?.rating ?? run.summary?.rating}
          />
          {run.decision ? (
            <Button
              variant="ghost"
              size="sm"
              onClick={() =>
                document
                  .getElementById(`decision-${id}`)
                  ?.scrollIntoView({ block: "start" })
              }
            >
              <ArrowDownIcon data-icon="inline-start" />
              Decision
            </Button>
          ) : null}
          {running ? <CancelButton id={id} /> : null}
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={onClose}
            aria-label="Close the run"
          >
            <XIcon />
          </Button>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-4 py-5">
          {run.connection === "retrying" && !run.ended ? (
            <p
              role="status"
              className="flex items-center gap-2 text-xs text-warn"
            >
              <Spinner /> Connection lost. Re-attaching to the run…
            </p>
          ) : null}
          {run.loadError && run.ended ? (
            <Alert variant="destructive">
              <TriangleAlertIcon />
              <AlertTitle>Could not load this run</AlertTitle>
              <AlertDescription>{run.loadError}</AlertDescription>
            </Alert>
          ) : null}
          {loading ? <StepperSkeleton /> : <Stepper run={run} />}
          {run.error ? (
            <Alert variant="destructive">
              <TriangleAlertIcon />
              <AlertTitle>The analysis stopped with an error</AlertTitle>
              <AlertDescription>{run.error}</AlertDescription>
            </Alert>
          ) : null}
          {run.status === "cancelled" ? (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <CircleSlashIcon className="size-4" /> Cancelled. Reports filed
              before the cancel are kept above.
            </p>
          ) : null}
          {run.decision ? (
            <DecisionCard
              run={run}
              durationMs={started && finished ? finished - started : undefined}
              onAct={onAct}
            />
          ) : null}
        </div>
      </div>
    </section>
  )
}

function CancelButton({ id }: { id: string }) {
  const [pending, setPending] = useState(false)
  return (
    <Button
      variant="outline"
      size="sm"
      disabled={pending}
      onClick={async () => {
        setPending(true)
        try {
          await cancelAnalysis(id)
          toast({ kind: "info", title: "Analysis cancelled", key: id })
        } catch (err) {
          toast({
            kind: "error",
            title: "Could not cancel the analysis",
            body: cleanError(err),
          })
        } finally {
          setPending(false)
        }
      }}
    >
      {pending ? (
        <Spinner data-icon="inline-start" />
      ) : (
        <SquareIcon data-icon="inline-start" />
      )}
      Cancel
    </Button>
  )
}

// --- stepper ---

function Stepper({ run }: { run: RunState }) {
  const stopped = run.ended || run.status !== "running"
  return (
    <ol aria-label="Pipeline" className="flex flex-col">
      {run.order.map((key, i) => (
        <Stage
          key={key}
          stageKey={key}
          state={run.stages[key]}
          stopped={stopped}
          last={i === run.order.length - 1}
          activity={run.activity.filter((a) => a.stage === key)}
          reports={run.reports.filter(
            (r) => r.stage === key && r.section !== DECISION_SECTION
          )}
        />
      ))}
    </ol>
  )
}

type NodeStatus = StageState["status"] | "interrupted"

function Stage({
  stageKey,
  state,
  stopped,
  last,
  activity,
  reports,
}: {
  stageKey: StageKey
  state: StageState | undefined
  stopped: boolean
  last: boolean
  activity: AgentActivity[]
  reports: Report[]
}) {
  const meta = STAGES[stageKey]
  // A run that ended mid-stage leaves that stage interrupted, not spinning.
  const status: NodeStatus =
    state?.status === "running" && stopped
      ? "interrupted"
      : (state?.status ?? "pending")
  return (
    <li
      className="grid grid-cols-[1.25rem_minmax(0,1fr)] gap-x-3"
      aria-current={status === "running" ? "step" : undefined}
    >
      <div className="flex flex-col items-center">
        <StageNode status={status} />
        {last ? null : (
          <span
            aria-hidden
            className={cn(
              "my-1 w-px flex-1",
              status === "done" ? "bg-up/40" : "bg-border"
            )}
          />
        )}
      </div>
      <div className={cn("flex min-w-0 flex-col gap-2", !last && "pb-5")}>
        <div className="flex min-h-5 flex-wrap items-baseline gap-x-2">
          <h3
            className={cn(
              "text-sm font-medium",
              status === "pending" && "text-muted-foreground"
            )}
          >
            {meta.label}
          </h3>
          <span className="text-xs text-muted-foreground">{meta.agents}</span>
          <span className="ml-auto text-xs text-muted-foreground tabular-nums">
            <StageTime status={status} state={state} stopped={stopped} />
          </span>
        </div>
        {status === "running" && state?.agent ? (
          <p className="text-xs text-primary">{state.agent} is working</p>
        ) : null}
        {activity.length ? <ActivityLines activity={activity} /> : null}
        {reports.map((r) => (
          <ReportBlock key={r.section} report={r} />
        ))}
      </div>
    </li>
  )
}

function StageNode({ status }: { status: NodeStatus }) {
  return (
    <span
      aria-hidden
      className={cn(
        "mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full border",
        status === "done" && "border-up/50 bg-up/15 text-up",
        status === "running" &&
          "border-primary bg-primary/10 text-primary motion-safe:animate-pulse",
        status === "interrupted" && "border-warn/50 text-warn",
        status === "pending" && "border-border bg-card"
      )}
    >
      {status === "done" ? (
        <CheckIcon className="size-3" />
      ) : status === "running" ? (
        <span className="size-1.5 rounded-full bg-primary" />
      ) : status === "interrupted" ? (
        <SquareIcon className="size-2" />
      ) : null}
    </span>
  )
}

function StageTime({
  status,
  state,
  stopped,
}: {
  status: NodeStatus
  state: StageState | undefined
  stopped: boolean
}) {
  if (status === "running" && state?.startedAt)
    return <Elapsed from={state.startedAt} />
  if (status === "done" && state?.startedAt && state.endedAt)
    return <>{fmtDuration(state.endedAt - state.startedAt) || "done"}</>
  if (status === "interrupted") return <>stopped</>
  if (status === "done") return <>done</>
  return <>{stopped ? "not run" : "waiting"}</>
}

/** Ticks once a second on its own, so the rest of the run view does not re-render. */
function Elapsed({ from, prefix = "" }: { from: number; prefix?: string }) {
  const now = useNow(true)
  return (
    <>
      {prefix}
      {fmtDuration(Math.max(0, now - from))}
    </>
  )
}

/** "Market Analyst · get_stock_data, get_indicators ×3": one folded line per agent. */
function ActivityLines({ activity }: { activity: AgentActivity[] }) {
  return (
    <ul className="flex flex-col gap-1">
      {activity.map((a) =>
        a.tools.length ? (
          <li
            key={a.agent}
            className="flex min-w-0 gap-1.5 text-xs text-muted-foreground"
          >
            <span className="shrink-0 text-foreground/80">{a.agent}</span>
            <span aria-hidden>·</span>
            <span className="min-w-0 font-mono text-pretty">
              {a.tools
                .map((t) => (t.count > 1 ? `${t.name} ×${t.count}` : t.name))
                .join(", ")}
            </span>
          </li>
        ) : null
      )}
    </ul>
  )
}

// Memoised: a new event re-renders only the reports whose content changed.
const ReportBlock = memo(function ReportBlock({ report }: { report: Report }) {
  return (
    <Collapsible defaultOpen className="group/report rounded-lg border bg-card">
      <CollapsibleTrigger className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm font-medium outline-none hover:bg-surface/60 focus-visible:ring-3 focus-visible:ring-ring/50">
        {report.title}
        <ChevronDownIcon className="ml-auto size-4 text-muted-foreground transition-transform group-data-[state=closed]/report:-rotate-90 motion-reduce:transition-none" />
      </CollapsibleTrigger>
      <CollapsibleContent className="border-t px-4 py-3">
        <div className={MD_REPORT}>
          <Md>{report.content}</Md>
        </div>
      </CollapsibleContent>
    </Collapsible>
  )
})

function StepperSkeleton() {
  return (
    <div
      className="flex flex-col gap-5"
      role="status"
      aria-label="Loading the run"
    >
      {[0, 1, 2, 3, 4].map((i) => (
        <div key={i} className="flex items-center gap-3">
          <Skeleton className="size-5 rounded-full" />
          <Skeleton className="h-4 w-40" />
          <Skeleton className="ml-auto h-3 w-10" />
        </div>
      ))}
    </div>
  )
}

// --- decision ---

const SCALE = ["Sell", "Underweight", "Hold", "Overweight", "Buy"] as const

const TONE_TEXT = {
  up: "text-up",
  down: "text-down",
  hold: "text-primary",
  none: "text-foreground",
} as const
const TONE_FILL = {
  up: "bg-up",
  down: "bg-down",
  hold: "bg-primary",
  none: "bg-foreground",
} as const

function DecisionCard({
  run,
  durationMs,
  onAct,
}: {
  run: RunState
  durationMs: number | undefined
  onAct: () => void
}) {
  const decision = run.decision!
  const tone = ratingTone(decision.rating)
  const symbol = run.summary?.symbol ?? "this symbol"
  const at = decision.at ?? run.lastAt
  const active = SCALE.findIndex(
    (s) => s.toUpperCase() === decision.rating.trim().toUpperCase()
  )

  const act = () => {
    const summary = decision.summary
      .replace(/[*_#>`|]+/g, "") // markdown markers read as noise in a chat message
      .replace(/\s+/g, " ")
      .trim()
    const date = run.summary?.date ? `, market date ${run.summary.date}` : ""
    prefill(
      `The full analysis of ${symbol} (run ${run.id}${date}) ended with the rating ${decision.rating}.\n` +
        `Portfolio Manager summary: ${summary.length > 700 ? `${summary.slice(0, 700)}…` : summary}\n\n` +
        `Given my balances and trading limits, what would you do about ${symbol} now? Propose a concrete order, and wait for my confirmation before placing anything.`
    )
    onAct()
  }

  return (
    <section
      id={`decision-${run.id}`}
      aria-label="Decision"
      className="scroll-mt-4 overflow-hidden rounded-xl border bg-card"
    >
      <div className="flex flex-col gap-4 p-5">
        <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
          <div className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground">
              Portfolio Manager’s rating for {symbol}
            </span>
            <span
              // Plain strings: tailwind-merge reads the custom text-display/text-2xs sizes as colours.
              className={`text-display font-semibold tracking-tight ${TONE_TEXT[tone]}`}
            >
              {decision.rating || "No rating"}
            </span>
          </div>
          <dl className="flex gap-6 text-xs tabular-nums">
            {at ? (
              <div className="flex flex-col gap-0.5">
                <dt className="text-muted-foreground">Decided</dt>
                <dd>
                  {new Date(at).toLocaleString(undefined, {
                    dateStyle: "medium",
                    timeStyle: "short",
                  })}
                </dd>
              </div>
            ) : null}
            {durationMs ? (
              <div className="flex flex-col gap-0.5">
                <dt className="text-muted-foreground">Duration</dt>
                <dd>{fmtDuration(durationMs)}</dd>
              </div>
            ) : null}
          </dl>
        </div>
        {/* The five-tier scale, with this run's rating lit. */}
        <div
          className="grid grid-cols-5 gap-1"
          role="img"
          aria-label={`Rating ${decision.rating} on the scale Sell, Underweight, Hold, Overweight, Buy`}
        >
          {SCALE.map((s, i) => (
            <div key={s} className="flex min-w-0 flex-col gap-1.5">
              <span
                className={cn(
                  "h-1.5 rounded-full",
                  i === active ? TONE_FILL[tone] : "bg-muted"
                )}
              />
              <span
                className={`truncate text-2xs ${i === active ? `font-medium ${TONE_TEXT[tone]}` : "text-muted-foreground"}`}
              >
                {s}
              </span>
            </div>
          ))}
        </div>
        {decision.summary ? (
          <div className={cn(MD_REPORT, "border-t pt-4")}>
            <Md>{decision.summary}</Md>
          </div>
        ) : null}
      </div>
      <footer className="flex flex-wrap items-center gap-3 border-t bg-surface/40 px-5 py-3">
        <Button size="sm" onClick={act}>
          <MessageSquareTextIcon data-icon="inline-start" />
          Ask the assistant to act on this
        </Button>
        <span className="text-xs text-pretty text-muted-foreground">
          Fills in the chat message. Nothing is ordered until you send it and
          confirm.
        </span>
      </footer>
    </section>
  )
}

/** For the thread's run cards: one segment per stage, while the run is live. */
export function RunProgress({ id }: { id: string }) {
  const run = useAnalysis(id)
  if (!run || run.ended) return null
  return (
    <span className="flex gap-0.5" aria-hidden>
      {run.order.map((k) => (
        <span
          key={k}
          className={cn(
            "h-1 w-3 rounded-full",
            run.stages[k]?.status === "done"
              ? "bg-up"
              : run.stages[k]?.status === "running"
                ? "bg-primary motion-safe:animate-pulse"
                : "bg-muted"
          )}
        />
      ))}
    </span>
  )
}
