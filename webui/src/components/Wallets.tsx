import { memo, useEffect, useId, useState, type ReactNode } from "react"
import { getAddress, isAddress } from "viem"
import {
  CheckIcon,
  EyeIcon,
  PencilIcon,
  PlugIcon,
  PlusIcon,
  Trash2Icon,
  WalletIcon,
  XIcon,
} from "lucide-react"

import { TradingControl } from "@/components/TradingControl"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { Input } from "@/components/ui/input"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
} from "@/components/ui/input-group"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { useTradingStatus } from "@/lib/tradingStatus"
import { hasWallet, useWallets, type Wallet } from "@/lib/wallets"
import { cn } from "@/lib/utils"

const HEX_ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/
const usd = (n: number) =>
  `${n.toLocaleString("en-US", { maximumFractionDigits: 2 })} USDT`
const short = (a: string) => `${a.slice(0, 6)}…${a.slice(-4)}`

export default function Wallets() {
  return (
    <div className="mx-auto grid w-full max-w-6xl gap-2 p-2 lg:grid-cols-2">
      <BinanceAccountCard />
      <div className="flex flex-col gap-2">
        <BrowserWalletsCard />
        <WatchAddressCard />
      </div>
      <WalletListCard />
    </div>
  )
}

// --- Binance account ---

function BinanceAccountCard() {
  const { status, error } = useTradingStatus()
  const rows: [string, ReactNode][] = status
    ? [
        ["API keys", status.configured ? "Configured" : "Not configured"],
        ["Can trade", status.can_trade ? "Yes" : "No"],
        ["Max per order", usd(status.limits.max_order_usdt)],
        [
          "Used today",
          `${usd(status.used_today_usdt)} of ${usd(status.limits.daily_cap_usdt)}`,
        ],
      ]
    : []
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle>
          <h2>Binance Spot account</h2>
        </CardTitle>
        <CardDescription>
          API keys stay on the server. Agents trade only within these limits.
        </CardDescription>
        <CardAction>
          <TradingControl />
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {!status && error ? (
          <Alert variant="destructive">
            <AlertDescription>
              Trading API unavailable: {error}
            </AlertDescription>
          </Alert>
        ) : null}
        {!status && !error ? <Skeleton className="h-24 w-full" /> : null}
        {status ? (
          <>
            {status.configured ? null : (
              <Alert>
                <AlertDescription>
                  Add{" "}
                  <code className="font-mono text-xs">
                    {status.mode === "live"
                      ? "BINANCE_API_KEY / BINANCE_API_SECRET"
                      : "BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET"}
                  </code>{" "}
                  to <code className="font-mono text-xs">.env</code> and restart
                  the server.
                </AlertDescription>
              </Alert>
            )}
            <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-1.5">
              {rows.map(([k, v]) => (
                <div key={k} className="contents">
                  <dt className="text-muted-foreground">{k}</dt>
                  <dd className="font-medium tabular-nums">{v}</dd>
                </div>
              ))}
              <dt className="text-muted-foreground">Symbols</dt>
              <dd className="flex flex-wrap gap-1">
                {status.limits.allowed_symbols.map((s) => (
                  <Badge key={s} variant="outline" className="font-mono">
                    {s}
                  </Badge>
                ))}
              </dd>
            </dl>
          </>
        ) : null}
      </CardContent>
    </Card>
  )
}

// --- Browser wallets (EIP-6963 discovery; eth_requestAccounts only) ---

type Eip1193Provider = {
  request(args: { method: string; params?: unknown[] }): Promise<unknown>
}
type ProviderDetail = {
  info: { uuid: string; name: string; icon: string; rdns: string }
  provider: Eip1193Provider
}

function useInjectedWallets(): ProviderDetail[] {
  const [found, setFound] = useState<ProviderDetail[]>([])
  useEffect(() => {
    const onAnnounce = (e: Event) => {
      const d = (e as CustomEvent<ProviderDetail>).detail
      if (
        typeof d?.info?.uuid !== "string" ||
        typeof d.info.name !== "string" ||
        typeof d.provider?.request !== "function"
      )
        return
      setFound((list) =>
        list.some((x) => x.info.uuid === d.info.uuid) ? list : [...list, d]
      )
    }
    window.addEventListener("eip6963:announceProvider", onAnnounce)
    window.dispatchEvent(new Event("eip6963:requestProvider"))
    return () =>
      window.removeEventListener("eip6963:announceProvider", onAnnounce)
  }, [])
  return found
}

function BrowserWalletsCard() {
  const providers = useInjectedWallets()
  const { add } = useWallets()
  const [busy, setBusy] = useState<string | null>(null)
  const [note, setNote] = useState<{ error: boolean; text: string } | null>(
    null
  )

  async function connect(d: ProviderDetail) {
    setBusy(d.info.uuid)
    setNote(null)
    try {
      const accounts = await d.provider.request({
        method: "eth_requestAccounts",
      })
      const addresses = (Array.isArray(accounts) ? accounts : [])
        .filter(
          (a): a is string => typeof a === "string" && HEX_ADDRESS_RE.test(a)
        )
        .map((a) => getAddress(a))
      if (!addresses.length) throw new Error("The wallet returned no accounts.")
      const fresh = addresses.filter((a) => !hasWallet(a))
      for (const address of fresh)
        add({
          address,
          label: `${d.info.name} ${short(address)}`,
          kind: "browser",
          provider: d.info.name,
        })
      setNote({
        error: false,
        text: fresh.length
          ? `Added ${fresh.length} account${fresh.length > 1 ? "s" : ""} from ${d.info.name}.`
          : `${d.info.name} accounts are already in your list.`,
      })
    } catch (err) {
      const e = err as { code?: number; message?: string }
      setNote({
        error: true,
        text:
          e.code === 4001
            ? "Request rejected in the wallet."
            : (e.message ?? String(err)),
      })
    } finally {
      setBusy(null)
    }
  }

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle>
          <h2>Browser wallet</h2>
        </CardTitle>
        <CardDescription>
          Read-only: shares your address, never signs or sends transactions.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {providers.length ? (
          <ul className="flex flex-col gap-1">
            {providers.map((d) => (
              <li key={d.info.uuid} className="flex items-center gap-2">
                {d.info.icon?.startsWith("data:image/") ? (
                  <img src={d.info.icon} alt="" className="size-5 rounded" />
                ) : (
                  <WalletIcon className="size-5 text-muted-foreground" />
                )}
                <span className="flex-1 truncate">{d.info.name}</span>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy !== null}
                  onClick={() => void connect(d)}
                >
                  {busy === d.info.uuid ? (
                    <Spinner data-icon="inline-start" />
                  ) : (
                    <PlugIcon data-icon="inline-start" />
                  )}
                  Connect
                </Button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-muted-foreground">
            No browser wallet detected. Install one (e.g. MetaMask, Rabby) or
            add an address below as watch-only.
          </p>
        )}
        {note ? (
          <p
            role={note.error ? "alert" : "status"}
            className={cn(
              "text-xs",
              note.error ? "text-destructive" : "text-muted-foreground"
            )}
          >
            {note.text}
          </p>
        ) : null}
      </CardContent>
    </Card>
  )
}

// --- Watch-only address ---

function addressError(value: string): string | null {
  if (!value) return null
  if (!HEX_ADDRESS_RE.test(value))
    return "Not an EVM address (0x followed by 40 hex characters)."
  // Mixed case must match the EIP-55 checksum; all-lower/upper is accepted as-is.
  if (!isAddress(value))
    return "Checksum mismatch. Check the address for typos."
  if (hasWallet(value)) return "Already in your list."
  return null
}

function WatchAddressCard() {
  const { add } = useWallets()
  const [address, setAddress] = useState("")
  const [label, setLabel] = useState("")
  const errorId = useId()
  const value = address.trim()
  const error = addressError(value)
  const valid = value !== "" && error === null

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle>
          <h2>Watch-only address</h2>
        </CardTitle>
        <CardDescription>
          Track any address on Ethereum, BNB Chain, Base and Arbitrum.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            if (!valid) return
            const checksummed = getAddress(value)
            add({
              address: checksummed,
              label: label.trim() || `Watch ${short(checksummed)}`,
              kind: "watch",
            })
            setAddress("")
            setLabel("")
          }}
        >
          <Input
            aria-label="Label (optional)"
            placeholder="Label (optional)"
            value={label}
            maxLength={40}
            onChange={(e) => setLabel(e.target.value)}
          />
          <InputGroup>
            <InputGroupInput
              aria-label="Address"
              placeholder="0x…"
              spellCheck={false}
              autoComplete="off"
              value={address}
              onChange={(e) => setAddress(e.target.value)}
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? errorId : undefined}
              className="font-mono"
            />
            <InputGroupAddon align="inline-end">
              <InputGroupButton type="submit" size="xs" disabled={!valid}>
                <PlusIcon data-icon="inline-start" />
                Add
              </InputGroupButton>
            </InputGroupAddon>
          </InputGroup>
          {error ? (
            <p id={errorId} className="text-xs text-destructive">
              {error}
            </p>
          ) : null}
        </form>
      </CardContent>
    </Card>
  )
}

// --- Saved wallets ---

function WalletListCard() {
  const { wallets, rename, remove } = useWallets()
  return (
    <Card size="sm" className="lg:col-span-2">
      <CardHeader>
        <CardTitle>
          <h2>Your wallets</h2>
        </CardTitle>
        <CardDescription>
          Stored in this browser only. Balances appear on the Portfolio tab.
        </CardDescription>
      </CardHeader>
      <CardContent className="px-0">
        {wallets.length ? (
          <ul className="flex flex-col divide-y">
            {wallets.map((w) => (
              <WalletRow
                key={w.id}
                wallet={w}
                onRename={rename}
                onRemove={remove}
              />
            ))}
          </ul>
        ) : (
          <Empty>
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <WalletIcon />
              </EmptyMedia>
              <EmptyTitle>No wallets yet</EmptyTitle>
              <EmptyDescription>
                Connect a browser wallet or add a watch-only address above.
              </EmptyDescription>
            </EmptyHeader>
          </Empty>
        )}
      </CardContent>
    </Card>
  )
}

const WalletRow = memo(function WalletRow({
  wallet,
  onRename,
  onRemove,
}: {
  wallet: Wallet
  onRename: (id: string, label: string) => void
  onRemove: (id: string) => void
}) {
  const [draft, setDraft] = useState<string | null>(null)
  const save = () => {
    const label = draft?.trim()
    if (label && label !== wallet.label) onRename(wallet.id, label)
    setDraft(null)
  }

  return (
    <li className="flex items-center gap-3 px-(--card-spacing) py-2">
      {wallet.kind === "browser" ? (
        <WalletIcon className="size-4 shrink-0 text-muted-foreground" />
      ) : (
        <EyeIcon className="size-4 shrink-0 text-muted-foreground" />
      )}
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        {draft === null ? (
          <span className="truncate font-medium">{wallet.label}</span>
        ) : (
          <Input
            autoFocus
            aria-label="Wallet label"
            value={draft}
            maxLength={40}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={save}
            onKeyDown={(e) => {
              if (e.key === "Enter") save()
              else if (e.key === "Escape") setDraft(null)
            }}
            className="h-7"
          />
        )}
        <span
          className="truncate font-mono text-xs text-muted-foreground"
          title={wallet.address}
        >
          {wallet.address}
        </span>
      </div>
      <Badge variant="secondary">
        {wallet.kind === "browser"
          ? (wallet.provider ?? "Browser")
          : "Watch-only"}
      </Badge>
      {draft === null ? (
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={`Rename ${wallet.label}`}
          onClick={() => setDraft(wallet.label)}
        >
          <PencilIcon />
        </Button>
      ) : (
        <>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Save label"
            // Keep focus in the input so its onBlur doesn't fire first.
            onMouseDown={(e) => e.preventDefault()}
            onClick={save}
          >
            <CheckIcon />
          </Button>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Cancel rename"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => setDraft(null)}
          >
            <XIcon />
          </Button>
        </>
      )}
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label={`Remove ${wallet.label}`}
        onClick={() => onRemove(wallet.id)}
      >
        <Trash2Icon />
      </Button>
    </li>
  )
})
