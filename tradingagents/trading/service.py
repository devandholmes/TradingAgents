"""TradingService: the one entry point for Binance Spot trading, used by the REST routes and the agents.

Created once per app (``running_service`` in the FastAPI lifespan). Mode, keys and
limits come from the environment; live trading happens only with ``TRADING_MODE=live``.
When keys are configured a background task keeps the server-time offset and the
allowed symbols' filters fresh, so placing an order costs one round trip: the order.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx

from tradingagents.default_config import _TRADINGAGENTS_HOME
from tradingagents.trading.client import LIVE_URL, TESTNET_URL, TIMEOUT, BinanceError, SpotClient
from tradingagents.trading.fees import commissions, fee_columns, maker_wait_s, merge, paper_fee
from tradingagents.trading.guard import (
    Guard,
    Limits,
    Mode,
    OrderRequest,
    Source,
    TradeRejected,
    order_params,
)

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 600
FEES_TTL, FEES_RETRY = 3600.0, 60.0   # commission rates change rarely; a failed read is retried sooner
POLL_SECONDS = 1.0                    # order-status poll while a maker order rests
WOULD_TAKE = "immediately match"      # Binance's -2010 text for a LIMIT_MAKER that would trade as taker
OPEN = ("NEW", "PARTIALLY_FILLED")    # order statuses that can still fill


def order_view(raw: dict) -> dict:
    """A Binance order in the API's shape; a filled MARKET order's price is its average fill.

    A placing answer (FULL) carries its fills: ``commission`` is then the fee per asset.
    """
    executed = float(raw.get("executedQty", 0))
    quote = float(raw.get("cummulativeQuoteQty", 0))
    price = float(raw.get("price", 0))
    fee = {"commission": commissions(raw["fills"])} if "fills" in raw else {}
    return {
        "symbol": raw["symbol"],
        "order_id": raw["orderId"],
        "side": raw.get("side"),
        "type": raw.get("type"),
        "price": price or (quote / executed if executed else 0.0),
        "orig_qty": float(raw.get("origQty", 0)),
        "executed_qty": executed,
        "quote_qty": quote,
        "status": raw.get("status"),
        "time": raw.get("time") or raw.get("transactTime") or raw.get("updateTime"),
        **fee,
    }


class TradingService:
    def __init__(self, mode: Mode, client: SpotClient, guard: Guard):
        self.mode = mode
        self.client = client
        self.guard = guard
        # Last price of a symbol from memory, or None; the REST ticker is the fallback.
        self.price_lookup: Callable[[str], float | None] = lambda symbol: None
        # Called after the kill switch changes; the account stream swaps in its status broadcast.
        self.on_status: Callable[[], None] = lambda: None
        self._refresher: asyncio.Task | None = None
        self._fees: tuple[float, dict] | None = None   # (expiry on the monotonic clock, fees block)

    @classmethod
    def from_env(cls, home: Path = Path(_TRADINGAGENTS_HOME)) -> TradingService:
        live = os.getenv("TRADING_MODE", "").strip().lower() == "live"
        prefix = "BINANCE_" if live else "BINANCE_TESTNET_"
        http = httpx.AsyncClient(base_url=LIVE_URL if live else TESTNET_URL, timeout=TIMEOUT)
        client = SpotClient(http, os.getenv(f"{prefix}API_KEY", ""), os.getenv(f"{prefix}API_SECRET", ""))
        mode: Mode = "live" if live else "testnet"
        guard = Guard(mode, Limits.from_env(), home / "trading_state.json", home / "trades.jsonl")
        return cls(mode, client, guard)

    def start(self) -> None:
        if self.client.configured:
            self._refresher = asyncio.create_task(self._refresh(), name="trading-refresh")

    async def close(self) -> None:
        if self._refresher:
            self._refresher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresher
        await self.client.aclose()

    async def _refresh(self) -> None:
        while True:
            try:
                await self.client.sync_time()
                await self.client.load_rules(list(self.guard.limits.allowed_symbols))
            except BinanceError as exc:
                logger.warning("Trading cache refresh failed: %s", exc)
            await asyncio.sleep(REFRESH_SECONDS)

    def _require_keys(self) -> None:
        if not self.client.configured:
            raise TradeRejected("Binance API keys not configured")

    async def status(self) -> dict:
        limits = self.guard.limits
        return {
            "mode": self.mode,
            "enabled": self.guard.enabled,
            "configured": self.client.configured,
            "can_trade": self.guard.enabled and self.client.configured,
            "limits": {
                "max_order_usdt": limits.max_order_usdt,
                "daily_cap_usdt": limits.daily_cap_usdt,
                "allowed_symbols": list(limits.allowed_symbols),
            },
            "used_today_usdt": round(self.guard.used_today, 2),
        }

    async def fees(self) -> dict:
        """Commission rates for the account, cached; the paper rate when Binance cannot say.

        ``maker``/``taker`` add the standard, tax and special rates (all charged per trade).
        ``bnb_discount`` is whether fees are paid in BNB at a discount: null on the testnet
        (no SAPI), without keys, or when Binance does not answer.
        """
        if self._fees and time.monotonic() < self._fees[0]:
            return self._fees[1]
        rate = paper_fee()
        fees, ttl = {"maker": rate, "taker": rate, "bnb_discount": None, "source": "paper"}, FEES_TTL
        if self.client.configured:
            try:
                raw = await self.client.commission_rates(self.guard.limits.allowed_symbols[0])
                kinds = [raw.get(k) or {} for k in ("standardCommission", "taxCommission", "specialCommission")]
                fees |= {side: round(sum(float(k.get(side) or 0) for k in kinds), 8) for side in ("maker", "taker")}
                fees["source"] = "binance"
                if self.mode == "live":
                    burn = (await self.client.bnb_burn()).get("spotBNBBurn")
                    fees["bnb_discount"] = bool(burn) and bool((raw.get("discount") or {}).get("enabledForAccount"))
            except (BinanceError, KeyError, TypeError, ValueError) as exc:
                logger.warning("Commission rates unavailable: %s", exc)
                ttl = FEES_RETRY
        self._fees = (time.monotonic() + ttl, fees)
        return fees

    async def set_enabled(self, enabled: bool) -> dict:
        self.guard.set_enabled(enabled)
        self.on_status()
        return await self.status()

    async def account(self) -> dict:
        self._require_keys()
        raw = await self.client.account()
        balances = [
            {"asset": b["asset"], "free": float(b["free"]), "locked": float(b["locked"])}
            for b in raw["balances"]
        ]
        return {
            "mode": self.mode,
            "balances": [b for b in balances if b["free"] or b["locked"]],
            "update_time": raw.get("updateTime"),
        }

    async def balances(self) -> list[dict]:
        return (await self.account())["balances"]

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        self._require_keys()
        return [order_view(o) for o in await self.client.open_orders(symbol.upper() if symbol else None)]

    async def _last_price(self, symbol: str) -> float:
        return self.price_lookup(symbol) or await self.client.last_price(symbol)

    async def place_order(self, req: OrderRequest, source: Source) -> dict:
        """Guard, round and send ``req``; raises TradeRejected or BinanceError. Every outcome is audited."""
        reserved, sent, client_id = 0.0, False, None
        try:
            self.guard.check_access(req.symbol, self.client.configured)
            if req.quote_amount is not None:
                price, notional = 0.0, req.quote_amount
            else:
                price = req.price or await self._last_price(req.symbol)
                notional = req.quantity * price
            self.guard.reserve(notional)
            reserved = notional
            # The price band uses the cached price only: no extra round trip on the order path.
            reference = self.price_lookup(req.symbol) if req.type != "MARKET" else None
            params = order_params(req, await self.client.symbol_rules(req.symbol), price, reference)
            if req.type != "MARKET":
                # Our own id, so a resting order whose reply never came can still be cancelled.
                params["newClientOrderId"] = client_id = f"ta-{uuid.uuid4().hex}"
            sent = True
            raw = await self.client.new_order(params)
        except TradeRejected as exc:
            self.guard.release(reserved)
            self.guard.record(source, req, "rejected", reason=exc.reason)
            raise
        except Exception as exc:
            # No response to a sent order: it may have executed, so its notional stays counted.
            kept = sent and isinstance(exc, BinanceError) and exc.status is None
            if not kept:
                self.guard.release(reserved)
            elif client_id:
                # It may be resting unseen: cancel it (best effort); any fill stays counted.
                with contextlib.suppress(Exception):
                    await asyncio.shield(self.client.cancel_client_order(req.symbol, client_id))
            self.guard.record(source, req, "error", reason=str(exc) or type(exc).__name__,
                              notional=round(reserved, 8) if kept else None)
            raise
        order = order_view(raw)
        status = "filled" if order["status"] == "FILLED" else "new"
        self.guard.record(source, req, status, notional=round(notional, 8), order=order)
        return order

    async def place_maker_first(self, req: OrderRequest, wait_s: float | None = None,
                                source: Source = "bot", stop: asyncio.Event | None = None) -> dict:
        """Fill ``req`` (a MARKET request) as maker where possible, then finish as taker.

        A post-only LIMIT_MAKER rests at the best bid (BUY) or ask (SELL) from one
        bookTicker read for up to ``wait_s`` (default ``TRADING_MAKER_WAIT_S``); the
        unfilled rest is cancelled and sent as a MARKET order when it is worth at least
        the symbol's minNotional, else left for the next reconcile. Both legs go through
        ``place_order``, so the guard checks each one. A guard rejection of the maker leg
        is raised; once something has filled, a failed taker leg is logged and the result
        returned, so the caller can still book what filled. Setting ``stop`` (a risk exit
        waiting to sell) cancels the resting order at once and skips the taker leg.

        Returns ``{filled_qty, quote, fee, fee_asset, commission, maker_fill_ratio,
        order_ids, remainder_qty}``.
        """
        if req.type != "MARKET":
            raise TradeRejected("maker-first takes a MARKET request")
        self.guard.check_access(req.symbol, self.client.configured)
        rules = await self.client.symbol_rules(req.symbol)
        bid, ask = await self.client.book_ticker(req.symbol)
        price = bid if req.side == "BUY" else ask
        qty = req.quantity or req.quote_amount / price
        maker = OrderRequest(symbol=req.symbol, side=req.side, type="LIMIT_MAKER", quantity=qty, price=price)
        maker_qty = maker_quote = 0.0
        fee: dict[str, float] = {}
        order_ids: list[int] = []
        try:
            placed = await self.place_order(maker, source)
        except BinanceError as exc:
            if WOULD_TAKE not in str(exc):
                raise
            placed = None   # the book moved onto our price: nothing rests, all goes to the taker leg
        if placed:
            order_ids.append(placed["order_id"])
            order = await self._rest(req.symbol, placed, maker_wait_s() if wait_s is None else wait_s, stop)
            qty, maker_qty, maker_quote = order["orig_qty"], order["executed_qty"], order["quote_qty"]
            # The cap counted the whole limit; only what filled stays counted (the taker leg reserves its own).
            self.guard.release((qty - maker_qty) * price)
            if maker_qty:
                try:
                    fee = commissions(await self.client.order_trades(req.symbol, placed["order_id"]))
                except (BinanceError, httpx.HTTPError) as exc:
                    # The fill stands whatever the fee read does: model it in the asset received.
                    logger.warning("Maker-first %s %s: fills not read (%s); fee modelled", req.side, req.symbol, exc)
                    fee = ({rules.base_asset: maker_qty * paper_fee()} if req.side == "BUY"
                           else {rules.quote_asset: maker_quote * paper_fee()})
        remainder = max(0.0, qty - maker_qty)
        taker_qty = taker_quote = 0.0
        if remainder and not (stop and stop.is_set()) and remainder * price >= float(rules.min_notional):
            finish = OrderRequest(symbol=req.symbol, side=req.side, type="MARKET", quantity=remainder)
            try:
                done = await self.place_order(finish, source)
            except (TradeRejected, BinanceError) as exc:
                if not maker_qty:
                    raise
                logger.warning("Maker-first %s %s: taker leg failed, %.8f left: %s",
                               req.side, req.symbol, remainder, exc)
            else:
                order_ids.append(done["order_id"])
                taker_qty, taker_quote = done["executed_qty"], done["quote_qty"]
                fee = merge(fee, done.get("commission", {}))
        filled = maker_qty + taker_qty
        amount, asset = fee_columns(fee)
        return {
            "filled_qty": filled,
            "quote": maker_quote + taker_quote,
            "fee": amount,
            "fee_asset": asset,
            "commission": fee,
            "maker_fill_ratio": maker_qty / filled if filled else 0.0,
            "order_ids": order_ids,
            "remainder_qty": max(0.0, qty - filled),
        }

    async def _rest(self, symbol: str, order: dict, wait_s: float, stop: asyncio.Event | None = None) -> dict:
        """Wait for a resting order to fill, then cancel what is left; the order's final view.

        Never leaves it resting: a failed poll, a failed cancel or a cancelled task cancels
        it once more (best effort) and raises, so no taker leg is sent beside a live order.
        """
        deadline, stop = time.monotonic() + wait_s, stop or asyncio.Event()
        try:
            while order["status"] in OPEN and time.monotonic() < deadline and not stop.is_set():
                with contextlib.suppress(TimeoutError):   # a poll interval, cut short by ``stop``
                    await asyncio.wait_for(stop.wait(), POLL_SECONDS)
                order = order_view(await self.client.get_order(symbol, order["order_id"]))
            if order["status"] in OPEN:
                try:
                    order = order_view(await self.client.cancel_order(symbol, order["order_id"]))
                except BinanceError:
                    # Filled between the last poll and the cancel: read the final state instead.
                    order = order_view(await self.client.get_order(symbol, order["order_id"]))
            if order["status"] in OPEN:
                raise BinanceError(f"maker order {order['order_id']} is still open after its cancel")
        except BaseException:
            with contextlib.suppress(Exception):
                await asyncio.shield(self.client.cancel_order(symbol, order["order_id"]))
            raise
        return order

    async def cancel_order(self, symbol: str, order_id: int, source: Source) -> dict:
        # Cancelling only lowers exposure, so the kill switch does not block it.
        self._require_keys()
        order = order_view(await self.client.cancel_order(symbol.upper(), order_id))
        logger.info("Order %s %s cancelled by %s", symbol, order_id, source)
        return order

    def recent_trades(self, limit: int = 50) -> list[dict]:
        return self.guard.recent(limit)


_service: TradingService | None = None


def get_service() -> TradingService:
    """The app's TradingService; usable as a FastAPI dependency."""
    if _service is None:
        raise RuntimeError("TradingService is not running; it starts with the app's lifespan")
    return _service


@contextlib.asynccontextmanager
async def running_service(service: TradingService | None = None) -> AsyncIterator[TradingService]:
    """Run ``service`` (default: from the environment) as the app's service until exit."""
    global _service
    _service = service or TradingService.from_env()
    _service.start()
    try:
        yield _service
    finally:
        await _service.close()
        _service = None
