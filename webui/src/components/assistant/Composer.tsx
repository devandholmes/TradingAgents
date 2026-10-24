import { useEffect, useRef, useState } from "react"
import { ArrowUpIcon, PencilIcon, PlayIcon, SquareIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupTextarea,
} from "@/components/ui/input-group"
import { Spinner } from "@/components/ui/spinner"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import {
  ANALYSTS,
  cleanError,
  startAnalysis,
  type AnalystKey,
} from "@/lib/analysis"
import {
  addRunMessage,
  sendChat,
  setChatContext,
  setDraft,
  stopChat,
  useChat,
} from "@/lib/chat"
import { navigate, registerChatInput } from "@/lib/nav"
import { toast } from "@/lib/toast"
import { useTradingStatus } from "@/lib/tradingStatus"
import { cn } from "@/lib/utils"

import { preloadMarkdown } from "./parts"

// Fundamentals are skipped for crypto by the pipeline (cli/prompts.py), so they are not offered.
const CRYPTO_ANALYSTS = ANALYSTS.filter((a) => a.key !== "fundamentals")
const DEFAULT_ANALYSTS: AnalystKey[] = CRYPTO_ANALYSTS.map((a) => a.key)
const SYMBOL = /^[A-Z0-9]{2,20}$/

/** Start a run, add its card to the conversation and open it in the Assistant tab. */
export function useRunStarter() {
  const [pending, setPending] = useState(false)
  async function run(symbol: string, analysts: AnalystKey[]) {
    if (pending) return
    setPending(true)
    try {
      const summary = await startAnalysis({ symbol, analysts })
      addRunMessage(summary.id, summary.symbol ?? symbol)
      navigate("assistant", { run: summary.id })
    } catch (err) {
      toast({
        kind: "error",
        title: `Could not start the analysis of ${symbol}`,
        body: cleanError(err),
      })
    } finally {
      setPending(false)
    }
  }
  return { run, pending }
}

export function Composer({ variant }: { variant: "dock" | "workspace" }) {
  const { draft, busy, context, focusTick } = useChat()
  const { refresh } = useTradingStatus()
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const [analysts, setAnalysts] = useState<AnalystKey[]>(DEFAULT_ANALYSTS)
  const starter = useRunStarter()
  const workspace = variant === "workspace"

  useEffect(() => registerChatInput(inputRef.current), [])

  // A prefill ("Ask the assistant to act on this") asks the composer to take focus.
  useEffect(() => {
    if (!focusTick) return
    const el = inputRef.current
    if (!el) return
    el.focus()
    el.setSelectionRange(el.value.length, el.value.length)
  }, [focusTick])

  const send = () => {
    preloadMarkdown() // warm the chunk while the model thinks
    void sendChat(draft, refresh)
  }

  return (
    <form
      className={cn(
        "flex flex-col gap-2",
        workspace ? "mx-auto w-full max-w-3xl px-4 pb-4" : "border-t p-3"
      )}
      onSubmit={(e) => {
        e.preventDefault()
        send()
      }}
    >
      <InputGroup className={workspace ? "bg-card" : undefined}>
        <InputGroupTextarea
          ref={inputRef}
          data-chat-input
          aria-label="Message the assistant"
          aria-keyshortcuts="/"
          placeholder={
            workspace
              ? `Ask about ${context.symbol}, your account or a run…`
              : `Ask about ${context.symbol}…`
          }
          className={workspace ? "max-h-48 min-h-14" : "max-h-32 min-h-10"}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (
              e.key === "Enter" &&
              !e.shiftKey &&
              !e.nativeEvent.isComposing
            ) {
              e.preventDefault()
              send()
            }
          }}
        />
        <InputGroupAddon align="block-end" className="gap-2 p-2">
          {workspace ? (
            <SymbolChip
              symbol={context.symbol}
              onChange={(symbol) => setChatContext({ ...context, symbol })}
            />
          ) : (
            <span className="font-mono text-xs text-muted-foreground">
              {context.symbol} · {context.interval}
            </span>
          )}
          {busy ? (
            <InputGroupButton
              size="icon-sm"
              type="button"
              className="ml-auto"
              onClick={stopChat}
            >
              <SquareIcon />
              <span className="sr-only">Stop reply</span>
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
      <div className="flex flex-wrap items-center gap-2">
        {workspace ? (
          <ToggleGroup
            type="multiple"
            variant="outline"
            size="sm"
            spacing={0}
            value={analysts}
            onValueChange={(v) => v.length && setAnalysts(v as AnalystKey[])}
            aria-label="Analysts for the full analysis"
          >
            {CRYPTO_ANALYSTS.map((a) => (
              <ToggleGroupItem
                key={a.key}
                value={a.key}
                className="px-2.5 text-muted-foreground data-[state=on]:bg-primary/12 data-[state=on]:text-primary"
              >
                {a.label}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        ) : null}
        <Button
          type="button"
          size="sm"
          variant={workspace ? "secondary" : "outline"}
          className={workspace ? "ml-auto" : "w-full"}
          disabled={starter.pending}
          onClick={() =>
            void starter.run(
              context.symbol,
              workspace ? analysts : DEFAULT_ANALYSTS
            )
          }
        >
          {starter.pending ? (
            <Spinner data-icon="inline-start" />
          ) : (
            <PlayIcon data-icon="inline-start" />
          )}
          {workspace ? "Run full analysis" : `Analyze ${context.symbol}`}
        </Button>
      </div>
    </form>
  )
}

function SymbolChip({
  symbol,
  onChange,
}: {
  symbol: string
  onChange: (symbol: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(symbol)
  const valid = SYMBOL.test(value)

  if (!editing)
    return (
      <Button
        type="button"
        variant="outline"
        size="xs"
        className="font-mono"
        aria-label={`Symbol ${symbol}. Change symbol`}
        onClick={() => {
          setValue(symbol)
          setEditing(true)
        }}
      >
        {symbol}
        <PencilIcon data-icon="inline-end" />
      </Button>
    )

  const commit = () => {
    if (valid) onChange(value)
    setEditing(false)
  }
  return (
    <input
      autoFocus
      aria-label="Symbol, for example BTCUSDT"
      aria-invalid={!valid}
      value={value}
      maxLength={20}
      spellCheck={false}
      className="h-6 w-28 rounded-md border bg-background px-2 font-mono text-xs uppercase outline-none focus-visible:ring-3 focus-visible:ring-ring/50 aria-invalid:border-destructive"
      onChange={(e) => setValue(e.target.value.toUpperCase().trim())}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault()
          commit()
        } else if (e.key === "Escape") {
          e.stopPropagation()
          setEditing(false)
        }
      }}
    />
  )
}
