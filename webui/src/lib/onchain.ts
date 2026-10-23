import { useEffect, useState } from "react"
import {
  createClient,
  fallback,
  formatUnits,
  http,
  parseAbi,
  type Address,
  type Chain,
} from "viem"
import { multicall } from "viem/actions"
import { arbitrum, base, bsc, mainnet } from "viem/chains"

import type { ChainKey, Wallet } from "@/lib/wallets"

// --- curated chain + token list (addresses and decimals verified on-chain) ---

/** Multicall3 is deployed at the same address on every supported chain. */
const MULTICALL3: Address = "0xcA11bde05977b3631167028862bE2a173976CA11"

/** `native` tokens are read via Multicall3.getEthBalance, everything else via ERC-20 balanceOf. */
type Token = {
  symbol: string
  address: Address
  decimals: number
  /** Binance base asset used for pricing (`USDT` = $1); null = no reliable price. */
  base: string | null
  native?: true
}

type ChainConfig = {
  label: string
  chain: Chain
  rpcs: string[]
  tokens: Token[]
}

const native = (symbol: string): Token => ({
  symbol,
  address: MULTICALL3,
  decimals: 18,
  base: symbol,
  native: true,
})
const token = (
  symbol: string,
  address: Address,
  decimals: number,
  base: string | null
): Token => ({ symbol, address, decimals, base })

export const CHAINS: Record<ChainKey, ChainConfig> = {
  ethereum: {
    label: "Ethereum",
    chain: mainnet,
    rpcs: [
      "https://ethereum-rpc.publicnode.com",
      "https://eth.drpc.org",
      "https://eth.llamarpc.com",
    ],
    tokens: [
      native("ETH"),
      token("USDT", "0xdAC17F958D2ee523a2206206994597C13D831ec7", 6, "USDT"),
      token("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6, "USDT"),
      token("DAI", "0x6B175474E89094C44Da98b954EedeAC495271d0F", 18, "USDT"),
      token("WBTC", "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", 8, "BTC"),
      token("WETH", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", 18, "ETH"),
      token("LINK", "0x514910771AF9Ca656af840dff83E8264EcF986CA", 18, "LINK"),
      token("UNI", "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984", 18, "UNI"),
      token("PEPE", "0x6982508145454Ce325dDbE47a25d4ec3d2311933", 18, "PEPE"),
    ],
  },
  bsc: {
    label: "BNB Chain",
    chain: bsc,
    rpcs: [
      "https://bsc-rpc.publicnode.com",
      "https://bsc-dataseed.bnbchain.org",
      "https://bsc.drpc.org",
    ],
    tokens: [
      native("BNB"),
      token("USDT", "0x55d398326f99059fF775485246999027B3197955", 18, "USDT"),
      token("USDC", "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", 18, "USDT"),
      token("FDUSD", "0xc5f0f7b66764F6ec8C8Dff7BA683102295E16409", 18, "USDT"),
      token("BTCB", "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c", 18, "BTC"),
      token("ETH", "0x2170Ed0880ac9A755fd29B2688956BD959F933F8", 18, "ETH"),
      token("WBNB", "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", 18, "BNB"),
      token("CAKE", "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82", 18, "CAKE"),
      token("XRP", "0x1D2F0da169ceB9fC7B3144628dB156f3F6c60dBE", 18, "XRP"),
    ],
  },
  base: {
    label: "Base",
    chain: base,
    rpcs: [
      "https://base-rpc.publicnode.com",
      "https://mainnet.base.org",
      "https://base.drpc.org",
    ],
    tokens: [
      native("ETH"),
      token("USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6, "USDT"),
      token("USDbC", "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA", 6, "USDT"),
      token("USDT", "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2", 6, "USDT"),
      token("DAI", "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb", 18, "USDT"),
      token("WETH", "0x4200000000000000000000000000000000000006", 18, "ETH"),
      token("cbBTC", "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", 8, "BTC"),
      token("cbETH", "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22", 18, null),
      token("AERO", "0x940181a94A35A4569E4529A3CDfB74e38FD98631", 18, null),
    ],
  },
  arbitrum: {
    label: "Arbitrum",
    chain: arbitrum,
    rpcs: [
      "https://arbitrum-one-rpc.publicnode.com",
      "https://arb1.arbitrum.io/rpc",
      "https://arbitrum.drpc.org",
    ],
    tokens: [
      native("ETH"),
      token("USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6, "USDT"),
      token("USDC.e", "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8", 6, "USDT"),
      token("USDT", "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", 6, "USDT"),
      token("DAI", "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1", 18, "USDT"),
      token("WBTC", "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f", 8, "BTC"),
      token("WETH", "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", 18, "ETH"),
      token("ARB", "0x912CE59144191C1204E64559FE8253a0e49E6548", 18, "ARB"),
      token("LINK", "0xf97f4df75117a78c1A5a0DBb814Af92458539FB4", 18, "LINK"),
    ],
  },
}

export const CHAIN_KEYS = Object.keys(CHAINS) as ChainKey[]

// Both reads share one signature, so a single ABI covers the whole multicall.
const BALANCE_ABI = parseAbi([
  "function balanceOf(address) view returns (uint256)",
  "function getEthBalance(address) view returns (uint256)",
])

export type OnchainHolding = {
  chain: ChainKey
  address: Address
  /** Token symbol as it appears on-chain (WETH, BTCB, USDC.e…). */
  asset: string
  /** Binance base asset used for pricing (`USDT` = $1), null when unpriced. */
  base: string | null
  amount: number
}

/** One Multicall3 eth_call for every (address × token) pair on one chain; zero balances dropped. */
export async function fetchChainHoldings(
  key: ChainKey,
  addresses: Address[],
  signal?: AbortSignal
): Promise<OnchainHolding[]> {
  const { chain, rpcs, tokens } = CHAINS[key]
  // A client per refresh lets the AbortSignal cancel in-flight requests; clients are cheap.
  const client = createClient({
    chain,
    transport: fallback(
      rpcs.map((url) =>
        http(url, { retryCount: 0, timeout: 10_000, fetchOptions: { signal } })
      )
    ),
  })
  const pairs = addresses.flatMap((address) =>
    tokens.map((token) => ({ address, token }))
  )
  const results = await multicall(client, {
    allowFailure: true,
    batchSize: 0, // never split: exactly one eth_call per chain
    multicallAddress: MULTICALL3,
    contracts: pairs.map(({ address, token }) => ({
      address: token.address,
      abi: BALANCE_ABI,
      functionName: token.native ? "getEthBalance" : "balanceOf",
      args: [address],
    })),
  })
  // allowFailure also swallows transport errors (RPC down, aborted): surface those instead of "no balances".
  if (results.length && results.every((r) => r.status === "failure"))
    throw results[0].error
  const holdings: OnchainHolding[] = []
  results.forEach((r, i) => {
    if (r.status !== "success" || r.result === 0n) return
    const { address, token } = pairs[i]
    holdings.push({
      chain: key,
      address,
      asset: token.symbol,
      base: token.base,
      amount: Number(formatUnits(r.result, token.decimals)),
    })
  })
  return holdings
}

function describe(err: unknown): string {
  if (err && typeof err === "object" && "shortMessage" in err)
    return String(err.shortMessage)
  return err instanceof Error ? err.message : String(err)
}

const REFRESH_MS = 60_000
const EMPTY: OnchainHolding[] = []

type State = {
  key: string
  tick: number
  holdings: OnchainHolding[]
  error?: string
}

/** Read-only balances for `wallets` on all chains in parallel; refreshes every 60s and on `refresh()`. */
export function useOnchainBalances(wallets: Wallet[]): {
  holdings: OnchainHolding[]
  loading: boolean
  error?: string
  refresh(): void
} {
  // Primitive key: unique lowercase addresses, so a new array identity alone never refetches.
  const key = [...new Set(wallets.map((w) => w.address.toLowerCase()))]
    .sort()
    .join(",")
  const [tick, setTick] = useState(0)
  const [state, setState] = useState<State>({
    key: "",
    tick: -1,
    holdings: EMPTY,
  })

  useEffect(() => {
    if (!key) return
    const ctrl = new AbortController()
    const addresses = key.split(",") as Address[]
    Promise.allSettled(
      CHAIN_KEYS.map((c) => fetchChainHoldings(c, addresses, ctrl.signal))
    ).then((settled) => {
      if (ctrl.signal.aborted) return
      setState((prev) => {
        const holdings: OnchainHolding[] = []
        const failed: string[] = []
        settled.forEach((s, i) => {
          if (s.status === "fulfilled") holdings.push(...s.value)
          else {
            failed.push(`${CHAINS[CHAIN_KEYS[i]].label}: ${describe(s.reason)}`)
            // Keep the last good balances for a chain whose RPCs hiccuped, so totals don't jump.
            if (prev.key === key)
              holdings.push(
                ...prev.holdings.filter((h) => h.chain === CHAIN_KEYS[i])
              )
          }
        })
        return {
          key,
          tick,
          holdings,
          error: failed.length ? failed.join(" · ") : undefined,
        }
      })
    })
    const id = setTimeout(() => setTick((t) => t + 1), REFRESH_MS)
    return () => {
      ctrl.abort()
      clearTimeout(id)
    }
  }, [key, tick])

  const current = state.key === key
  return {
    holdings: key && current ? state.holdings : EMPTY,
    loading: !!key && (!current || state.tick !== tick),
    error: key && current ? state.error : undefined,
    refresh: () => setTick((t) => t + 1),
  }
}
