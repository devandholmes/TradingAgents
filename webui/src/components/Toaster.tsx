import { memo, useEffect, useRef, useState } from "react"
import {
  CircleCheckIcon,
  CircleXIcon,
  InfoIcon,
  TriangleAlertIcon,
  XIcon,
} from "lucide-react"

import { useBinanceAccount } from "@/lib/account"
import { dismissToast, useToasts, type Toast } from "@/lib/toast"
import { cn } from "@/lib/utils"

const ICON = {
  success: CircleCheckIcon,
  error: CircleXIcon,
  warn: TriangleAlertIcon,
  info: InfoIcon,
}
const TONE = {
  success: "text-up",
  error: "text-down",
  warn: "text-warn",
  info: "text-info",
}
/** Errors stay longer: they usually need reading. */
const LIFETIME_MS = { success: 5000, info: 5000, warn: 7000, error: 9000 }

/** Toast stack (bottom-right; bottom on phones). Also keeps the account stream open so order
 * events toast on every tab. */
export function Toaster() {
  const toasts = useToasts()
  return (
    <>
      <AccountFeed />
      <ol
        aria-label="Notifications"
        aria-live="polite"
        className="pointer-events-none fixed inset-x-0 bottom-0 z-50 flex flex-col items-end gap-2 p-3 sm:inset-x-auto sm:right-0 sm:w-96"
      >
        {toasts.map((t) => (
          <ToastItem key={t.id} toast={t} />
        ))}
      </ol>
    </>
  )
}

/** Subscribes to the shared /ws/account socket (lib/account.ts raises the order toasts). */
function AccountFeed() {
  useBinanceAccount()
  return null
}

const ToastItem = memo(function ToastItem({ toast: t }: { toast: Toast }) {
  const [leaving, setLeaving] = useState(false)
  const [paused, setPaused] = useState(false)
  const remaining = useRef(LIFETIME_MS[t.kind])

  useEffect(() => {
    if (paused || leaving) return
    const start = Date.now()
    const timer = setTimeout(() => setLeaving(true), remaining.current)
    return () => {
      clearTimeout(timer)
      remaining.current -= Date.now() - start
    }
  }, [paused, leaving])

  const Icon = ICON[t.kind]
  return (
    <li
      role={t.kind === "error" ? "alert" : undefined}
      data-leaving={leaving || undefined}
      onAnimationEnd={leaving ? () => dismissToast(t.id) : undefined}
      onPointerEnter={() => setPaused(true)}
      onPointerLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
      className="toast pointer-events-auto flex w-full items-start gap-3 rounded-lg bg-popover py-3 pr-2 pl-3 text-sm text-popover-foreground shadow-lg ring-1 ring-foreground/10"
    >
      <Icon
        aria-hidden
        className={cn("mt-0.5 size-4 shrink-0", TONE[t.kind])}
      />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <p className="font-medium text-pretty">{t.title}</p>
        {t.body ? (
          <p className="text-xs/relaxed text-pretty text-muted-foreground tabular-nums">
            {t.body}
          </p>
        ) : null}
      </div>
      <button
        type="button"
        aria-label="Dismiss notification"
        onClick={() => setLeaving(true)}
        className="-my-1 grid size-8 shrink-0 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
      >
        <XIcon className="size-4" />
      </button>
    </li>
  )
})
