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
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx

from tradingagents.default_config import _TRADINGAGENTS_HOME
from tradingagents.trading.client import LIVE_URL, TESTNET_URL, TIMEOUT, BinanceError, SpotClient
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


def order_view(raw: dict) -> dict:
    """A Binance order in the API's shape; a filled MARKET order's price is its average fill."""
    executed = float(raw.get("executedQty", 0))
    quote = float(raw.get("cummulativeQuoteQty", 0))
    price = float(raw.get("price", 0))
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
        reserved, sent = 0.0, False
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
            reference = self.price_lookup(req.symbol) if req.type == "LIMIT" else None
            params = order_params(req, await self.client.symbol_rules(req.symbol), price, reference)
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
            self.guard.record(source, req, "error", reason=str(exc) or type(exc).__name__,
                              notional=round(reserved, 8) if kept else None)
            raise
        order = order_view(raw)
        status = "filled" if order["status"] == "FILLED" else "new"
        self.guard.record(source, req, status, notional=round(notional, 8), order=order)
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
