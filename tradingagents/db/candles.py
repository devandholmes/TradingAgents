"""The ``candles`` table: Binance klines by symbol and interval.

Rows go in and come out as ``get_klines`` returns them: ``time`` is the candle's
open in unix seconds (UTC), then open, high, low, close and volume. Only closed
candles are stored, so a stored candle never changes; an upsert of the same
candle rewrites nothing.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable

import psycopg

INTERVAL_SECONDS = {"15m": 900, "1h": 3600, "4h": 4 * 3600, "1d": 86400}

_UPSERT = """
INSERT INTO candles (symbol, interval, open_time, open, high, low, close, volume)
VALUES (%s, %s, to_timestamp(%s), %s, %s, %s, %s, %s)
ON CONFLICT (symbol, interval, open_time) DO UPDATE
SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, close = EXCLUDED.close, volume = EXCLUDED.volume
WHERE (candles.open, candles.high, candles.low, candles.close, candles.volume)
      IS DISTINCT FROM (EXCLUDED.open, EXCLUDED.high, EXCLUDED.low, EXCLUDED.close, EXCLUDED.volume)
"""


def upsert_candles(conn: psycopg.Connection, symbol: str, interval: str, rows: Iterable[dict]) -> int:
    """Insert or correct candles in the caller's transaction; the number of rows sent."""
    params = [(symbol, interval, r["time"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows]
    if params:
        with conn.cursor() as cur:
            cur.executemany(_UPSERT, params)   # pipelined: one round trip per batch, not per row
    return len(params)


def read_candles(
    conn: psycopg.Connection, symbol: str, interval: str, start: datetime.datetime, end: datetime.datetime
) -> list[dict]:
    """Candles opened in ``[start, end)``, oldest first."""
    rows = conn.execute(
        "SELECT extract(epoch FROM open_time)::bigint, open, high, low, close, volume FROM candles"
        " WHERE symbol = %s AND interval = %s AND open_time >= %s AND open_time < %s ORDER BY open_time",
        (symbol, interval, start, end),
    ).fetchall()
    return [dict(zip(("time", "open", "high", "low", "close", "volume"), row, strict=True)) for row in rows]


def latest_open_time(conn: psycopg.Connection, symbol: str, interval: str) -> datetime.datetime | None:
    """The newest stored candle's open, or None for a series with none."""
    return conn.execute(
        "SELECT max(open_time) FROM candles WHERE symbol = %s AND interval = %s", (symbol, interval)
    ).fetchone()[0]
