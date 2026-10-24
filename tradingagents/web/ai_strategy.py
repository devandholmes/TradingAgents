"""The TradingAgents AI strategy's daily signals: one full analysis per allowed symbol, every day.

At 00:10 UTC, just after the daily candle closes, the scheduler starts an
analysis of each symbol of ``TRADING_ALLOWED_SYMBOLS`` through the dashboard's
own run registry (``analyze.Runs``), one at a time, so the runs show in the
Assistant's history like any other. A finished run's rating becomes an
``ai_signals`` row and a ``ta_ai_signal`` NOTIFY in one transaction; the
strategy engine turns it into the ``tradingagents_ai`` instances' exposure.

Each run costs LLM credits. ``TRADINGAGENTS_AI_STRATEGY=off`` turns the scheduler
off; without a database it does not start, since the signals would have nowhere to go.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable

from tradingagents import db
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.trading.guard import Limits
from tradingagents.web import analyze

logger = logging.getLogger(__name__)

ENV = "TRADINGAGENTS_AI_STRATEGY"
RUN_AT = datetime.time(0, 10, tzinfo=datetime.UTC)
POLL_SECONDS = 15              # how often a run under way is checked
RUN_TIMEOUT = 3 * 3600         # seconds a symbol may take, waiting for a free slot included, before it is cancelled
CHANNEL = "ta_ai_signal"

Clock = Callable[[], datetime.datetime]
Sleep = Callable[[float], Awaitable[object]]


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def enabled() -> bool:
    return (os.environ.get(ENV) or "on").strip().lower() not in ("off", "0", "false", "no")


def next_run_at(now: datetime.datetime) -> datetime.datetime:
    """The first RUN_AT strictly after ``now``."""
    today = datetime.datetime.combine(now.astimezone(datetime.UTC).date(), RUN_AT)
    return today if now < today else today + datetime.timedelta(days=1)


async def record_signal(symbol: str, run_id: str, rating: str) -> None:
    """Insert the signal and notify the engine, together: NOTIFY is delivered on commit."""
    if (pool := db.pool()) is None:
        raise RuntimeError("the database is offline")
    async with pool.connection() as conn:   # one transaction, committed on exit
        await conn.execute("INSERT INTO ai_signals (symbol, run_id, rating) VALUES (%s, %s, %s)",
                           (symbol, run_id, rating))
        await conn.execute("SELECT pg_notify(%s, %s)", (CHANNEL, json.dumps({"symbol": symbol, "rating": rating})))


async def _analyse(runs: analyze.Runs, symbol: str, trade_date: str, now: Clock, sleep: Sleep) -> analyze.Run | None:
    """Start the symbol's run once a slot is free and wait for it to end; None when no slot came free."""
    deadline = now() + datetime.timedelta(seconds=RUN_TIMEOUT)
    # The user's own runs share MAX_RUNNING: wait for a free slot rather than skip the day.
    while (run := await asyncio.to_thread(runs.start, symbol, normalize_symbol(symbol), trade_date,
                                          analyze.ANALYSTS)) is None:
        if now() >= deadline:
            return None
        await sleep(POLL_SECONDS)
    while run.status == "running":
        if now() >= deadline:
            await asyncio.to_thread(runs.cancel, run)
            break
        await sleep(POLL_SECONDS)
    return run


async def run_daily(runs: analyze.Runs, symbols: list[str], trade_date: str, now: Clock = _utcnow,
                    sleep: Sleep = asyncio.sleep) -> dict[str, str]:
    """Analyse each symbol in turn and record each rating; the ratings recorded, by symbol."""
    signals = {}
    for symbol in symbols:
        run = await _analyse(runs, symbol, trade_date, now, sleep)
        if run is None or run.status != "done" or not run.rating:
            logger.warning("AI strategy: no signal for %s on %s (%s)", symbol, trade_date,
                           "no free analysis slot" if run is None else run.error or run.status)
            continue
        try:
            await record_signal(symbol, run.id, run.rating)
        except Exception as exc:
            logger.warning("AI strategy: %s signal of run %s not recorded (%s)", symbol, run.id, db.describe(exc))
            continue
        signals[symbol] = run.rating
        logger.info("AI strategy: %s rated %s (run %s)", symbol, run.rating, run.id)
    return signals


async def scheduler(runs: analyze.Runs, symbols: list[str], now: Clock = _utcnow,
                    sleep: Sleep = asyncio.sleep) -> None:
    """Every day at RUN_AT, the day's analyses. A day the server was down for is not caught up."""
    while True:
        current = now()
        await sleep((next_run_at(current) - current).total_seconds())
        try:
            await run_daily(runs, symbols, now().date().isoformat(), now, sleep)
        except Exception:   # tomorrow is another day: the task must outlive one bad one
            logger.exception("AI strategy: the daily analyses failed")


@contextlib.asynccontextmanager
async def running_scheduler(runs: analyze.Runs, database_up: bool) -> AsyncIterator[asyncio.Task | None]:
    """The scheduler as a task for the app's lifetime; None when it is off or there is no database."""
    if not (database_up and enabled()):
        yield None
        return
    task = asyncio.create_task(scheduler(runs, list(Limits.from_env().allowed_symbols)), name="ai-strategy")
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
