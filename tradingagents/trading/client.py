"""Async signed client for the Binance Spot REST API: orders, cancels and account reads only.

Withdrawals and transfers are deliberately absent: the client exposes no way to
call them. One ``httpx.AsyncClient`` per base URL is shared by every request.
The server-time offset and the symbol trading rules (exchangeInfo filters) are
cached in memory, so a signed request costs exactly one round trip; a request
Binance rejects with -1021 (timestamp outside recvWindow) is retried once after
re-reading the server time.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from tradingagents.dataflows.errors import VendorUnavailableError

LIVE_URL = "https://api.binance.com"
TESTNET_URL = "https://testnet.binance.vision"

TIMEOUT = 5.0
RECV_WINDOW = 5000
INVALID_TIMESTAMP = -1021

_SIGNATURE = re.compile(r"signature=[0-9a-fA-F]+")


class BinanceError(VendorUnavailableError):
    """Binance refused or failed a request. The message never carries keys or signatures."""

    def __init__(self, message: str, code: int | None = None, status: int | None = None):
        super().__init__(message)
        self.code, self.status = code, status


def sign(query: str, secret: str) -> str:
    """Hex HMAC-SHA256 of the (already percent-encoded) query string."""
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class SymbolRules:
    """The exchangeInfo filters an order has to pass. A zero step, tick or bound is disabled."""

    symbol: str
    step: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_step: Decimal
    market_min_qty: Decimal
    market_max_qty: Decimal
    tick: Decimal
    min_price: Decimal
    max_price: Decimal
    min_notional: Decimal
    min_notional_market: bool
    quote_precision: int
    # Limit-price band around the average price, per side (PERCENT_PRICE_BY_SIDE).
    bid_down: Decimal = Decimal(0)
    bid_up: Decimal = Decimal(0)
    ask_down: Decimal = Decimal(0)
    ask_up: Decimal = Decimal(0)
    base_asset: str = ""
    quote_asset: str = ""

    @classmethod
    def parse(cls, info: dict) -> SymbolRules:
        filters = {f["filterType"]: f for f in info.get("filters", [])}

        def number(kind: str, key: str) -> Decimal:
            return Decimal(filters.get(kind, {}).get(key, "0"))

        step, min_qty, max_qty = (number("LOT_SIZE", k) for k in ("stepSize", "minQty", "maxQty"))
        m_step, m_min, m_max = (number("MARKET_LOT_SIZE", k) for k in ("stepSize", "minQty", "maxQty"))
        # NOTIONAL replaced MIN_NOTIONAL; a symbol carries one or the other.
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        # PERCENT_PRICE_BY_SIDE replaced PERCENT_PRICE, whose one band binds both sides.
        down, up = number("PERCENT_PRICE", "multiplierDown"), number("PERCENT_PRICE", "multiplierUp")
        side = filters.get("PERCENT_PRICE_BY_SIDE", {})
        return cls(
            symbol=info["symbol"],
            step=step,
            min_qty=min_qty,
            max_qty=max_qty,
            # MARKET_LOT_SIZE may be absent or zeroed; LOT_SIZE binds market orders too.
            market_step=m_step or step,
            market_min_qty=max(m_min, min_qty),
            market_max_qty=min(m_max, max_qty) if m_max and max_qty else m_max or max_qty,
            tick=number("PRICE_FILTER", "tickSize"),
            min_price=number("PRICE_FILTER", "minPrice"),
            max_price=number("PRICE_FILTER", "maxPrice"),
            min_notional=Decimal(notional.get("minNotional", "0")),
            min_notional_market=bool(notional.get("applyMinToMarket", notional.get("applyToMarket", True))),
            quote_precision=int(info.get("quoteAssetPrecision", info.get("quotePrecision", 8))),
            bid_down=Decimal(side.get("bidMultiplierDown", down)),
            bid_up=Decimal(side.get("bidMultiplierUp", up)),
            ask_down=Decimal(side.get("askMultiplierDown", down)),
            ask_up=Decimal(side.get("askMultiplierUp", up)),
            base_asset=info.get("baseAsset", ""),
            quote_asset=info.get("quoteAsset", ""),
        )


class SpotClient:
    """Binance Spot REST calls over one shared ``httpx.AsyncClient``."""

    def __init__(self, http: httpx.AsyncClient, api_key: str = "", api_secret: str = ""):
        self._http = http
        self._key, self._secret = api_key, api_secret
        self.time_offset_ms = 0
        self.rules: dict[str, SymbolRules] = {}

    @property
    def configured(self) -> bool:
        return bool(self._key and self._secret)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _redact(self, text: str) -> str:
        text = _SIGNATURE.sub("signature=***", text)
        for secret in (self._key, self._secret):
            if secret:
                text = text.replace(secret, "***")
        return text

    async def _send(self, method: str, path: str, query: str, headers: dict | None) -> object:
        try:
            response = await self._http.request(method, f"{path}?{query}" if query else path, headers=headers)
        except httpx.HTTPError as exc:
            raise BinanceError(self._redact(f"Binance {path} request failed: {type(exc).__name__}: {exc}")) from None
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.is_success and body is not None:
            return body
        if isinstance(body, dict) and "msg" in body:
            msg, code = str(body["msg"]), body.get("code")
        else:
            msg, code = response.text[:200], None
        raise BinanceError(
            self._redact(f"Binance {path} HTTP {response.status_code}: {msg}"), code, response.status_code
        )

    async def public(self, path: str, params: dict | None = None) -> object:
        return await self._send("GET", path, urlencode(params or {}), None)

    async def signed(self, method: str, path: str, params: dict | None = None) -> object:
        if not self.configured:
            raise BinanceError("Binance API keys are not configured")
        for retry in (False, True):
            # Binance signs the percent-encoded query; the exact signed string is what is sent.
            query = urlencode({
                **(params or {}),
                "recvWindow": RECV_WINDOW,
                "timestamp": int(time.time() * 1000) + self.time_offset_ms,
            })
            query += f"&signature={sign(query, self._secret)}"
            try:
                return await self._send(method, path, query, {"X-MBX-APIKEY": self._key})
            except BinanceError as exc:
                # A -1021 rejection placed nothing, so retrying an order is safe.
                if exc.code != INVALID_TIMESTAMP or retry:
                    raise
                await self.sync_time()
        raise AssertionError("unreachable")

    def ws_signed(self, params: dict | None = None) -> dict:
        """Params for a signed WebSocket API request: HMAC of the sorted, unencoded ``key=value`` pairs."""
        if not self.configured:
            raise BinanceError("Binance API keys are not configured")
        params = {
            **(params or {}),
            "apiKey": self._key,
            "recvWindow": RECV_WINDOW,
            "timestamp": int(time.time() * 1000) + self.time_offset_ms,
        }
        payload = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        return {**params, "signature": sign(payload, self._secret)}

    async def sync_time(self) -> None:
        """Cache the offset to Binance's clock, taken at the request's midpoint."""
        sent = time.time() * 1000
        server = (await self.public("/api/v3/time"))["serverTime"]
        self.time_offset_ms = int(server - (sent + time.time() * 1000) / 2)

    async def load_rules(self, symbols: list[str]) -> None:
        params = {"symbols": json.dumps(symbols, separators=(",", ":"))}
        info = await self.public("/api/v3/exchangeInfo", params)
        self.rules.update((s["symbol"], SymbolRules.parse(s)) for s in info["symbols"])

    async def symbol_rules(self, symbol: str) -> SymbolRules:
        """Cached rules; fetched only for a symbol the warm-up did not load."""
        if symbol not in self.rules:
            await self.load_rules([symbol])
        return self.rules[symbol]

    async def last_price(self, symbol: str) -> float:
        return float((await self.public("/api/v3/ticker/price", {"symbol": symbol}))["price"])

    async def book_ticker(self, symbol: str) -> tuple[float, float]:
        """Best bid and best ask."""
        book = await self.public("/api/v3/ticker/bookTicker", {"symbol": symbol})
        return float(book["bidPrice"]), float(book["askPrice"])

    async def account(self) -> dict:
        return await self.signed("GET", "/api/v3/account", {"omitZeroBalances": "true"})

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        return await self.signed("GET", "/api/v3/openOrders", {"symbol": symbol} if symbol else None)

    async def new_order(self, params: dict) -> dict:
        # FULL adds the immediate fills, each with its commission and commission asset.
        return await self.signed("POST", "/api/v3/order", {**params, "newOrderRespType": "FULL"})

    async def get_order(self, symbol: str, order_id: int) -> dict:
        return await self.signed("GET", "/api/v3/order", {"symbol": symbol, "orderId": order_id})

    async def order_trades(self, symbol: str, order_id: int) -> list[dict]:
        """The account's trades of one order, each with its commission (fills that came after the placing)."""
        return await self.signed("GET", "/api/v3/myTrades", {"symbol": symbol, "orderId": order_id})

    async def commission_rates(self, symbol: str) -> dict:
        return await self.signed("GET", "/api/v3/account/commission", {"symbol": symbol})

    async def bnb_burn(self) -> dict:
        """Whether spot fees are paid in BNB. SAPI: live only, the testnet has no /sapi routes."""
        return await self.signed("GET", "/sapi/v1/bnbBurn")

    async def cancel_order(self, symbol: str, order_id: int) -> dict:
        return await self.signed("DELETE", "/api/v3/order", {"symbol": symbol, "orderId": order_id})

    async def cancel_client_order(self, symbol: str, client_id: str) -> dict:
        return await self.signed("DELETE", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_id})
