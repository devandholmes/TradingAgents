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
With ``execution='maker_first'`` a strategy's BUY or SELL goes through
``TradingService.place_maker_first`` instead (post-only first, MARKET for the rest).
Each row keeps its fee: summed from the order's fills (live), or modelled in USDT (paper).

Risk exits (``stop_loss_pct``, ``take_profit_pct``, ``trailing_pct``) run on the live
price: ``start`` runs a monitor that checks the active holding every ``RISK_TICK`` seconds
against the in-process price cache (``TradingService.price_lookup``; no request of its
own, and no check while the cache has no fresh price). The reference is the bot's
average fill price (fees left out, so a tight stop does not fire on the fee alone);
trailing uses ``peak_price``, the highest price seen since the position opened (saved
as it rises, so a restart resumes it). On a trigger the whole bot holding
is sold through the same SELL path, always MARKET, with the exit's name as the row's
reason, and the latch is set: the bot then trades to 0 whatever the strategy's target,
until that target returns to 0 itself (its own exit), which clears it.
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
from tradingagents.trading.fees import fee_columns, net_fill, paper_fee
from tradingagents.trading.guard import OrderRequest, TradeRejected
from tradingagents.trading.service import get_service

logger = logging.getLogger(__name__)

SLIPPAGE = 0.0002    # 2 bps, as the backtest
EPSILON = 1e-9       # a target at or below this is flat
TOLERANCE = 0.005    # exposure gaps up to this count as in sync: rounding and fill dust, never worth an order
TRADES_SHOWN = 50
RISK_TICK = 0.5      # seconds between risk checks (at least 1 Hz), from the price cache only
RISK_BACKOFF = 5.0   # extra pause after a failed check, so an outage is not logged twice a second

TRADE_COLUMNS = "id, ts, instance_id, side, quote, qty, price, real, status, order_id, reason, fee, fee_asset"
FILLED = "SELECT instance_id, side, quote, qty, price, real FROM bot_trades WHERE status = 'filled'"


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
    filled_value: float = 0.0   # fill price times quantity held; over qty, the entry the risk exits measure from

    def apply(self, side: str, quote: float, qty: float, price: float = 0.0) -> None:
        if side == "BUY":
            self.qty += qty
            self.cost += quote
            self.filled_value += qty * price
        elif self.qty > 0:
            part = min(qty / self.qty, 1.0)
            removed = self.cost * part
            self.realized += quote - removed
            self.cost -= removed
            self.filled_value -= self.filled_value * part
            self.qty = max(self.qty - qty, 0.0)


def replay(trades: list[dict]) -> dict[tuple[str, bool], Holding]:
    """Holdings per (instance, real) from filled bot trades, oldest first."""
    held: dict[tuple[str, bool], Holding] = {}
    for t in trades:
        held.setdefault((t["instance_id"], t["real"]), Holding()).apply(t["side"], t["quote"] or 0.0, t["qty"] or 0.0,
                                                                        t["price"] or 0.0)
    return held


def mode_for(size: float, min_notional: float | None) -> str:
    return "live" if min_notional is not None and size >= min_notional else "paper"


def risk_levels(avg: float | None, peak: float | None, stop_loss_pct: float | None,
                take_profit_pct: float | None, trailing_pct: float | None) -> dict:
    """The prices the risk exits fire at; None where the exit is off or there is no position."""
    return {
        "stop_price": avg * (1 - stop_loss_pct / 100) if avg and stop_loss_pct else None,
        "take_profit_price": avg * (1 + take_profit_pct / 100) if avg and take_profit_pct else None,
        "trailing_stop_price": peak * (1 - trailing_pct / 100) if peak and trailing_pct else None,
    }


def risk_trigger(price: float, levels: dict) -> str | None:
    """Which exit ``price`` fires, stop-loss first (the backtest's conservative order)."""
    if levels["stop_price"] is not None and price <= levels["stop_price"]:
        return "stop_loss"
    if levels["take_profit_price"] is not None and price >= levels["take_profit_price"]:
        return "take_profit"
    if levels["trailing_stop_price"] is not None and price <= levels["trailing_stop_price"]:
        return "trailing_stop"
    return None


def _tagged(reason: str | None, detail: str) -> str:
    return f"{reason}: {detail}" if reason else detail


@dataclass
class Watch:
    """What the risk monitor checks each tick: the active holding's entry, peak and percentages."""

    instance_id: str
    symbol: str
    avg: float
    peak: float
    stop_loss_pct: float | None
    take_profit_pct: float | None
    trailing_pct: float | None

    def levels(self) -> dict:
        return risk_levels(self.avg, self.peak, self.stop_loss_pct, self.take_profit_pct, self.trailing_pct)


class Bot:
    def __init__(self, publish: Callable[[dict], None] = lambda message: None):
        self.publish = publish
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._watch: Watch | None = None   # refreshed by every status read; None: nothing to protect
        self._monitor: asyncio.Task | None = None
        self._hurry = asyncio.Event()   # set by a firing risk exit: a resting maker-first order stops waiting

    def start(self) -> None:
        """Run the risk monitor (the web lifespan does; tests drive ``check_risk`` themselves)."""
        self._monitor = asyncio.create_task(self._watch_prices(), name="bot-risk")

    async def close(self) -> None:
        if self._monitor:
            self._monitor.cancel()
            await asyncio.gather(self._monitor, return_exceptions=True)
        # An order in flight finishes and is recorded; the HTTP timeout bounds the wait.
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # --- reads ------------------------------------------------------------------

    @staticmethod
    async def _config(conn: AsyncConnection) -> dict | None:
        found = await rows(conn, """
            SELECT c.instance_id, c.size_usdt, c.activated_at, i.strategy_key, i.symbol, i.interval,
                   c.stop_loss_pct, c.take_profit_pct, c.trailing_pct, c.execution, c.peak_price,
                   c.risk_latched, c.risk_reason, coalesce(s.pending_target, s.exposure) AS target
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
        active, unrealized, paper_unrealized, drift, watch = None, 0.0, 0.0, 0.0, None
        position = {"exposure": 0.0, "qty": 0.0, "avg_price": None, "value": 0.0, "unrealized_pnl": 0.0}
        risk = {"stop_loss_pct": None, "take_profit_pct": None, "trailing_pct": None,
                **risk_levels(None, None, None, None, None), "peak_price": None, "latched": False, "reason": None}
        if cfg:
            size, symbol = cfg["size_usdt"], cfg["symbol"]
            strategy = CATALOG.get(cfg["strategy_key"])
            active = {
                "instance_id": cfg["instance_id"], "name": strategy.name if strategy else cfg["strategy_key"],
                "symbol": symbol, "interval": cfg["interval"], "size_usdt": size,
                "mode": mode_for(size, self._cached_min(symbol)), "activated_at": cfg["activated_at"].isoformat(),
                "target": cfg["target"], "execution": cfg["execution"],
            }
            real = held.get((cfg["instance_id"], True), Holding())
            paper = held.get((cfg["instance_id"], False), Holding())
            qty, cost = real.qty + paper.qty, real.cost + paper.cost
            avg = (real.filled_value + paper.filled_value) / qty if qty > 0 else None   # the risk exits' entry
            if price:
                unrealized = qty * price - cost
                paper_unrealized = paper.qty * price - paper.cost
            position = {"exposure": cost / size, "qty": qty, "avg_price": cost / qty if qty else None,
                        "value": qty * (price or 0.0), "unrealized_pnl": unrealized}
            if cfg["target"] is not None:   # an instance not yet seeded has no target to follow
                drift = cfg["target"] - position["exposure"]
            peak = max(cfg["peak_price"] or 0.0, avg) if avg else None
            pcts = (cfg["stop_loss_pct"], cfg["take_profit_pct"], cfg["trailing_pct"])
            risk = {"stop_loss_pct": pcts[0], "take_profit_pct": pcts[1], "trailing_pct": pcts[2],
                    **risk_levels(avg, peak, *pcts), "peak_price": peak,
                    "latched": cfg["risk_latched"], "reason": cfg["risk_reason"]}
            if avg and any(pcts) and not cfg["risk_latched"]:
                watch = Watch(cfg["instance_id"], symbol, avg, peak, *pcts)
        self._watch = watch
        return {
            "active": active,
            "position": position,
            "pnl": {"realized": realized, "unrealized": unrealized, "total": realized + unrealized,
                    "paper_total": paper_realized + paper_unrealized},
            "trades": [jsonable(t) for t in trades],
            "in_sync": abs(drift) <= TOLERANCE,
            "drift": drift,
            "risk": risk,
        }

    async def _broadcast(self) -> dict:
        status = await self.status()
        self.publish({"type": "bot", **status})
        return status

    # --- actions ----------------------------------------------------------------

    async def activate(self, instance_id: str, size_usdt: float, *, stop_loss_pct: float | None = None,
                       take_profit_pct: float | None = None, trailing_pct: float | None = None,
                       execution: str = "market") -> dict:
        """Follow ``instance_id`` with ``size_usdt``, replacing (after closing) the active one.

        The risk percentages (0 < pct <= 50, None: off) and ``execution`` are the
        route's to validate; the table's constraints hold them too. Re-activating the
        active instance keeps its peak and latch; another instance starts clean.
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
            cfg = await self._config(conn)
            if cfg and cfg["instance_id"] != instance_id and await self._close(conn, cfg):
                # Switching now would strand the old instance's coins outside any bot's control.
                raise TradeRejected(f"the bot's {cfg['symbol']} bought for {cfg['instance_id']} could not be sold; "
                                    "deactivate the bot or retry once orders go through")
            try:   # loads the symbol's filters so the mode is known; unreachable Binance leaves it paper
                await get_service().client.symbol_rules(symbol)
            except Exception as exc:
                logger.warning("Bot: %s filters not loaded (%s); paper mode", symbol, type(exc).__name__)
            same = cfg is not None and cfg["instance_id"] == instance_id
            fresh = "" if same else ", peak_price = NULL, risk_latched = false, risk_reason = NULL"
            await conn.execute("UPDATE bot_config SET instance_id = %s, size_usdt = %s, activated_at = now(), "
                               "enabled = true, stop_loss_pct = %s, take_profit_pct = %s, trailing_pct = %s, "
                               f"execution = %s{fresh} WHERE id = 1",
                               (instance_id, size_usdt, stop_loss_pct, take_profit_pct, trailing_pct, execution))
            logger.warning("Bot activated: %s with %g USDT (stop %s, take profit %s, trailing %s, %s)", instance_id,
                           size_usdt, stop_loss_pct, take_profit_pct, trailing_pct, execution)
            await self._reconcile(conn)
        return await self._broadcast()

    async def deactivate(self, close_position: bool) -> dict:
        async with self._lock, _pool().connection() as conn:
            if close_position and (cfg := await self._config(conn)):
                await self._close(conn, cfg)
            await conn.execute("UPDATE bot_config SET instance_id = NULL, activated_at = NULL, enabled = false, "
                               "peak_price = NULL, risk_latched = false, risk_reason = NULL WHERE id = 1")
        logger.warning("Bot deactivated (close_position=%s)", close_position)
        return await self._broadcast()

    async def set_risk(self, stop_loss_pct: float | None, take_profit_pct: float | None,
                       trailing_pct: float | None, execution: str | None = None) -> dict:
        """Change the active bot's risk exits (None: off) and, if given, its execution; LookupError if none."""
        async with self._lock, _pool().connection() as conn:
            if not await self._config(conn):
                raise LookupError("no active bot")
            await conn.execute("UPDATE bot_config SET stop_loss_pct = %s, take_profit_pct = %s, trailing_pct = %s, "
                               "execution = coalesce(%s, execution) WHERE id = 1",
                               (stop_loss_pct, take_profit_pct, trailing_pct, execution))
        logger.warning("Bot risk set: stop %s, take profit %s, trailing %s, execution %s",
                       stop_loss_pct, take_profit_pct, trailing_pct, execution or "unchanged")
        return await self._broadcast()

    # --- risk exits ---------------------------------------------------------------

    async def _watch_prices(self) -> None:
        while True:
            await asyncio.sleep(RISK_TICK)
            try:
                await self.check_risk()
            except Exception:
                logger.exception("Bot risk check failed")
                await asyncio.sleep(RISK_BACKOFF)

    async def check_risk(self) -> str | None:
        """One monitor tick: the exit the cached price fired (and was handled), or None.

        Memory only, unless the price makes a new peak (saved) or fires an exit.
        """
        if (watch := self._watch) is None:
            return None
        price = get_service().price_lookup(watch.symbol)
        if not price or not math.isfinite(price) or price <= 0:
            return None   # no fresh price: nothing to judge by
        if price > watch.peak:
            watch.peak = price
            try:
                async with _pool().connection() as conn:
                    await conn.execute("UPDATE bot_config SET peak_price = greatest(peak_price, %s) "
                                       "WHERE id = 1 AND enabled AND instance_id = %s", (price, watch.instance_id))
            except Exception as exc:   # the exit check below must not wait on it
                logger.warning("Bot peak price not saved: %s", db.describe(exc))
        if not (reason := risk_trigger(price, watch.levels())):
            return None
        self._hurry.set()   # an order in flight under the lock gives way within one poll
        await self._risk_exit(watch, price)
        return reason

    async def _risk_exit(self, watch: Watch, price: float) -> None:
        """Re-check under the lock on fresh rows (so repeated ticks exit once), latch, then sell everything."""
        # Waits behind an action in flight; a resting maker-first order is cut short by ``_hurry`` (about a second).
        async with self._lock, _pool().connection() as conn:
            cfg = await self._config(conn)
            if cfg and cfg["instance_id"] == watch.instance_id and not cfg["risk_latched"]:
                held = replay(await rows(conn, FILLED + " AND instance_id = %s ORDER BY id", (cfg["instance_id"],)))
                qty = sum(h.qty for h in held.values())
                avg = sum(h.filled_value for h in held.values()) / qty if qty > 0 else None
                peak = max(cfg["peak_price"] or 0.0, watch.peak, avg or 0.0)
                levels = risk_levels(avg, peak, cfg["stop_loss_pct"], cfg["take_profit_pct"], cfg["trailing_pct"])
                if avg and (reason := risk_trigger(price, levels)):
                    # Latched before selling: a failed sell is retried by the next reconcile, never re-fired.
                    await conn.execute("UPDATE bot_config SET risk_latched = true, risk_reason = %s WHERE id = 1",
                                       (reason,))
                    await conn.commit()
                    logger.warning("Bot %s on %s at %g (entry %g, peak %g)", reason, cfg["symbol"], price, avg, peak)
                    await self._rebalance(conn, {**cfg, "risk_latched": True, "risk_reason": reason}, 0.0, price)
        await self._broadcast()   # also refreshes the watch, so a stale one stops here

    async def _follow(self, conn: AsyncConnection, cfg: dict, target: float) -> float:
        """The target the bot trades to: 0 while a risk exit's latch holds; the strategy's own 0 clears it."""
        if not cfg["risk_latched"]:
            return target
        if target <= EPSILON:
            await conn.execute("UPDATE bot_config SET risk_latched = false, risk_reason = NULL WHERE id = 1")
            await conn.commit()
            logger.warning("Bot risk latch (%s) cleared: %s is flat", cfg["risk_reason"], cfg["instance_id"])
            cfg.update(risk_latched=False, risk_reason=None)
        return 0.0

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
        await self._rebalance(conn, cfg, await self._follow(conn, cfg, min(max(cfg["target"], 0.0), 1.0)), price)

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
                await self._rebalance(conn, cfg, await self._follow(conn, cfg, min(max(target, 0.0), 1.0)), price)
            await self._broadcast()
        except Exception:
            logger.exception("Bot signal %s not handled", signal)

    async def _rebalance(self, conn: AsyncConnection, cfg: dict, target: float, price: float) -> None:
        held = replay(await rows(conn, FILLED + " AND instance_id = %s ORDER BY id", (cfg["instance_id"],)))
        real = held.get((cfg["instance_id"], True), Holding())
        paper = held.get((cfg["instance_id"], False), Holding())
        size, symbol = cfg["size_usdt"], cfg["symbol"]
        exposure = (real.cost + paper.cost) / size
        # A latched rebalance is a risk exit (or its retry): MARKET only, tagged with the exit.
        exit_reason = cfg.get("risk_reason") if cfg.get("risk_latched") else None
        if target > exposure + TOLERANCE:
            if real.qty + paper.qty <= 0:   # a new position: the trailing peak starts over
                await conn.execute("UPDATE bot_config SET peak_price = NULL WHERE id = 1")
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
                await self._paper(conn, cfg, "SELL", price, qty=paper.qty * part, reason=exit_reason)
            if real.qty > 0:   # the bot's own coins, by quantity; the guard floors it to the lot step
                req = OrderRequest(symbol=symbol, side="SELL", type="MARKET", quantity=real.qty * part)
                minimum = self._cached_min(symbol)
                # Checked here too: the guard lets a small MARKET order through when the symbol's filter allows it.
                if minimum is not None and req.quantity * price < minimum:
                    await self._record(conn, cfg, "SELL", None, req.quantity, price, real=True, status="rejected",
                                       reason=_tagged(exit_reason, f"sell value is below the {minimum:g} USDT minimum"))
                else:
                    await self._real(conn, cfg, req, price, exit_reason)

    async def _paper(self, conn: AsyncConnection, cfg: dict, side: str, price: float, *,
                     quote: float = 0.0, qty: float = 0.0, reason: str | None = None) -> None:
        rate = paper_fee()
        if side == "BUY":
            fill = price * (1 + SLIPPAGE)
            fee = quote * rate
            qty = (quote - fee) / fill
        else:
            fill = price * (1 - SLIPPAGE)
            fee = qty * fill * rate
            quote = qty * fill - fee
        await self._record(conn, cfg, side, quote, qty, fill, real=False, status="filled", reason=reason,
                           fee=fee, fee_asset="USDT")

    async def _real(self, conn: AsyncConnection, cfg: dict, req: OrderRequest, price: float,
                    exit_reason: str | None = None) -> None:
        """Send ``req`` and record it: MARKET for risk exits and ``execution='market'``, else maker-first."""
        service = get_service()
        maker = exit_reason is None and cfg.get("execution") == "maker_first"
        try:
            if maker:
                self._hurry.clear()
                result = await service.place_maker_first(req, stop=self._hurry)
            else:
                order = await service.place_order(req, "bot")
        except TradeRejected as exc:
            await self._record(conn, cfg, req.side, req.quote_amount, req.quantity, price,
                               real=True, status="rejected", reason=_tagged(exit_reason, exc.reason))
            return
        except Exception as exc:
            # ponytail: an unanswered order may have filled; the bot then under-counts until the user reconciles.
            await self._record(conn, cfg, req.side, req.quote_amount, req.quantity, price, real=True,
                               status="error", reason=_tagged(exit_reason, str(exc) or type(exc).__name__))
            return
        if maker:
            executed, quote, commission = result["filled_qty"], result["quote"], result["commission"]
            fill = quote / executed if executed else price
            order_id = result["order_ids"][0] if result["order_ids"] else None
            status = "filled" if executed > 0 else "cancelled"
        else:
            executed, quote, commission = order["executed_qty"], order["quote_qty"], order.get("commission")
            fill, order_id = order["price"], order["order_id"]
            status = "filled" if executed > 0 else "new"
        rules = service.client.rules.get(req.symbol)
        base, quote_asset = (rules.base_asset, rules.quote_asset) if rules else ("", "")
        if commission is None:   # an answer without fills: the fee is modelled, off what is received
            rate = paper_fee()
            commission = {base: executed * rate} if req.side == "BUY" else {quote_asset: quote * rate}
        # Binance takes the fee from what is received: counting it off keeps a later SELL within what the bot got.
        qty, quote = net_fill(req.side, executed, quote, commission, base, quote_asset)
        fee, fee_asset = fee_columns(commission)
        await self._record(conn, cfg, req.side, quote, qty, fill, real=True, status=status, order_id=order_id,
                           reason=exit_reason, fee=fee, fee_asset=fee_asset or None)

    @staticmethod
    async def _record(conn: AsyncConnection, cfg: dict, side: str, quote: float | None, qty: float | None,
                      price: float, *, real: bool, status: str, order_id: int | None = None,
                      reason: str | None = None, fee: float | None = None, fee_asset: str | None = None) -> None:
        await conn.execute(
            "INSERT INTO bot_trades (instance_id, side, quote, qty, price, real, status, order_id, reason, fee, "
            "fee_asset) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (cfg["instance_id"], side, quote, qty, price, real, status, order_id, reason, fee, fee_asset))
        # Committed at once: a later failure must not roll back the record of an order that was sent.
        await conn.commit()
        logger.info("Bot %s %s %s: %s%s", "real" if real else "paper", side, cfg["instance_id"], status,
                    f" ({reason})" if reason else "")
