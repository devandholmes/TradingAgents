"""Binance as a get_stock_data / get_indicators vendor.

``get_klines`` is replaced by an in-memory candle source, so these run without
a network connection. The fake ignores ``endTime`` and returns candles past the
requested end, so the vendor's own point-in-time cut is what is under test.
"""
import re
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from tradingagents.dataflows import router
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.vendors.binance import ohlcv
from tradingagents.dataflows.vendors.yahoo import market

FIRST_DAY = datetime(2025, 1, 1, tzinfo=timezone.utc)
CANDLES = [
    {
        "time": int((FIRST_DAY + timedelta(days=i)).timestamp()),
        "open": 100.0 + i,
        "high": 101.5 + i,
        "low": 99.25 + i,
        "close": 100.5 + i + (i % 7) * 0.3,
        "volume": 1000.0 + 10 * i,
    }
    for i in range(500)  # through 2026-05-15, well past every requested date
]


@pytest.fixture
def klines(monkeypatch):
    calls = []

    def fake_get_klines(symbol, interval="1d", limit=500, start_ms=None, end_ms=None):
        calls.append({"symbol": symbol, "interval": interval, "limit": limit,
                      "start_ms": start_ms, "end_ms": end_ms})
        return [c for c in CANDLES if c["time"] * 1000 >= start_ms][:limit]

    monkeypatch.setattr(ohlcv, "get_klines", fake_get_klines)
    return calls


def _yahoo_frame(through: str) -> pd.DataFrame:
    """The same candles shaped like a yfinance ``history`` frame."""
    df = pd.DataFrame([c for c in CANDLES if c["time"] < pd.Timestamp(through, tz="UTC").timestamp() + 86400])
    df.index = pd.to_datetime(df.pop("time"), unit="s").dt.tz_localize("UTC").rename("Date")
    df.columns = [c.capitalize() for c in df.columns]
    return df


def _fail(*args, **kwargs):
    raise AssertionError("this vendor should not have been called")


@pytest.mark.unit
def test_crypto_is_served_by_binance(klines, monkeypatch):
    monkeypatch.setitem(router.VENDOR_METHODS["get_stock_data"], "yfinance", _fail)
    monkeypatch.setattr(ohlcv, "KLINES_PAGE", 4)  # 10 days -> three pages
    set_config({"data_vendors": {"core_stock_apis": "binance,yfinance"}})

    out = router.route_to_vendor("get_stock_data", "BTC-USD", "2026-03-01", "2026-03-10")

    assert out.startswith("# Stock data for BTCUSDT (from BTC-USD) from 2026-03-01 to 2026-03-10\n")
    assert "# Total records: 10\n" in out
    assert {c["symbol"] for c in klines} == {"BTCUSDT"}
    assert len(klines) == 3


@pytest.mark.unit
@pytest.mark.parametrize("method, args, category", [
    ("get_stock_data", ("AAPL", "2026-03-01", "2026-03-10"), "core_stock_apis"),
    ("get_indicators", ("AAPL", "rsi", "2026-03-10", 30), "technical_indicators"),
])
def test_non_crypto_falls_through_to_yfinance(klines, monkeypatch, method, args, category):
    monkeypatch.setitem(router.VENDOR_METHODS[method], "yfinance", lambda *a, **k: "from yfinance")
    set_config({"data_vendors": {category: "binance,yfinance"}})

    assert router.route_to_vendor(method, *args) == "from yfinance"
    assert klines == []  # never asked Binance about a stock


@pytest.mark.unit
def test_non_crypto_alone_reports_no_data(klines):
    set_config({"data_vendors": {"core_stock_apis": "binance"}})
    out = router.route_to_vendor("get_stock_data", "AAPL", "2026-03-01", "2026-03-10")
    assert out.startswith("NO_DATA_AVAILABLE"), out


@pytest.mark.unit
def test_stock_data_matches_the_yfinance_format(klines, monkeypatch):
    monkeypatch.setattr(market, "yf_retry", lambda fn: _yahoo_frame("2026-03-10")
                        .loc["2026-03-01":])
    yahoo = market.get_YFin_data_online("BTC-USD", "2026-03-01", "2026-03-10")
    binance = ohlcv.get_stock_data("BTC-USD", "2026-03-01", "2026-03-10")

    def shape(text):
        lines = text.splitlines()
        return [re.sub(r"for .+ from", "for <label> from", lines[0]), *lines[1:4],
                [line.split(",")[0] for line in lines[4:]]]

    assert shape(binance) == shape(yahoo)
    assert binance.splitlines()[3] == "Date,Open,High,Low,Close,Volume"


@pytest.mark.unit
@pytest.mark.parametrize("indicator", ["close_50_sma", "macd", "rsi", "boll_ub", "atr"])
def test_indicators_match_the_yfinance_output(klines, monkeypatch, indicator):
    """Same candles in, the same text out: header, values, description."""
    monkeypatch.setattr(market, "load_ohlcv", lambda symbol, as_of: _yahoo_frame(as_of)
                        .tz_localize(None).reset_index())
    yahoo = market.get_stock_stats_indicators_window("BTC-USD", indicator, "2026-03-10", 20)
    binance = ohlcv.get_indicators("BTC-USD", indicator, "2026-03-10", 20)

    assert binance == yahoo


@pytest.mark.unit
def test_no_candle_after_the_requested_date(klines):
    out = ohlcv.get_stock_data("ETH-USDT", "2026-03-01", "2026-03-10")
    dates = [line.split(",")[0] for line in out.splitlines()[4:]]
    assert dates[-1] == "2026-03-10"
    end_of_day_ms = int(datetime(2026, 3, 11, tzinfo=timezone.utc).timestamp()) * 1000 - 1
    assert all(c["end_ms"] == end_of_day_ms for c in klines)

    ind = ohlcv.get_indicators("ETH-USDT", "close_10_ema", "2026-03-10", 5)
    assert "2026-03-11" not in ind
    # The as-of value is computed from candles up to that day only.
    closes = pd.Series([c["close"] for c in CANDLES if c["time"] * 1000 <= end_of_day_ms])
    expected = closes.ewm(span=10, adjust=True).mean().iloc[-1]
    assert float(ind.splitlines()[2].split(": ")[1]) == pytest.approx(expected)


@pytest.mark.unit
def test_unsupported_indicator_is_rejected_like_yfinance(klines):
    with pytest.raises(ValueError, match="Indicator foo is not supported"):
        ohlcv.get_indicators("BTC-USD", "foo", "2026-03-10", 5)
    assert klines == []
