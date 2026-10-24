import { useEffect } from "react"
import { MessageSquarePlusIcon, SparklesIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { Skeleton } from "@/components/ui/skeleton"
import {
  refreshAnalyses,
  useAnalyses,
  useAnalysis,
  type AnalysisSummary,
} from "@/lib/analysis"
import { newChat, sendChat, useChat } from "@/lib/chat"
import { navigate, useRoute } from "@/lib/nav"
import { useTradingStatus } from "@/lib/tradingStatus"
import { cn } from "@/lib/utils"

import { Composer } from "./Composer"
import { RunStatus } from "./parts"
import { RunView } from "./RunView"
import { Thread } from "./Thread"

/**
 * The Assistant tab: run history on the left, the shared conversation in the middle, the selected
 * run (#/assistant?run=<id>) on the right. Below 1280px the run replaces the conversation.
 */
export default function AssistantWorkspace() {
  const { params } = useRoute()
  const runId = params.get("run")
  const close = () => navigate("assistant")

  // Esc closes the open run (it covers the conversation below 1280px), unless typing in a field.
  useEffect(() => {
    if (!runId) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return
      const t = e.target
      if (t instanceof HTMLElement && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))
        return
      navigate("assistant")
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [runId])

  return (
    <div className="flex min-h-0 flex-1 max-lg:h-[calc(100svh-5.5rem)]">
      <HistoryRail selected={runId} />
      <div
        className={cn(
          "flex min-h-0 min-w-0 flex-1 flex-col",
          runId && "max-xl:hidden xl:w-[28rem] xl:flex-none xl:border-r"
        )}
      >
        <MobileBar selected={runId} />
        <Thread limit={50} selectedRun={runId} empty={<ThreadEmpty />} />
        <Composer variant="workspace" />
      </div>
      {runId ? (
        <RunView
          key={runId}
          id={runId}
          onClose={close}
          // Narrow layouts show one pane: switch to the conversation holding the pre-filled message.
          onAct={() => {
            if (!window.matchMedia("(min-width: 80rem)").matches) close()
          }}
        />
      ) : null}
    </div>
  )
}

// --- history ---

function when(iso: string) {
  const d = new Date(iso)
  const today = d.toDateString() === new Date().toDateString()
  return d.toLocaleString(
    undefined,
    today
      ? { timeStyle: "short" }
      : { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }
  )
}

function startNewChat() {
  newChat()
  navigate("assistant")
}

function HistoryRail({ selected }: { selected: string | null }) {
  const { list, error, loading } = useAnalyses()
  const { messages } = useChat()
  return (
    <nav
      aria-label="Analysis history"
      className="hidden w-60 shrink-0 flex-col border-r bg-sidebar lg:flex"
    >
      <div className="p-2">
        <Button
          variant="outline"
          size="sm"
          className="w-full justify-start"
          disabled={!messages.length}
          onClick={startNewChat}
        >
          <MessageSquarePlusIcon data-icon="inline-start" />
          New chat
        </Button>
      </div>
      <h2 className="px-4 pt-2 pb-1.5 text-xs font-medium text-muted-foreground">
        Analyses
      </h2>
      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {loading && !list ? (
          <div
            className="flex flex-col gap-1"
            role="status"
            aria-label="Loading analyses"
          >
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-12 rounded-lg" />
            ))}
          </div>
        ) : error && !list ? (
          <div className="flex flex-col items-start gap-2 px-2 text-xs text-muted-foreground">
            <p className="text-pretty">Could not load the analyses: {error}</p>
            <Button
              variant="outline"
              size="xs"
              onClick={() => void refreshAnalyses()}
            >
              Retry
            </Button>
          </div>
        ) : list?.length ? (
          <ul className="flex flex-col gap-0.5">
            {list.map((s) => (
              <RailItem key={s.id} summary={s} selected={s.id === selected} />
            ))}
          </ul>
        ) : (
          <p className="px-2 text-xs text-pretty text-muted-foreground">
            No analyses yet. Choose analysts under the message box and run one.
          </p>
        )}
      </div>
    </nav>
  )
}

function RailItem({
  summary: s,
  selected,
}: {
  summary: AnalysisSummary
  selected: boolean
}) {
  // A running run is followed live, so its row turns into a rating the moment it finishes.
  const live = useAnalysis(s.status === "running" ? s.id : null)
  const status = live?.ended ? live.status : s.status
  const rating = live?.decision?.rating ?? s.rating
  return (
    <li>
      <button
        type="button"
        aria-current={selected || undefined}
        onClick={() => navigate("assistant", { run: s.id })}
        className={cn(
          "relative flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left transition-colors outline-none hover:bg-sidebar-accent focus-visible:ring-3 focus-visible:ring-ring/50",
          selected &&
            "bg-sidebar-accent before:absolute before:inset-y-2 before:left-0 before:w-0.5 before:rounded-full before:bg-primary"
        )}
      >
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="truncate font-mono text-sm">{s.symbol}</span>
          <span className="text-2xs text-muted-foreground tabular-nums">
            {when(s.started_at)}
          </span>
        </span>
        <RunStatus status={status} rating={rating} />
      </button>
    </li>
  )
}

/** Below 1024px the rail folds into a native select. */
function MobileBar({ selected }: { selected: string | null }) {
  const { list } = useAnalyses()
  return (
    <div className="flex items-center gap-2 border-b p-2 lg:hidden">
      <select
        aria-label="Open an analysis"
        className="h-8 min-w-0 flex-1 rounded-md border bg-card px-2 text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
        value={selected ?? ""}
        onChange={(e) =>
          navigate(
            "assistant",
            e.target.value ? { run: e.target.value } : undefined
          )
        }
      >
        <option value="">Conversation</option>
        {list?.map((s) => (
          <option key={s.id} value={s.id}>
            {s.symbol}, {when(s.started_at)}
            {s.rating
              ? `: ${s.rating}`
              : s.status === "running"
                ? ": running"
                : ""}
          </option>
        ))}
      </select>
      <Button
        variant="outline"
        size="icon-sm"
        aria-label="New chat"
        onClick={startNewChat}
      >
        <MessageSquarePlusIcon />
      </Button>
    </div>
  )
}

function ThreadEmpty() {
  const { context } = useChat()
  const { refresh } = useTradingStatus()
  const prompts = [
    `How is ${context.symbol} trending on the 4h chart?`,
    "What are my balances and open orders?",
    `Is now a good moment to add to ${context.symbol}?`,
  ]
  return (
    <Empty className="m-auto max-w-md border-0">
      <EmptyHeader>
        <EmptyMedia variant="icon">
          <SparklesIcon />
        </EmptyMedia>
        <EmptyTitle>Ask about the market or your account</EmptyTitle>
        <EmptyDescription className="text-pretty">
          The assistant reads live Binance prices, indicators and your account
          balances. For a second opinion from the whole analyst team, run a full
          analysis below.
        </EmptyDescription>
      </EmptyHeader>
      <EmptyContent className="flex-col items-stretch gap-1.5">
        {prompts.map((p) => (
          <Button
            key={p}
            variant="outline"
            size="sm"
            className="h-auto justify-start py-1.5 text-left whitespace-normal"
            onClick={() => void sendChat(p, refresh)}
          >
            {p}
          </Button>
        ))}
      </EmptyContent>
    </Empty>
  )
}
