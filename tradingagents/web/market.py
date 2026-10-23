"""Market data endpoints for the dashboard: candles, 24h trends and indicators.

``get_indicators``, ``get_trends`` and ``market_snapshot`` are plain functions
so other modules (the chat endpoint's live context) can reuse them without HTTP.
Vendor failures propagate as ``VendorError``; the app maps them to HTTP errors.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal

import pandas as pd
from fastapi import APIRouter, Query
from pydantic import BaseModel
from stockstats import wrap

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors.binance import client

Interval = Literal[
    "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"
]
Symbol = Annotated[str, Query(pattern=r"^[A-Za-z0-9]{2,20}$")]

# Candles the indicators are computed over; enough for the 50-bar SMA to settle.
INDICATOR_BARS = 200

# Output key -> stockstats column.
_INDICATOR_COLUMNS = {
    "rsi14": "rsi_14",
    "macd": "macd",
    "macd_signal": "macds",
    "macd_hist": "macdh",
    "sma20": "close_20_sma",
    "sma50": "close_50_sma",
    "ema20": "close_20_ema",
}

_LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
_TREND_FIELDS = ("symbol", "lastPrice", "priceChangePercent", "quoteVolume")


class Candle(BaseModel):
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class TrendItem(BaseModel):
    symbol: str
    lastPrice: float
    priceChangePercent: float
    quoteVolume: float


class Trends(BaseModel):
    gainers: list[TrendItem]
    losers: list[TrendItem]
    volume: list[TrendItem]


class Indicators(BaseModel):
    symbol: str
    interval: str
    price: float
    rsi14: float | None
    macd: float | None
    macd_signal: float | None
    macd_hist: float | None
    sma20: float | None
    sma50: float | None
    ema20: float | None
    trend: Literal["up", "down", "sideways"]


def _is_leveraged(base: str) -> bool:
    # Leveraged tokens are a coin plus a suffix (BTCUP, ETHDOWN); the length check
    # keeps real coins that merely end in one (JUP) in the list.
    return any(base.endswith(s) and len(base) > len(s) + 1 for s in _LEVERAGED_SUFFIXES)


def get_trends(quote: str = "USDT", limit: int = 10) -> dict:
    """Top gainers, losers and quote-volume leaders among ``quote``-quoted pairs."""
    quote = quote.upper()
    rows = [
        {field: t[field] for field in _TREND_FIELDS}
        for t in client.get_24h_tickers()
        # Zero volume marks a halted or delisted pair still listed with stale stats.
        if t["symbol"].endswith(quote)
        and t["quoteVolume"] > 0
        and not _is_leveraged(t["symbol"][: -len(quote)])
    ]
    by_change = sorted(rows, key=lambda t: t["priceChangePercent"], reverse=True)
    return {
        "gainers": by_change[:limit],
        "losers": by_change[::-1][:limit],
        "volume": sorted(rows, key=lambda t: t["quoteVolume"], reverse=True)[:limit],
    }


def _klines(symbol: str, interval: str) -> list[dict]:
    klines = client.get_klines(symbol, interval, limit=INDICATOR_BARS)
    if not klines:
        raise NoMarketDataError(symbol, detail=f"Binance returned no {interval} candles")
    return klines


def _number(value) -> float | None:
    value = float(value)
    return None if math.isnan(value) else value


def _indicators(symbol: str, interval: str, klines: list[dict]) -> dict:
    df = wrap(pd.DataFrame(klines))
    values = {key: _number(df[column].iloc[-1]) for key, column in _INDICATOR_COLUMNS.items()}
    price, sma20, sma50 = klines[-1]["close"], values["sma20"], values["sma50"]
    trend = "sideways"
    if sma20 is not None and sma50 is not None:
        if price > sma20 > sma50:
            trend = "up"
        elif price < sma20 < sma50:
            trend = "down"
    return {"symbol": symbol, "interval": interval, "price": price, **values, "trend": trend}


def get_indicators(symbol: str, interval: str = "1h") -> dict:
    """RSI, MACD and moving averages over the last ``INDICATOR_BARS`` candles, plus a trend label."""
    symbol = symbol.upper()
    return _indicators(symbol, interval, _klines(symbol, interval))


def market_snapshot(symbol: str, interval: str = "1h", candles: int = 24) -> dict:
    """Live context for ``symbol``: 24h ticker, indicators and the last ``candles`` candles."""
    symbol = symbol.upper()
    klines = _klines(symbol, interval)
    tickers = client.get_24h_tickers([symbol])
    return {
        "ticker": tickers[0] if tickers else None,
        "indicators": _indicators(symbol, interval, klines),
        "candles": klines[-candles:],
    }


router = APIRouter(prefix="/api", tags=["market"])


@router.get("/klines", response_model=list[Candle])
def klines(
    symbol: Symbol,
    interval: Interval = "1m",
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> list[dict]:
    return client.get_klines(symbol, interval, limit=limit)


@router.get("/trends", response_model=Trends)
def trends(
    quote: Annotated[str, Query(pattern=r"^[A-Za-z]{2,10}$")] = "USDT",
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> dict:
    return get_trends(quote, limit)


@router.get("/indicators", response_model=Indicators)
def indicators(symbol: Symbol, interval: Interval = "1h") -> dict:
    return get_indicators(symbol, interval)
