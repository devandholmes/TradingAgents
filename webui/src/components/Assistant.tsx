import { useEffect } from "react"
import { ArrowUpRightIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Card, CardAction, CardHeader, CardTitle } from "@/components/ui/card"
import { setChatContext } from "@/lib/chat"
import { navigate } from "@/lib/nav"

import { Composer } from "./assistant/Composer"
import { Thread } from "./assistant/Thread"

/**
 * Dashboard Quick chat: the latest messages of the shared conversation, a composer, and an
 * Analyze button. The same conversation continues in the Assistant tab.
 */
export function Assistant({
  symbol,
  interval,
}: {
  symbol: string
  interval: string
}) {
  // The chart's symbol and interval are what "this" means in a question.
  useEffect(() => setChatContext({ symbol, interval }), [symbol, interval])

  return (
    <Card size="sm" className="min-h-[420px] gap-0 py-0 lg:min-h-0">
      <CardHeader className="border-b py-2.5">
        <CardTitle className="text-title">
          <h2>Quick chat</h2>
        </CardTitle>
        <CardAction>
          <Button
            variant="ghost"
            size="xs"
            onClick={() => navigate("assistant")}
          >
            Open in Assistant
            <ArrowUpRightIcon data-icon="inline-end" />
          </Button>
        </CardAction>
      </CardHeader>
      <Thread
        limit={12}
        empty={
          <p className="m-auto max-w-[32ch] text-center text-xs text-pretty text-muted-foreground">
            Ask about {symbol}, your balances or open orders. The conversation
            continues in the Assistant tab.
          </p>
        }
      />
      <Composer variant="dock" />
    </Card>
  )
}
