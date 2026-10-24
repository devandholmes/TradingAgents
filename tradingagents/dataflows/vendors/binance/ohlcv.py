"""Binance daily candles and stockstats indicators for the agents.

Same signatures and output text as the yfinance implementations
(``get_YFin_data_online`` / ``get_stock_stats_indicators_window``) so the two
are interchangeable in a vendor chain. Only crypto pairs are served; any other
symbol raises ``NoMarketDataError`` so a ``"binance,yfinance"`` chain falls
through to the next vendor. Candles are daily, in UTC. They are read from
Postgres (``tradingagents.db``) when it holds every day asked for, from
Binance's REST API otherwise.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

import pandas as pd
from dateutil.relativedelta import relativedelta
from stockstats import wrap

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors.binance.client import get_klines, to_binance_symbol
from tradingagents.dataflows.vendors.yahoo.market import INDICATOR_DESCRIPTIONS
from tradingagents.dataflows.vendors.yahoo.ohlcv import _assert_ohlcv_not_stale

logger = logging.getLogger(__name__)

# Binance's maximum candles per klines request.
KLINES_PAGE = 1000

# Extra history fetched ahead of an indicator window so long averages
# (200 SMA) and EMA-based series (MACD) have settled by the first reported day.
INDICATOR_WARMUP_DAYS = 1000


def _stored_daily(pair: str, start_s: int, end_s: int) -> list[dict] | None:
    """The daily candles opened in ``[start_s, end_s)`` from Postgres, when every one is stored.

    None, and the caller asks Binance, when a day is missing (only closed candles
    are stored, so a range ending today always is), when no database is set up,
    or when it cannot be reached.
    """
    try:   # psycopg comes with the [web] extra
        import psycopg

        from tradingagents.db import connect, database_url
        from tradingagents.db.candles import read_candles
    except ImportError:
        return None
    if not database_url():
        return None
    try:
        with connect() as conn:
            rows = read_candles(conn, pair, "1d", datetime.fromtimestamp(start_s, UTC),
                                datetime.fromtimestamp(end_s, UTC))
    except psycopg.Error as exc:
        logger.debug("Stored candles unavailable for %s: %s", pair, exc)
        return None
    return rows if rows and len(rows) == (end_s - start_s) // 86400 else None


def _daily_candles(symbol: str, start_date: str, end_date: str) -> tuple[pd.DataFrame, str]:
    """Daily OHLCV for ``start_date``..``end_date`` inclusive, indexed by ``Date``.

    No candle opened after ``end_date`` is returned, so a backtest never sees
    future prices. A candle for ``end_date`` itself is included even while it is
    still open, like the yfinance vendor's current-day row (#986).
    """
    pair = to_binance_symbol(symbol)
    if pair is None:
        raise NoMarketDataError(symbol, None, "Binance lists crypto pairs only")

    start_s = int(datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=UTC)
    end_s = int((end_dt + timedelta(days=1)).timestamp())  # exclusive

    rows = _stored_daily(pair, start_s, end_s)
    if rows is None:
        rows = []
        start_ms, end_ms = start_s * 1000, end_s * 1000 - 1
        while start_ms <= end_ms:
            page = get_klines(pair, "1d", KLINES_PAGE, start_ms, end_ms)
            rows += page
            if len(page) < KLINES_PAGE:
                break
            start_ms = (page[-1]["time"] + 1) * 1000

    # Enforce the window here too rather than trusting the endpoint's filter.
    rows = [row for row in rows if start_s <= row["time"] < end_s]
    if not rows:
        raise NoMarketDataError(symbol, pair, f"rows between {start_date} and {end_date}")

    data = pd.DataFrame(rows)
    data["Date"] = pd.to_datetime(data.pop("time"), unit="s")
    data = data.drop_duplicates("Date").set_index("Date")
    data.columns = [c.capitalize() for c in data.columns]
    return data, pair


def get_stock_data(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
):
    data, pair = _daily_candles(symbol, start_date, end_date)
    _assert_ohlcv_not_stale(data, end_date, symbol, pair)

    # Prices keep Binance's own precision: rounding to cents would erase the
    # movement of sub-dollar coins.
    csv_string = data.to_csv()

    label = pair if pair == symbol.upper() else f"{pair} (from {symbol})"
    header = f"# Stock data for {label} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(data)}\n\n"

    return header + csv_string


def get_indicators(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    as_of_date: Annotated[
        str, "The current trading date you are trading on, YYYY-mm-dd"
    ],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    if indicator not in INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator} is not supported. Please choose from: {list(INDICATOR_DESCRIPTIONS.keys())}"
        )

    as_of_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    before = as_of_dt - relativedelta(days=look_back_days)
    history_start = (before - timedelta(days=INDICATOR_WARMUP_DAYS)).strftime("%Y-%m-%d")

    data, pair = _daily_candles(symbol, history_start, as_of_date)
    _assert_ohlcv_not_stale(data, as_of_date, symbol, pair)

    values = {
        ts.strftime("%Y-%m-%d"): "N/A" if pd.isna(value) else str(value)
        for ts, value in wrap(data)[indicator].items()
    }

    ind_string = ""
    current_dt = as_of_dt
    while current_dt >= before:
        date_str = current_dt.strftime("%Y-%m-%d")
        ind_string += f"{date_str}: {values.get(date_str, 'N/A: No candle for this date')}\n"
        current_dt = current_dt - relativedelta(days=1)

    return (
        f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {as_of_date}:\n\n"
        + ind_string
        + "\n\n"
        + INDICATOR_DESCRIPTIONS[indicator]
    )
