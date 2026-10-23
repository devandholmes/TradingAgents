"""Binance public market-data client.

Uses the keyless market-data mirror (https://data-api.binance.vision), which
serves only public endpoints: no account, no orders, no API key. Prices come
back as strings and are converted to floats here so callers get numbers.
"""

from __future__ import annotations

import json

import requests

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.symbols import crypto_base

BASE_URL = "https://data-api.binance.vision"

# Network timeout (seconds), consistent with the other vendors.
REQUEST_TIMEOUT = 30

_TICKER_FIELDS = (
    "lastPrice", "priceChangePercent", "highPrice", "lowPrice", "volume", "quoteVolume",
)


def _get(path: str, params: dict | None = None):
    """GET a public endpoint; any failure is raised as VendorUnavailableError."""
    try:
        response = requests.get(f"{BASE_URL}{path}", params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except requests.HTTPError as exc:
        # Binance explains a rejected request in the body, e.g. {"msg": "Invalid symbol."}.
        detail = exc.response.text[:200] if exc.response is not None else ""
        status = exc.response.status_code if exc.response is not None else "?"
        raise VendorUnavailableError(f"Binance {path} returned HTTP {status}: {detail}") from exc
    except requests.RequestException as exc:
        raise VendorUnavailableError(f"Binance {path} request failed: {exc}") from exc


def to_binance_symbol(ticker: str) -> str | None:
    """Map ``BTC-USD``/``BTCUSD``/``BTC-USDT``/``btcusdt`` to ``BTCUSDT``; None for non-crypto.

    Binance has no USD pairs, so every USD-family quote resolves to the USDT pair.
    """
    base = crypto_base(ticker)
    return f"{base}USDT" if base else None


def get_klines(
    symbol: str,
    interval: str = "1d",
    limit: int = 500,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[dict]:
    """Return candles oldest first, ``time`` being the open time in unix seconds."""
    params = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
    if start_ms is not None:
        params["startTime"] = start_ms
    if end_ms is not None:
        params["endTime"] = end_ms
    rows = _get("/api/v3/klines", params)
    return [
        {
            "time": int(row[0]) // 1000,
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        }
        for row in rows
    ]


def get_24h_tickers(symbols: list[str] | None = None) -> list[dict]:
    """Return 24h rolling-window stats for ``symbols``, or for every symbol when None."""
    params = None
    if symbols:
        params = {"symbols": json.dumps([s.upper() for s in symbols], separators=(",", ":"))}
    return [
        {"symbol": row["symbol"], **{field: float(row[field]) for field in _TICKER_FIELDS}}
        for row in _get("/api/v3/ticker/24hr", params)
    ]
