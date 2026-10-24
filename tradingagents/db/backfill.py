"""Backfill Binance klines into Postgres: ``python -m tradingagents.db.backfill``.

Runs as a process of its own, never inside the web server. For each allowed
symbol (``TRADING_ALLOWED_SYMBOLS``) and interval it resumes after the newest
stored candle, or starts ``--days`` back for a series with none, and pages
through the public klines endpoint 1000 candles at a time up to the last
closed candle. Each page is committed as it lands, so an interrupted run loses
at most one page and the next run carries on from there. Requests are paced and
a failed one is retried with a growing pause, well inside Binance's request
weight limit.

A series is only ever extended forward: a larger ``--days`` later does not
reach further back for a series already stored.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import time

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors.binance.client import get_klines
from tradingagents.dataflows.vendors.binance.ohlcv import INDICATOR_WARMUP_DAYS, KLINES_PAGE
from tradingagents.db import connect, describe
from tradingagents.db.candles import INTERVAL_SECONDS, latest_open_time, upsert_candles
from tradingagents.db.migrate import migrate

logger = logging.getLogger(__name__)

DAYS = 365
# Daily candles reach further back, so the agents' indicators (1000 days of warm-up) read them too.
DAILY_DAYS = INDICATOR_WARMUP_DAYS + DAYS
PAUSE = 0.2   # seconds between requests: about 10 request weight a second against Binance's 6000 a minute
RETRY_PAUSES = (5, 15, 45)


def _klines(pair: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    for pause in (*RETRY_PAUSES, None):
        try:
            return get_klines(pair, interval, KLINES_PAGE, start_ms, end_ms)
        except VendorUnavailableError as exc:
            if pause is None:
                raise
            logger.warning("%s %s: %s; retrying in %ss", pair, interval, exc, pause)
            time.sleep(pause)
    raise AssertionError("unreachable")


def backfill_series(conn, pair: str, interval: str, days: int, now: float | None = None) -> int:
    """Fetch and store ``pair``'s closed ``interval`` candles not yet stored; the number fetched."""
    step = INTERVAL_SECONDS[interval]
    now = time.time() if now is None else now
    end_ms = (int(now) // step * step) * 1000 - 1   # the open candle is not stored
    latest = latest_open_time(conn, pair, interval)
    start_s = int(latest.timestamp()) + step if latest else (int(now) - days * 86400) // step * step
    fetched, start_ms = 0, start_s * 1000
    while start_ms <= end_ms:
        page = [row for row in _klines(pair, interval, start_ms, end_ms) if row["time"] * 1000 <= end_ms]
        if not page:
            break
        upsert_candles(conn, pair, interval, page)
        conn.commit()
        fetched += len(page)
        start_ms = (page[-1]["time"] + step) * 1000
        if len(page) < KLINES_PAGE:
            break
        time.sleep(PAUSE)
    return fetched


def main(argv: list[str] | None = None) -> None:
    from tradingagents.trading.guard import Limits

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=DAYS, help=f"history for a new series (default {DAYS})")
    parser.add_argument("--symbols", help="comma-separated pairs (default TRADING_ALLOWED_SYMBOLS)")
    parser.add_argument("--intervals", default=",".join(INTERVAL_SECONDS), help="comma-separated intervals")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else Limits.from_env().allowed_symbols
    intervals = [i.strip() for i in args.intervals.split(",")]
    if unknown := set(intervals) - set(INTERVAL_SECONDS):
        parser.error(f"unknown intervals {sorted(unknown)}; choose from {', '.join(INTERVAL_SECONDS)}")
    if not migrate():
        raise SystemExit(1)

    began = time.monotonic()
    print(f"{'symbol':<10} {'interval':<8} {'fetched':>8} {'stored':>8}  first .. last")
    with connect() as conn:
        for pair in symbols:
            for interval in intervals:
                days = max(args.days, DAILY_DAYS) if interval == "1d" else args.days
                try:
                    fetched = backfill_series(conn, pair, interval, days)
                except VendorUnavailableError as exc:
                    conn.rollback()
                    print(f"{pair:<10} {interval:<8} failed: {describe(exc)}")
                    continue
                count, first, last = conn.execute(
                    "SELECT count(*), min(open_time), max(open_time) FROM candles WHERE symbol = %s AND interval = %s",
                    (pair, interval),
                ).fetchone()
                span = f"{first:%Y-%m-%d %H:%M} .. {last:%Y-%m-%d %H:%M}" if count else "-"
                print(f"{pair:<10} {interval:<8} {fetched:>8} {count:>8}  {span}", flush=True)
        total = conn.execute("SELECT count(*) FROM candles").fetchone()[0]
    print(f"{total} candles stored; took {datetime.timedelta(seconds=round(time.monotonic() - began))}")


if __name__ == "__main__":
    main()
