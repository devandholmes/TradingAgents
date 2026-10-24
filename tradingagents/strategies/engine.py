"""The strategy engine: ``tradingagents-engine`` or ``python -m tradingagents.strategies.engine``.

A process of its own (the web app spawns it unless ``TRADINGAGENTS_ENGINE=off``),
so paper-trading every strategy never costs the web server's event loop anything.

- Every catalog strategy runs on every allowed symbol (``TRADING_ALLOWED_SYMBOLS``)
  and interval as the instance ``<key>:<SYMBOL>:<interval>``, with 1000 USDT.
- An instance without state is seeded by backtesting the stored candles: its fills
  (source ``backtest``), one equity point per day, its state and its metrics.
- One combined websocket then streams every series' klines. On each closed bar the
  candle is stored, each instance on that series recomputes its target over the
  last ``LOOKBACK`` bars, the target decided at the previous close fills at this
  bar's open (as in the backtest), and fills, equity, state and metrics are written
  in one transaction that ends with NOTIFY ta_strategy_update, plus ta_bot_signal
  when the bot's instance (bot_config) changed its target.
- Each (re)connect first fetches the bars missed while away from REST, and a restart
  resumes every instance from instance_state, replaying the stored bars it has not
  seen, so a bar is never traded twice or skipped.
- A stateful strategy (a latch) can hold a position for longer than the window, so it
  carries its state across the window's edge: from the target it decided at the last
  bar traded, only the window's entries and exits after that bar count. On start its
  pending target is checked against one full-history computation, as its seed was.
- ``tradingagents_ai`` trades the latest ai_signals rating (LISTEN ta_ai_signal).

Metrics: 7d, 30d, 90d and 1y from daily equity (each day's last close, annualised by
365, so every interval ranks alike); ``live`` from every bar since going live.

All database and CPU work runs on one worker thread, in order; the event loop only
reads the websocket and the notifications.
"""

from __future__ import annotations

import asyncio
import bisect
import json
import logging
import math
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from tradingagents import db
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.db import backfill
from tradingagents.db.candles import INTERVAL_SECONDS, latest_open_time, upsert_candles
from tradingagents.strategies.backtest import _win_rate, backtest, bars_per_year, metrics
from tradingagents.strategies.library import CATALOG, INTERVALS, Strategy, exposure_from_ratings

logger = logging.getLogger(__name__)

CAPITAL, FEE, SLIPPAGE = 1000.0, 0.001, 0.0002
LOOKBACK = 1000      # bars a live target is computed over
LIVE_BARS = 5000     # ponytail: the live metrics cover at most the last 5000 bars; read the table for more
MAX_FILLS = 5000     # recent fills kept per instance, for the windows' trades and win rate
DAILY_DAYS = 400     # daily equity points kept per instance (the 1y window and its baseline)
PERIODS = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}
STREAM_URL = "wss://data-stream.binance.vision/stream?streams="
LOCK_KEY = 0x7A_DB_0002   # session advisory lock: one engine per database
AI_KEY = "tradingagents_ai"
NOTIFY_IDS = 100     # instance ids per NOTIFY, well inside its 8000-byte payload
AI_RETRY = 5         # seconds before listening again after a lost connection

COLUMNS = ("time", "open", "high", "low", "close", "volume")
METRICS = ("return_pct", "sharpe", "sortino", "max_drawdown_pct", "win_rate", "exposure_pct", "trades")

_INSERT_FILL = (
    "INSERT INTO paper_fills (instance_id, ts, side, price, qty, quote, fee, exposure_after, source)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
_COPY_FILLS = "COPY paper_fills (instance_id, ts, side, price, qty, quote, fee, exposure_after, source) FROM STDIN"
_UPSERT_POINT = (
    "INSERT INTO equity_points (instance_id, ts, equity, exposure, source) VALUES (%s, %s, %s, %s, %s)"
    " ON CONFLICT (instance_id, ts) DO UPDATE SET equity = EXCLUDED.equity, exposure = EXCLUDED.exposure,"
    " source = EXCLUDED.source"
)
_COPY_POINTS = "COPY equity_points (instance_id, ts, equity, exposure, source) FROM STDIN"
_UPSERT_STATE = (
    "INSERT INTO instance_state (instance_id, exposure, cash, qty, equity, pending_target, last_bar, updated_at)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, now()) ON CONFLICT (instance_id) DO UPDATE SET"
    " exposure = EXCLUDED.exposure, cash = EXCLUDED.cash, qty = EXCLUDED.qty, equity = EXCLUDED.equity,"
    " pending_target = EXCLUDED.pending_target, last_bar = EXCLUDED.last_bar, updated_at = now()"
)
_UPSERT_METRIC = (
    "INSERT INTO strategy_metrics (instance_id, period, return_pct, sharpe, sortino, max_drawdown_pct, win_rate,"
    " exposure_pct, trades, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())"
    " ON CONFLICT (instance_id, period) DO UPDATE SET return_pct = EXCLUDED.return_pct, sharpe = EXCLUDED.sharpe,"
    " sortino = EXCLUDED.sortino, max_drawdown_pct = EXCLUDED.max_drawdown_pct, win_rate = EXCLUDED.win_rate,"
    " exposure_pct = EXCLUDED.exposure_pct, trades = EXCLUDED.trades, updated_at = now()"
)


@dataclass
class Instance:
    id: str
    strategy: Strategy
    symbol: str
    interval: str
    exposure: float = 0.0
    cash: float = CAPITAL
    qty: float = 0.0
    equity: float = CAPITAL
    pending: float | None = None          # target decided at last_bar's close, filled at the next open
    last_bar: pd.Timestamp | None = None  # None: not seeded
    daily: dict = field(default_factory=dict)                            # day -> (ts, equity), oldest first
    live: deque = field(default_factory=lambda: deque(maxlen=LIVE_BARS))  # (ts, equity) since going live
    fills: deque = field(default_factory=lambda: deque(maxlen=MAX_FILLS))

    def clear(self) -> None:
        self.daily.clear()
        self.live.clear()
        self.fills.clear()

    def mark(self, ts: pd.Timestamp, equity: float) -> None:
        self.daily[ts.floor("D")] = (ts, equity)
        while len(self.daily) > DAILY_DAYS:
            del self.daily[next(iter(self.daily))]
        self.live.append((ts, equity))

    def fill(self, ts: pd.Timestamp, open_: float) -> dict | None:
        """Move to the pending target at ``open_``: the backtest's execution model, one bar at a time."""
        new, level = self.pending, self.exposure
        if new is None or new == level:
            return None
        if new > level:
            price = open_ * (1 + SLIPPAGE)
            budget = self.cash * (new - level) / (1 - level)
            quote = budget / (1 + FEE)
            qty = quote / price
            self.qty += qty
            self.cash -= budget
            side = "BUY"
        else:
            price = open_ * (1 - SLIPPAGE)
            qty = self.qty * (level - new) / level
            quote = qty * price
            self.qty -= qty
            self.cash += quote * (1 - FEE)
            side = "SELL"
        self.exposure = new
        fill = {"ts": ts, "side": side, "price": price, "qty": qty, "quote": quote, "fee": quote * FEE,
                "exposure_after": new}
        self.fills.append(fill)
        return fill

    def state_row(self) -> tuple:
        return (self.id, self.exposure, self.cash, self.qty, self.equity, self.pending, self.last_bar)


def _frame(rows) -> pd.DataFrame:
    """Candles (dicts or tuples in ``COLUMNS`` order) indexed by open time."""
    df = pd.DataFrame(rows, columns=list(COLUMNS))
    df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("time").astype("int64"), unit="s", utc=True), name="open_time")
    return df.astype(float)


def _num(value):
    return None if value is None or not math.isfinite(value) else float(value)


def window_metrics(points: list[tuple], fills: list[dict], start: pd.Timestamp | None, per_year: float) -> tuple:
    """Metrics over ``points`` (ts, equity) from the last point at or before ``start``.

    Trades and win rate count the fills after that point; the fill before it only
    carries the exposure held into the window.
    """
    if start is not None:
        points = points[max(bisect.bisect_right([t for t, _ in points], start) - 1, 0):]
    first = points[0][0]
    inside = [f for f in fills if f["ts"] > first]
    carry = [f for f in fills if f["ts"] <= first][-1:]
    equity = pd.Series([e for _, e in points], index=pd.DatetimeIndex([t for t, _ in points]))
    m = metrics(equity, carry + inside, per_year)
    m.update(trades=len(inside), win_rate=_win_rate(inside))
    return (*(_num(m[k]) for k in METRICS[:-1]), m["trades"])


class Engine:
    """Instances, their state and the candle windows, driven by closed bars; not thread-safe."""

    def __init__(self, url: str | None = None, symbols=None, intervals=INTERVALS):
        if symbols is None:
            from tradingagents.trading.guard import Limits

            symbols = Limits.from_env().allowed_symbols
        self.url = url
        self.series = [(s, i) for s in symbols for i in intervals]
        self.conn: psycopg.Connection | None = None
        self.instances: dict[str, Instance] = {}
        self.windows: dict[tuple, pd.DataFrame] = {}
        self.anchors: dict[tuple, pd.Timestamp | None] = {}
        self.broken = True   # (re)load from the database before the next bar

    # --- startup -------------------------------------------------------------------------------

    def start(self, fetch: bool = True) -> None:
        """Connect, create the instances, fetch missed candles (``fetch``), seed, then resume."""
        self.close()
        self.conn = db.connect(self.url)
        if not self.conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()[0]:
            self.close()
            raise RuntimeError("another strategy engine is already running on this database")
        self.conn.commit()
        self._refresh_instances()
        if fetch:
            self.fetch_all()
        self._seed()
        self._resume()
        self.broken = False

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()   # also releases the advisory lock
            self.conn = None
        self.broken = True

    def _ensure(self) -> None:
        if self.broken:
            self.start(fetch=False)

    def _refresh_instances(self) -> None:
        """Create the universe's instances; one whose params changed starts over (seeded again)."""
        rows = [(f"{s.key}:{symbol}:{interval}", s.key, symbol, interval, Jsonb(s.params))
                for symbol, interval in self.series for s in CATALOG.values() if interval in s.intervals]
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO strategy_instances (id, strategy_key, symbol, interval, params)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET params = EXCLUDED.params,"
                " backtested_through = NULL, live_since = NULL"
                " WHERE strategy_instances.params IS DISTINCT FROM EXCLUDED.params", rows)
        self.conn.commit()
        self.instances = {r[0]: Instance(r[0], CATALOG[r[1]], r[2], r[3]) for r in rows}

    def _candles(self, symbol: str, interval: str, start: pd.Timestamp | None = None) -> pd.DataFrame:
        sql = ("SELECT extract(epoch FROM open_time)::bigint, open, high, low, close, volume FROM candles"
               " WHERE symbol = %s AND interval = %s")
        params: list = [symbol, interval]
        if start is not None:
            sql += " AND open_time >= %s"
            params.append(start)
        return _frame(self.conn.execute(sql + " ORDER BY open_time", params).fetchall())

    def _target(self, inst: Instance, candles: pd.DataFrame, carry: bool = False) -> pd.Series:
        """``inst``'s target over ``candles``; with ``carry``, from its state at its last bar (only bars after it are valid)."""
        if inst.strategy.key == AI_KEY:   # follows ai_signals (sync_ai), not the candles
            return pd.Series(inst.pending or 0.0, index=candles.index)
        anchor = self.anchors.get((inst.symbol, inst.interval))
        if inst.strategy.key == "dca" and anchor is not None and candles.index[0] > anchor:
            # DCA's steps are timed from the series' first candle, not from the window's.
            head = candles.iloc[:1].set_axis(pd.DatetimeIndex([anchor]))
            return inst.strategy.target(pd.concat([head, candles])).iloc[1:]
        if carry:   # a latch's state outlives the window: its earlier bars only warm the indicators up
            return inst.strategy.target(candles, inst.pending or 0.0, inst.last_bar)
        return inst.strategy.target(candles)

    def _ai_target(self, symbol: str) -> float:
        ratings = [r for (r,) in self.conn.execute(
            "SELECT rating FROM ai_signals WHERE symbol = %s ORDER BY ts, id", (symbol,))]
        return float(exposure_from_ratings(pd.Series(ratings, dtype="string")).iloc[-1]) if ratings else 0.0

    def _seed(self) -> None:
        todo = {iid for (iid,) in self.conn.execute(
            "SELECT i.id FROM strategy_instances i LEFT JOIN instance_state s ON s.instance_id = i.id"
            " WHERE i.id = ANY(%s) AND (i.backtested_through IS NULL OR s.instance_id IS NULL)",
            (list(self.instances),))}
        self.conn.commit()
        if not todo:
            return
        began, seeded, last = time.monotonic(), [], None
        for symbol, interval in self.series:
            ids = [i.id for i in self.instances.values() if i.id in todo and (i.symbol, i.interval) == (symbol, interval)]
            if not ids:
                continue
            candles = self._candles(symbol, interval)
            if candles.empty:
                logger.warning("%s %s: no stored candles, %d instances wait for them", symbol, interval, len(ids))
                continue
            for iid in ids:
                self._seed_instance(self.instances[iid], candles)
                seeded.append(iid)
            last = candles.index[-1] if last is None else max(last, candles.index[-1])
        if seeded:
            self._notify_update(seeded, last)
            self.conn.commit()
        logger.info("Seeded %d instances in %.1f s", len(seeded), time.monotonic() - began)

    def _seed_instance(self, inst: Instance, candles: pd.DataFrame) -> None:
        """Backtest ``candles`` and store the result as the instance's history and state; one transaction."""
        last, close = candles.index[-1], float(candles["close"].iloc[-1])
        inst.clear()
        if inst.strategy.key == AI_KEY:   # live only: never backtested
            inst.exposure, inst.cash, inst.qty, inst.equity = 0.0, CAPITAL, 0.0, CAPITAL
            inst.pending, fills = self._ai_target(inst.symbol), []
            daily = pd.Series([CAPITAL], index=pd.DatetimeIndex([last]))
            held, source = [0.0], "live"
        else:
            target = self._target(inst, candles)
            result = backtest(candles, target, CAPITAL, FEE, SLIPPAGE)
            fills = result.fills
            inst.qty = sum(f["qty"] if f["side"] == "BUY" else -f["qty"] for f in fills)
            inst.equity = float(result.equity.iloc[-1])
            inst.cash = inst.equity - inst.qty * close
            inst.exposure, inst.pending = float(result.exposure.iloc[-1]), float(target.iloc[-1])
            daily = result.equity.groupby(result.equity.index.floor("D")).tail(1)
            held, source = result.exposure.reindex(daily.index), "backtest"
        inst.last_bar = last
        for ts, equity in daily.items():
            inst.mark(ts, float(equity))
        inst.live.clear()
        inst.live.append((last, inst.equity))
        inst.fills.extend(fills)

        cur = self.conn.cursor()
        for table in ("paper_fills", "equity_points", "strategy_metrics", "instance_state"):
            cur.execute(f"DELETE FROM {table} WHERE instance_id = %s", (inst.id,))
        with cur.copy(_COPY_FILLS) as copy:
            for f in fills:
                copy.write_row((inst.id, f["ts"], f["side"], f["price"], f["qty"], f["quote"], f["fee"],
                                f["exposure_after"], "backtest"))
        with cur.copy(_COPY_POINTS) as copy:
            for (ts, equity), exposure in zip(daily.items(), held, strict=True):
                copy.write_row((inst.id, ts, float(equity), float(exposure), source))
        cur.execute(_UPSERT_STATE, inst.state_row())
        cur.executemany(_UPSERT_METRIC, self._metric_rows(inst))
        cur.execute("UPDATE strategy_instances SET backtested_through = %s, live_since = now() WHERE id = %s",
                    (last, inst.id))
        self.conn.commit()

    def _resume(self) -> None:
        """Load every instance's state and recent history, then replay the stored bars it has not seen."""
        ids = list(self.instances)
        for inst in self.instances.values():
            inst.last_bar = None
            inst.clear()
        for iid, exposure, cash, qty, equity, pending, last_bar in self.conn.execute(
                "SELECT instance_id, exposure, cash, qty, equity, pending_target, last_bar FROM instance_state"
                " WHERE instance_id = ANY(%s)", (ids,)):
            inst = self.instances[iid]
            inst.exposure, inst.cash, inst.qty, inst.equity, inst.pending = exposure, cash, qty, equity, pending
            inst.last_bar = pd.Timestamp(last_bar)
        for iid, ts, equity in self.conn.execute(
                "SELECT DISTINCT ON (e.instance_id, date_trunc('day', e.ts)) e.instance_id, e.ts, e.equity"
                " FROM equity_points e JOIN instance_state s USING (instance_id)"
                " WHERE e.instance_id = ANY(%s) AND e.ts > s.last_bar - make_interval(days => %s)"
                " ORDER BY e.instance_id, date_trunc('day', e.ts), e.ts DESC", (ids, DAILY_DAYS)):
            ts = pd.Timestamp(ts)
            self.instances[iid].daily[ts.floor("D")] = (ts, equity)
        for iid, ts, equity in self.conn.execute(
                "SELECT i.id, e.ts, e.equity FROM strategy_instances i CROSS JOIN LATERAL ("
                " SELECT ts, equity FROM equity_points WHERE instance_id = i.id AND ts >= i.backtested_through"
                " ORDER BY ts DESC LIMIT %s) e WHERE i.id = ANY(%s) ORDER BY i.id, e.ts", (LIVE_BARS, ids)):
            self.instances[iid].live.append((pd.Timestamp(ts), equity))
        for iid, ts, side, price, qty, quote, fee, after in self.conn.execute(
                "SELECT i.id, f.ts, f.side, f.price, f.qty, f.quote, f.fee, f.exposure_after"
                " FROM strategy_instances i CROSS JOIN LATERAL (SELECT * FROM paper_fills WHERE instance_id = i.id"
                " ORDER BY ts DESC LIMIT %s) f WHERE i.id = ANY(%s) ORDER BY i.id, f.ts", (MAX_FILLS, ids)):
            self.instances[iid].fills.append({"ts": pd.Timestamp(ts), "side": side, "price": price, "qty": qty,
                                              "quote": quote, "fee": fee, "exposure_after": after})
        self.conn.commit()

        for key in self.series:
            step = pd.Timedelta(seconds=INTERVAL_SECONDS[key[1]])
            first = self.conn.execute("SELECT min(open_time) FROM candles WHERE symbol = %s AND interval = %s",
                                      key).fetchone()[0]
            self.anchors[key] = pd.Timestamp(first) if first else None
            self._check_latches(key)
            seen = [i.last_bar for i in self._on(key)]
            latest = min(seen) if seen else latest_open_time(self.conn, *key)
            self.conn.commit()
            if latest is None:
                self.windows[key] = _frame([])
                continue
            self._replay(key, pd.Timestamp(latest) - LOOKBACK * step)

    def _check_latches(self, key: tuple) -> None:
        """Set each latch's pending target to the full history's at its last bar, as its seed was decided.

        Normally a no-op; it corrects a target decided while latches restarted flat at
        the window's edge. Corrected states are stored, and the bot is told of its own.
        """
        latches = [i for i in self._on(key) if i.strategy.stateful]
        if not latches:
            return
        # ponytail: reads the whole stored series once per start; read from each instance's seed start if slow
        candles, fixed = self._candles(*key), []
        active = self._active()
        for inst in latches:
            seen = candles.loc[:inst.last_bar]
            if seen.empty:
                continue
            wanted = float(self._target(inst, seen).iloc[-1])
            if wanted != inst.pending:
                logger.warning("%s: pending target %s corrected to %s", inst.id, inst.pending, wanted)
                before, inst.pending = inst.pending, wanted
                fixed.append(inst)
                if inst.id == active:
                    self._notify("ta_bot_signal", self._bot_signal(inst, before, float(seen["close"].iloc[-1])))
        if fixed:
            self.conn.cursor().executemany(_UPSERT_STATE, [i.state_row() for i in fixed])
        self.conn.commit()

    # --- bars ----------------------------------------------------------------------------------

    def _on(self, key: tuple) -> list[Instance]:
        return [i for i in self.instances.values() if (i.symbol, i.interval) == key and i.last_bar is not None]

    def _replay(self, key: tuple, start: pd.Timestamp | None) -> list[str]:
        """Trade the stored bars from ``start`` that each instance has not seen, and keep the window."""
        frame = self._candles(*key, start=start)
        changed = self._advance(key, frame) if not frame.empty else []
        self.conn.commit()
        self.windows[key] = frame.iloc[-LOOKBACK:]
        return changed

    def fetch(self, key: tuple) -> bool:
        """Store the series' closed candles missing since the newest stored one, from REST."""
        symbol, interval = key
        try:
            backfill.backfill_series(self.conn, symbol, interval,
                                     backfill.DAILY_DAYS if interval == "1d" else backfill.DAYS)
            return True
        except VendorUnavailableError as exc:
            self.conn.rollback()
            logger.warning("%s %s: missed candles not fetched (%s)", symbol, interval, exc)
            return False

    def fetch_all(self) -> None:
        for key in self.series:
            if not self.fetch(key):
                return   # Binance is unreachable: the next (re)connect tries again

    def catch_up(self) -> list[str]:
        """After a (re)connect: fetch every series' missed bars from REST and trade them."""
        self._ensure()
        self.fetch_all()
        changed = []
        for key in self.series:
            window = self.windows.get(key)
            changed += self._replay(key, window.index[0] if window is not None and len(window) else None)
        return changed

    def on_bar(self, symbol: str, interval: str, candle: dict) -> list[str]:
        """A closed kline (``COLUMNS`` keys, ``time`` in unix seconds); the instance ids it updated."""
        self._ensure()
        key = (symbol, interval)
        window = self.windows.get(key)
        if window is None:
            return []
        ts = pd.Timestamp(candle["time"], unit="s", tz="UTC")
        if len(window) and ts <= window.index[-1]:
            return []   # already traded (the REST catch-up got there first)
        upsert_candles(self.conn, symbol, interval, [candle])
        if len(window) and ts - window.index[-1] > pd.Timedelta(seconds=INTERVAL_SECONDS[interval]):
            self.conn.commit()   # a gap: fetch the bars in between, then trade them all in order
            self.fetch(key)
            return self._replay(key, window.index[0])
        frame = pd.concat([window, _frame([candle])]).iloc[-LOOKBACK:]
        changed = self._advance(key, frame)
        self.conn.commit()
        self.windows[key] = frame
        return changed

    def _active(self) -> str | None:
        row = self.conn.execute("SELECT instance_id FROM bot_config WHERE id = 1 AND enabled").fetchone()
        return row[0] if row else None

    def _advance(self, key: tuple, candles: pd.DataFrame) -> list[str]:
        """Trade the bars of ``candles`` newer than each instance's last bar, then NOTIFY; the caller commits."""
        fills, points, changed = [], [], []
        active, signal = self._active(), None
        for inst in self._on(key):
            bars = candles[candles.index > inst.last_bar]
            if bars.empty:
                continue
            target = self._target(inst, candles, carry=True).reindex(bars.index)
            before = inst.pending
            for ts, open_, close, wanted in zip(bars.index, bars["open"], bars["close"], target, strict=True):
                if fill := inst.fill(ts, float(open_)):
                    fills.append((inst.id, ts, fill["side"], fill["price"], fill["qty"], fill["quote"], fill["fee"],
                                  fill["exposure_after"], "live"))
                inst.equity = inst.cash + inst.qty * float(close)
                inst.mark(ts, inst.equity)
                points.append((inst.id, ts, inst.equity, inst.exposure, "live"))
                inst.pending, inst.last_bar = float(wanted), ts
            changed.append(inst)
            if inst.id == active and inst.pending != before:
                signal = self._bot_signal(inst, before, float(bars["close"].iloc[-1]))
        if not changed:
            return []
        with self.conn.cursor() as cur:
            cur.executemany(_INSERT_FILL, fills)
            cur.executemany(_UPSERT_POINT, points)
            cur.executemany(_UPSERT_STATE, [i.state_row() for i in changed])
            cur.executemany(_UPSERT_METRIC, [row for i in changed for row in self._metric_rows(i)])
        self._notify_update([i.id for i in changed], candles.index[-1])
        if signal:
            self._notify("ta_bot_signal", signal)
        return [i.id for i in changed]

    def _metric_rows(self, inst: Instance) -> list[tuple]:
        days, fills = list(inst.daily.values()), list(inst.fills)
        rows = [(inst.id, period, *window_metrics(days, fills, inst.last_bar - pd.Timedelta(days=span), 365))
                for period, span in PERIODS.items()]
        rows.append((inst.id, "live", *window_metrics(list(inst.live), fills, None, bars_per_year(inst.interval))))
        return rows

    # --- AI signals and notifications ----------------------------------------------------------

    def sync_ai(self, symbol: str | None = None) -> None:
        """Point the AI instances (of ``symbol``, or all) at their latest rating; it fills at the next open."""
        self._ensure()
        active = self._active()
        for inst in self.instances.values():
            if inst.strategy.key != AI_KEY or inst.last_bar is None or symbol not in (None, inst.symbol):
                continue
            target = self._ai_target(inst.symbol)
            if target == inst.pending:
                continue
            before, inst.pending = inst.pending, target
            self.conn.execute(_UPSERT_STATE, inst.state_row())
            if inst.id == active:
                window = self.windows.get((inst.symbol, inst.interval))
                price = float(window["close"].iloc[-1]) if window is not None and len(window) else None
                self._notify("ta_bot_signal", self._bot_signal(inst, before, price))
        self.conn.commit()

    @staticmethod
    def _bot_signal(inst: Instance, before: float | None, price: float | None) -> dict:
        return {"instance_id": inst.id, "target": inst.pending,
                "previous": before if before is not None else inst.exposure,
                "price": price, "bar": inst.last_bar.isoformat()}

    def _notify(self, channel: str, payload: dict) -> None:
        self.conn.execute("SELECT pg_notify(%s, %s)", (channel, json.dumps(payload)))

    def _notify_update(self, ids: list[str], bar: pd.Timestamp) -> None:
        for i in range(0, len(ids), NOTIFY_IDS):
            self._notify("ta_strategy_update", {"instance_ids": ids[i:i + NOTIFY_IDS], "bar": bar.isoformat()})


# --- the process ---------------------------------------------------------------------------------


def stream_url(series) -> str:
    return STREAM_URL + "/".join(f"{symbol.lower()}@kline_{interval}" for symbol, interval in series)


def closed_candle(message) -> tuple[str, str, dict] | None:
    """(symbol, interval, candle) of a combined-stream kline message, or None while the bar is open."""
    data = json.loads(message)["data"]
    k = data["k"]
    if not k["x"]:
        return None
    return data["s"], k["i"], {"time": k["t"] // 1000, "open": float(k["o"]), "high": float(k["h"]),
                            "low": float(k["l"]), "close": float(k["c"]), "volume": float(k["v"])}


async def run(engine: Engine, connect=None, listen: bool = True) -> None:
    """Start ``engine`` and drive it from the kline stream and ta_ai_signal until cancelled."""
    from tradingagents.trading.streams import _reconnecting

    if connect is None:
        from websockets.asyncio.client import connect
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(1, thread_name_prefix="engine")
    bars: asyncio.Queue = asyncio.Queue()   # bounded in practice: one closed bar per series per interval

    async def call(fn, *args):
        try:
            return await loop.run_in_executor(executor, fn, *args)
        except Exception as exc:
            logger.exception("%s failed (%s); reloading from the database", fn.__name__, db.describe(exc))
            engine.broken = True

    async def consume(ws) -> None:
        bars.put_nowait(None)   # catch up first: the bars missed while disconnected
        async for message in ws:
            if closed := closed_candle(message):
                bars.put_nowait(closed)

    async def trade() -> None:
        while True:
            item = await bars.get()
            changed = await (call(engine.catch_up) if item is None else call(engine.on_bar, *item))
            if changed:
                logger.info("%s: %d instances updated", "catch-up" if item is None else " ".join(item[:2]),
                            len(changed))

    async def listen_ai() -> None:
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                        engine.url or db.database_url(), autocommit=True, **db.SESSION) as conn:
                    await conn.execute("LISTEN ta_ai_signal")
                    await call(engine.sync_ai)   # signals sent while not listening
                    async for note in conn.notifies():
                        await call(engine.sync_ai, json.loads(note.payload).get("symbol"))
            except (psycopg.Error, ValueError) as exc:
                logger.warning("ta_ai_signal listener: %s", db.describe(exc))
            await asyncio.sleep(AI_RETRY)

    try:
        await loop.run_in_executor(executor, engine.start)
        tasks = [_reconnecting("Kline", stream_url(engine.series), connect, consume, lambda: None), trade()]
        await asyncio.gather(*tasks, *([listen_ai()] if listen else []))
    finally:
        await loop.run_in_executor(executor, engine.close)
        executor.shutdown(wait=False)


def main() -> None:
    from dotenv import find_dotenv, load_dotenv

    from tradingagents.db.migrate import migrate

    load_dotenv(find_dotenv(usecwd=True))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s engine %(levelname)s %(message)s")
    if not migrate():
        raise SystemExit(1)
    try:
        with asyncio.Runner(loop_factory=db.LOOP_FACTORY) as runner:
            runner.run(run(Engine()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
