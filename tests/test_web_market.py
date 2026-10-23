"""Dashboard market API: klines, trends, indicators and error mapping.

The Binance client functions are replaced with fakes, so no request leaves the process.
"""
import pytest
from fastapi.testclient import TestClient

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.web import market
from tradingagents.web.app import app


@pytest.fixture(scope="module")
def http():
    # Started once, before the per-test network block: on Windows each event loop
    # opens a local socketpair, which that block would refuse.
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client


def _candles(closes):
    return [
        {"time": 1700000000 + i * 3600, "open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 10.0}
        for i, c in enumerate(closes)
    ]


def _ticker(symbol, change, quote_volume):
    return {
        "symbol": symbol, "lastPrice": 1.0, "priceChangePercent": change,
        "highPrice": 1.0, "lowPrice": 1.0, "volume": 1.0, "quoteVolume": quote_volume,
    }


@pytest.mark.unit
def test_health(http):
    assert http.get("/api/health").json() == {"ok": True}


@pytest.mark.unit
def test_klines_passes_query_through(http, monkeypatch):
    calls = []

    def fake(symbol, interval, limit):
        calls.append((symbol, interval, limit))
        return _candles([1.0])

    monkeypatch.setattr(market.client, "get_klines", fake)
    response = http.get("/api/klines", params={"symbol": "BTCUSDT", "interval": "1h", "limit": 5})

    assert response.status_code == 200
    assert response.json() == _candles([1.0])
    assert calls == [("BTCUSDT", "1h", 5)]


@pytest.mark.unit
@pytest.mark.parametrize(
    "params",
    [
        {"symbol": "BTC/USDT"},
        {"symbol": "BTCUSDT", "interval": "7m"},
        {"symbol": "BTCUSDT", "limit": 5000},
    ],
)
def test_klines_rejects_bad_input(http, params):
    assert http.get("/api/klines", params=params).status_code == 422


@pytest.mark.unit
def test_trends_filters_and_sorts(http, monkeypatch):
    tickers = [
        _ticker("AAAUSDT", 5.0, 100.0),
        _ticker("BBBUSDT", -8.0, 900.0),
        _ticker("CCCUSDT", 12.0, 50.0),
        _ticker("JUPUSDT", 1.0, 10.0),      # real coin ending in UP: kept
        _ticker("BTCUPUSDT", 50.0, 999.0),  # leveraged token: skipped
        _ticker("ETHDOWNUSDT", -50.0, 999.0),
        _ticker("DEADUSDT", -90.0, 0.0),    # halted: skipped
        _ticker("AAABTC", 99.0, 999.0),     # other quote: skipped
    ]
    monkeypatch.setattr(market.client, "get_24h_tickers", lambda symbols=None: tickers)
    body = http.get("/api/trends", params={"limit": 2}).json()

    assert [t["symbol"] for t in body["gainers"]] == ["CCCUSDT", "AAAUSDT"]
    assert [t["symbol"] for t in body["losers"]] == ["BBBUSDT", "JUPUSDT"]
    assert [t["symbol"] for t in body["volume"]] == ["BBBUSDT", "AAAUSDT"]
    assert set(body["gainers"][0]) == {"symbol", "lastPrice", "priceChangePercent", "quoteVolume"}


@pytest.mark.unit
@pytest.mark.parametrize(
    "closes, trend",
    [
        ([100.0 + i for i in range(200)], "up"),
        ([300.0 - i for i in range(200)], "down"),
        ([100.0 + (i % 2) for i in range(200)], "sideways"),
    ],
)
def test_indicators_trend(http, monkeypatch, closes, trend):
    monkeypatch.setattr(market.client, "get_klines", lambda symbol, interval, limit: _candles(closes))
    response = http.get("/api/indicators", params={"symbol": "btcusdt", "interval": "1h"})

    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "BTCUSDT"
    assert body["interval"] == "1h"
    assert body["price"] == closes[-1]
    assert body["trend"] == trend
    for key in ("rsi14", "macd", "macd_signal", "macd_hist", "sma20", "sma50", "ema20"):
        assert isinstance(body[key], float)
    assert body["sma20"] == pytest.approx(sum(closes[-20:]) / 20)


@pytest.mark.unit
def test_market_snapshot_shares_one_candle_fetch(monkeypatch):
    fetches = []

    def fake_klines(symbol, interval, limit):
        fetches.append(symbol)
        return _candles([100.0 + i for i in range(60)])

    monkeypatch.setattr(market.client, "get_klines", fake_klines)
    monkeypatch.setattr(market.client, "get_24h_tickers", lambda symbols=None: [_ticker("ETHUSDT", 1.0, 5.0)])
    snapshot = market.market_snapshot("ethusdt", "4h", candles=24)

    assert fetches == ["ETHUSDT"]
    assert snapshot["ticker"]["symbol"] == "ETHUSDT"
    assert snapshot["indicators"]["interval"] == "4h"
    assert len(snapshot["candles"]) == 24


@pytest.mark.unit
def test_no_candles_is_404(http, monkeypatch):
    monkeypatch.setattr(market.client, "get_klines", lambda symbol, interval, limit: [])
    assert http.get("/api/indicators", params={"symbol": "NEWUSDT"}).status_code == 404


@pytest.mark.unit
def test_vendor_failure_is_502(http, monkeypatch):
    def down(*args, **kwargs):
        raise VendorUnavailableError("Binance /api/v3/klines returned HTTP 400: Invalid symbol.")

    monkeypatch.setattr(market.client, "get_klines", down)
    response = http.get("/api/klines", params={"symbol": "NOPEUSDT"})

    assert response.status_code == 502
    assert "Invalid symbol" in response.json()["detail"]
