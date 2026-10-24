"""The strategy bot: follows one strategy instance with the user's own size, through the TradingService guard.

The engine NOTIFYs ``ta_bot_signal`` when the active instance's target exposure
changes; the web process's one LISTEN connection (``tradingagents.web.strategies``)
hands it to ``Bot.submit``. Signals, activation and deactivation run one at a time.

``reconcile`` brings the bot to its instance's current target (``pending_target``,
else ``exposure``) by the same rules: on activation, so the bot enters the strategy's
position at once; whenever the LISTEN connection (re)connects, web startup included,
so a signal missed while it was down still lands; and on demand (``POST /api/bot/sync``),
to retry after a rejected order. It is idempotent: in sync, it trades nothing.

The bot's position is its own, replayed from its ``bot_trades`` rows and never read
from the account, so a SELL never touches the user's other holdings. Exposure is the
cost of the bot's holding over ``size_usdt``: raising it by ``d`` spends ``d * size``;
lowering it from ``e`` to ``t`` sells ``(e - t) / e`` of the holding (the backtest's rule).

Mode is live when the size reaches the symbol's minNotional (the cached exchange
filters). Then a BUY of at least the minimum is a real MARKET order by quote amount,
through ``TradingService.place_order`` (kill switch, per-order limit, daily cap,
allowed symbols); a smaller one is paper-mirrored: filled at the signal price with
the backtest's slippage and fee. A real holding is only ever sold by a real order,
and a rejected real order is recorded as rejected, never replaced by a paper fill.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from tradingagents import db
from tradingagents.strategies import CATALOG
from tradingagents.trading.guard import OrderRequest, TradeRejected
from tradingagents.trading.service import get_service

logger = logging.getLogger(__name__)

FEE = 0.001          # per side, as the backtest
SLIPPAGE = 0.0002    # 2 bps, as the backtest
EPSILON = 1e-9       # a target at or below this is flat
TOLERANCE = 0.005    # exposure gaps up to this count as in sync: rounding and fill dust, never worth an order
TRADES_SHOWN = 50

TRADE_COLUMNS = "id, ts, instance_id, side, quote, qty, price, real, status, order_id, reason"
FILLED = "SELECT instance_id, side, quote, qty, real FROM bot_trades WHERE status = 'filled'"


async def rows(conn: AsyncConnection, query, params=()) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(query, params)
    return await cur.fetchall()


def jsonable(row: dict) -> dict:
    """A row whose timestamps are ISO strings, for REST and WebSocket alike."""
    return {k: v.isoformat() if isinstance(v, datetime.datetime) else v for k, v in row.items()}


def _pool():
    if (pool := db.pool()) is None:
        raise RuntimeError("database offline")
    return pool


@dataclass
class Holding:
    qty: float = 0.0
    cost: float = 0.0
    realized: float = 0.0

    def apply(self, side: str, quote: float, qty: float) -> None:
        if side == "BUY":
            self.qty += qty
            self.cost += quote
        elif self.qty > 0:
            removed = self.cost * min(qty / self.qty, 1.0)
            self.realized += quote - removed
            self.cost -= removed
            self.qty = max(self.qty - qty, 0.0)


def replay(trades: list[dict]) -> dict[tuple[str, bool], Holding]:
    """Holdings per (instance, real) from filled bot trades, oldest first."""
    held: dict[tuple[str, bool], Holding] = {}
    for t in trades:
        held.setdefault((t["instance_id"], t["real"]), Holding()).apply(t["side"], t["quote"] or 0.0, t["qty"] or 0.0)
    return held


def mode_for(size: float, min_notional: float | None) -> str:
    return "live" if min_notional is not None and size >= min_notional else "paper"


class Bot:
    def __init__(self, publish: Callable[[dict], None] = lambda message: None):
        self.publish = publish
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    async def close(self) -> None:
        # An order in flight finishes and is recorded; the HTTP timeout bounds the wait.
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # --- reads ------------------------------------------------------------------

    @staticmethod
    async def _config(conn: AsyncConnection) -> dict | None:
        found = await rows(conn, """
            SELECT c.instance_id, c.size_usdt, c.activated_at, i.strategy_key, i.symbol, i.interval,
                   coalesce(s.pending_target, s.exposure) AS target
            FROM bot_config c JOIN strategy_instances i ON i.id = c.instance_id
            LEFT JOIN instance_state s ON s.instance_id = c.instance_id
            WHERE c.id = 1 AND c.enabled""")
        return found[0] if found else None

    @staticmethod
    def _cached_min(symbol: str) -> float | None:
        rules = get_service().client.rules.get(symbol)
        return float(rules.min_notional) if rules else None

    @staticmethod
    async def _price(conn: AsyncConnection, symbol: str, interval: str) -> float | None:
        if price := get_service().price_lookup(symbol):
            return price
        last = await rows(conn, "SELECT close FROM candles WHERE symbol = %s AND interval = %s "
                                "ORDER BY open_time DESC LIMIT 1", (symbol, interval))
        return last[0]["close"] if last else None

    async def status(self) -> dict:
        async with _pool().connection() as conn:
            cfg = await self._config(conn)
            trades = await rows(conn, f"SELECT {TRADE_COLUMNS} FROM bot_trades ORDER BY id DESC LIMIT %s",
                                (TRADES_SHOWN,))
            # ponytail: replays every filled bot trade per status; keep running totals if they reach many thousands.
            held = replay(await rows(conn, FILLED + " ORDER BY id"))
            price = await self._price(conn, cfg["symbol"], cfg["interval"]) if cfg else None
        realized = sum(h.realized for h in held.values())
        paper_realized = sum(h.realized for (_, real), h in held.items() if not real)
        active, unrealized, paper_unrealized, drift = None, 0.0, 0.0, 0.0
        position = {"exposure": 0.0, "qty": 0.0, "avg_price": None, "value": 0.0, "unrealized_pnl": 0.0}
        if cfg:
            size, symbol = cfg["size_usdt"], cfg["symbol"]
            strategy = CATALOG.get(cfg["strategy_key"])
            active = {
                "instance_id": cfg["instance_id"], "name": strategy.name if strategy else cfg["strategy_key"],
                "symbol": symbol, "interval": cfg["interval"], "size_usdt": size,
                "mode": mode_for(size, self._cached_min(symbol)), "activated_at": cfg["activated_at"].isoformat(),
                "target": cfg["target"],
            }
            real = held.get((cfg["instance_id"], True), Holding())
            paper = held.get((cfg["instance_id"], False), Holding())
            qty, cost = real.qty + paper.qty, real.cost + paper.cost
            if price:
                unrealized = qty * price - cost
                paper_unrealized = paper.qty * price - paper.cost
            position = {"exposure": cost / size, "qty": qty, "avg_price": cost / qty if qty else None,
                        "value": qty * (price or 0.0), "unrealized_pnl": unrealized}
            if cfg["target"] is not None:   # an instance not yet seeded has no target to follow
                drift = cfg["target"] - position["exposure"]
        return {
            "active": active,
            "position": position,
            "pnl": {"realized": realized, "unrealized": unrealized, "total": realized + unrealized,
                    "paper_total": paper_realized + paper_unrealized},
            "trades": [jsonable(t) for t in trades],
            "in_sync": abs(drift) <= TOLERANCE,
            "drift": drift,
        }

    async def _broadcast(self) -> dict:
        status = await self.status()
        self.publish({"type": "bot", **status})
        return status

    # --- actions ----------------------------------------------------------------

    async def activate(self, instance_id: str, size_usdt: float) -> dict:
        """Follow ``instance_id`` with ``size_usdt``, replacing (after closing) the active one.

        Raises LookupError for an unknown instance and TradeRejected for a symbol or
        size the guard would refuse.
        """
        async with self._lock, _pool().connection() as conn:
            found = await rows(conn, "SELECT symbol FROM strategy_instances WHERE id = %s", (instance_id,))
            if not found:
                raise LookupError(instance_id)
            symbol, limits = found[0]["symbol"], get_service().guard.limits
            if symbol not in limits.allowed_symbols:
                raise TradeRejected(f"{symbol} is not an allowed symbol")
            if not (math.isfinite(size_usdt) and 0 < size_usdt <= limits.max_order_usdt):
                raise TradeRejected(f"size must be above 0 and at most the {limits.max_order_usdt:g} USDT "
                                    "per-order limit")
            if (cfg := await self._config(conn)) and cfg["instance_id"] != instance_id and await self._close(conn, cfg):
                # Switching now would strand the old instance's coins outside any bot's control.
                raise TradeRejected(f"the bot's {cfg['symbol']} bought for {cfg['instance_id']} could not be sold; "
                                    "deactivate the bot or retry once orders go through")
            try:   # loads the symbol's filters so the mode is known; unreachable Binance leaves it paper
                await get_service().client.symbol_rules(symbol)
            except Exception as exc:
                logger.warning("Bot: %s filters not loaded (%s); paper mode", symbol, type(exc).__name__)
            await conn.execute("UPDATE bot_config SET instance_id = %s, size_usdt = %s, activated_at = now(), "
                               "enabled = true WHERE id = 1", (instance_id, size_usdt))
            logger.warning("Bot activated: %s with %g USDT", instance_id, size_usdt)
            await self._reconcile(conn)
        return await self._broadcast()

    async def deactivate(self, close_position: bool) -> dict:
        async with self._lock, _pool().connection() as conn:
            if close_position and (cfg := await self._config(conn)):
                await self._close(conn, cfg)
            await conn.execute("UPDATE bot_config SET instance_id = NULL, activated_at = NULL, enabled = false "
                               "WHERE id = 1")
        logger.warning("Bot deactivated (close_position=%s)", close_position)
        return await self._broadcast()

    async def _close(self, conn: AsyncConnection, cfg: dict) -> bool:
        """Sell the bot's holding of ``cfg``'s instance; True when a real holding worth an order is left."""
        price = await self._price(conn, cfg["symbol"], cfg["interval"])
        if price is None:
            raise TradeRejected(f"no recent {cfg['symbol']} price to close the bot's position at")
        await self._rebalance(conn, cfg, 0.0, price)
        held = replay(await rows(conn, FILLED + " AND instance_id = %s AND real ORDER BY id", (cfg["instance_id"],)))
        left = held.get((cfg["instance_id"], True), Holding()).qty
        # Dust below the minimum cannot be sold at all, so it does not block.
        return left > 0 and left * price >= (self._cached_min(cfg["symbol"]) or 0.0)

    async def reconcile(self) -> dict:
        """Trade the active bot to its instance's current target, then return the status."""
        async with self._lock, _pool().connection() as conn:
            await self._reconcile(conn)
        return await self._broadcast()

    async def _reconcile(self, conn: AsyncConnection) -> None:
        if not (cfg := await self._config(conn)) or cfg["target"] is None:
            return
        try:   # unknown filters would make a live-size bot paper-trade: wait for them instead
            await get_service().client.symbol_rules(cfg["symbol"])
        except Exception as exc:
            logger.warning("Bot: %s filters not loaded (%s); not reconciled", cfg["symbol"], type(exc).__name__)
            return
        price = await self._price(conn, cfg["symbol"], cfg["interval"])
        if price is None:
            logger.warning("Bot: no %s price to reconcile %s at", cfg["symbol"], cfg["instance_id"])
            return
        await self._rebalance(conn, cfg, min(max(cfg["target"], 0.0), 1.0), price)

    async def preview(self, instance_id: str) -> dict:
        """What activating ``instance_id`` starts from: its current target and what the bot already holds of it."""
        async with _pool().connection() as conn:
            found = await rows(conn, """
                SELECT i.symbol, coalesce(s.pending_target, s.exposure) AS target
                FROM strategy_instances i LEFT JOIN instance_state s ON s.instance_id = i.id
                WHERE i.id = %s""", (instance_id,))
            if not found:
                raise LookupError(instance_id)
            held = replay(await rows(conn, FILLED + " AND instance_id = %s ORDER BY id", (instance_id,)))
        return {"instance_id": instance_id, "symbol": found[0]["symbol"], "target": found[0]["target"],
                "held_usdt": sum(h.cost for h in held.values())}

    def submit(self, signal: dict | None = None) -> None:
        """Handle a ``ta_bot_signal`` payload, or with none reconcile, in the background after the actions queued
        before it."""
        task = asyncio.create_task(self.on_signal(signal) if signal else self._resync(), name="bot-signal")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _resync(self) -> None:
        try:
            await self.reconcile()
        except Exception:
            logger.exception("Bot reconcile failed")

    async def on_signal(self, signal: dict) -> None:
        if signal.get("price") is None:   # the engine had no candle to price it: the next reconcile catches up
            logger.info("Bot signal %s has no price; ignored", signal)
            return
        try:
            target, price = float(signal["target"]), float(signal["price"])
            if not (math.isfinite(target) and math.isfinite(price) and price > 0):
                raise ValueError(f"bad target {target} or price {price}")
            async with self._lock, _pool().connection() as conn:
                cfg = await self._config(conn)
                if not cfg or signal.get("instance_id") != cfg["instance_id"]:
                    return
                await self._rebalance(conn, cfg, min(max(target, 0.0), 1.0), price)
            await self._broadcast()
        except Exception:
            logger.exception("Bot signal %s not handled", signal)

    async def _rebalance(self, conn: AsyncConnection, cfg: dict, target: float, price: float) -> None:
        held = replay(await rows(conn, FILLED + " AND instance_id = %s ORDER BY id", (cfg["instance_id"],)))
        real = held.get((cfg["instance_id"], True), Holding())
        paper = held.get((cfg["instance_id"], False), Holding())
        size, symbol = cfg["size_usdt"], cfg["symbol"]
        exposure = (real.cost + paper.cost) / size
        if target > exposure + TOLERANCE:
            quote = round((target - exposure) * size, 8)
            minimum = self._cached_min(symbol)
            if mode_for(size, minimum) == "live" and quote >= minimum:
                await self._real(conn, cfg, OrderRequest(symbol=symbol, side="BUY", type="MARKET",
                                                         quote_amount=quote), price)
            else:
                await self._paper(conn, cfg, "BUY", price, quote=quote)
        elif target < exposure - TOLERANCE:
            part = 1.0 if target <= EPSILON else (exposure - target) / exposure
            if paper.qty > 0:
                await self._paper(conn, cfg, "SELL", price, qty=paper.qty * part)
            if real.qty > 0:   # the bot's own coins, by quantity; the guard floors it to the lot step
                req = OrderRequest(symbol=symbol, side="SELL", type="MARKET", quantity=real.qty * part)
                minimum = self._cached_min(symbol)
                # Checked here too: the guard lets a small MARKET order through when the symbol's filter allows it.
                if minimum is not None and req.quantity * price < minimum:
                    await self._record(conn, cfg, "SELL", None, req.quantity, price, real=True, status="rejected",
                                       reason=f"sell value is below the {minimum:g} USDT minimum")
                else:
                    await self._real(conn, cfg, req, price)

    async def _paper(self, conn: AsyncConnection, cfg: dict, side: str, price: float, *,
                     quote: float = 0.0, qty: float = 0.0) -> None:
        if side == "BUY":
            fill = price * (1 + SLIPPAGE)
            qty = quote * (1 - FEE) / fill
        else:
            fill = price * (1 - SLIPPAGE)
            quote = qty * fill * (1 - FEE)
        await self._record(conn, cfg, side, quote, qty, fill, real=False, status="filled")

    async def _real(self, conn: AsyncConnection, cfg: dict, req: OrderRequest, price: float) -> None:
        try:
            order = await get_service().place_order(req, "bot")
        except TradeRejected as exc:
            await self._record(conn, cfg, req.side, req.quote_amount, req.quantity, price,
                               real=True, status="rejected", reason=exc.reason)
            return
        except Exception as exc:
            # ponytail: an unanswered order may have filled; the bot then under-counts until the user reconciles.
            await self._record(conn, cfg, req.side, req.quote_amount, req.quantity, price,
                               real=True, status="error", reason=str(exc) or type(exc).__name__)
            return
        executed, quote = order["executed_qty"], order["quote_qty"]
        # Binance takes the fee from what is received: counting it off keeps a later SELL within what the bot got.
        qty, quote = (executed * (1 - FEE), quote) if req.side == "BUY" else (executed, quote * (1 - FEE))
        await self._record(conn, cfg, req.side, quote, qty, order["price"], real=True,
                           status="filled" if executed > 0 else "new", order_id=order["order_id"])

    @staticmethod
    async def _record(conn: AsyncConnection, cfg: dict, side: str, quote: float | None, qty: float | None,
                      price: float, *, real: bool, status: str, order_id: int | None = None,
                      reason: str | None = None) -> None:
        await conn.execute(
            "INSERT INTO bot_trades (instance_id, side, quote, qty, price, real, status, order_id, reason) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (cfg["instance_id"], side, quote, qty, price, real, status, order_id, reason))
        # Committed at once: a later failure must not roll back the record of an order that was sent.
        await conn.commit()
        logger.info("Bot %s %s %s: %s%s", "real" if real else "paper", side, cfg["instance_id"], status,
                    f" ({reason})" if reason else "")
