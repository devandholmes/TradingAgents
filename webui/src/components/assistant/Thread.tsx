import { memo, type ReactNode } from "react"
import { ChevronRightIcon, WorkflowIcon } from "lucide-react"

import { Bubble, BubbleContent } from "@/components/ui/bubble"
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
import { useAnalyses } from "@/lib/analysis"
import { useChat, type RunMessage, type TextMessage } from "@/lib/chat"
import { navigate } from "@/lib/nav"
import { cn } from "@/lib/utils"

import { MD, Md, PartsList, RunStatus } from "./parts"
import { RunProgress } from "./RunView"

/**
 * The shared conversation: chat messages and analysis-run cards in one thread.
 * `limit` caps how many of the latest messages render (the dock shows a few; the workspace a long
 * tail). Older ones stay in the store.
 */
export function Thread({
  limit,
  selectedRun,
  empty,
  className,
}: {
  limit: number
  selectedRun?: string | null
  empty: ReactNode
  className?: string
}) {
  const { messages, busy } = useChat()
  const shown = messages.slice(-limit)
  const hidden = messages.length - shown.length
  const lastId = messages.at(-1)?.id

  return (
    <div className={cn("min-h-0 flex-1", className)}>
      <MessageScrollerProvider>
        <MessageScroller>
          <MessageScrollerViewport>
            <MessageScrollerContent className="mx-auto w-full max-w-3xl gap-5 p-4">
              {messages.length === 0 ? (
                <MessageScrollerItem className="flex flex-1 flex-col">
                  {empty}
                </MessageScrollerItem>
              ) : null}
              {hidden > 0 ? (
                <MessageScrollerItem>
                  <Marker>
                    <MarkerContent className="text-muted-foreground">
                      {hidden} earlier {hidden === 1 ? "message" : "messages"}{" "}
                      not shown
                    </MarkerContent>
                  </Marker>
                </MessageScrollerItem>
              ) : null}
              {shown.map((m) => (
                <MessageScrollerItem
                  key={m.id}
                  messageId={m.id}
                  scrollAnchor={m.role === "user"}
                >
                  {m.role === "run" ? (
                    <RunCard message={m} selected={m.runId === selectedRun} />
                  ) : (
                    <TextRow message={m} streaming={busy && m.id === lastId} />
                  )}
                </MessageScrollerItem>
              ))}
            </MessageScrollerContent>
          </MessageScrollerViewport>
          <MessageScrollerButton />
        </MessageScroller>
      </MessageScrollerProvider>
    </div>
  )
}

// Memoised: a streaming delta re-renders only the message it lands in.
const TextRow = memo(function TextRow({
  message: m,
  streaming,
}: {
  message: TextMessage
  streaming: boolean
}) {
  return (
    <Message align={m.role === "user" ? "end" : "start"}>
      <MessageContent>
        {m.parts?.length ? <PartsList parts={m.parts} /> : null}
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
        ) : streaming ? (
          <Marker role="status">
            <MarkerIcon>
              <Spinner />
            </MarkerIcon>
            <MarkerContent>Thinking…</MarkerContent>
          </Marker>
        ) : !m.error && !m.parts?.length ? (
          <Marker>
            <MarkerContent className="text-muted-foreground">
              Reply stopped before any text arrived.
            </MarkerContent>
          </Marker>
        ) : null}
        {m.error ? (
          <Bubble variant="destructive">
            <BubbleContent>{m.error}</BubbleContent>
          </Bubble>
        ) : null}
      </MessageContent>
    </Message>
  )
})

function RunCard({
  message,
  selected,
}: {
  message: RunMessage
  selected: boolean
}) {
  const { list } = useAnalyses()
  const summary = list?.find((s) => s.id === message.runId)
  const started = new Date(summary?.started_at ?? message.ts)
  return (
    <button
      type="button"
      onClick={() => navigate("assistant", { run: message.runId })}
      aria-current={selected || undefined}
      className={cn(
        "group/run flex w-full max-w-md items-center gap-3 rounded-xl border bg-card p-3 text-left transition-[background-color,border-color] outline-none hover:bg-muted/40 focus-visible:ring-3 focus-visible:ring-ring/50",
        selected && "border-primary/50 bg-primary/5"
      )}
    >
      <span
        className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-muted text-primary"
        aria-hidden
      >
        <WorkflowIcon className="size-4" />
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="text-sm font-medium">
          Full analysis of{" "}
          <span className="font-mono">{summary?.symbol ?? message.symbol}</span>
        </span>
        <span className="text-xs text-muted-foreground tabular-nums">
          Started{" "}
          {started.toLocaleString(undefined, {
            dateStyle: "medium",
            timeStyle: "short",
          })}
        </span>
        {(summary?.status ?? "running") === "running" ? (
          <RunProgress id={message.runId} />
        ) : null}
      </span>
      <RunStatus
        status={summary?.status ?? "running"}
        rating={summary?.rating}
      />
      <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground transition-transform group-hover/run:translate-x-0.5" />
    </button>
  )
}
