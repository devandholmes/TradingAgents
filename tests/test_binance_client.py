"""Binance public market-data client: symbol mapping, parsing, and error reporting.

``requests.get`` is mocked, so these run without a network connection.
"""
import json
from unittest import mock

import pytest
import requests

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors.binance import client


def _response(payload, status=200):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(payload).encode()
    return response


@pytest.mark.unit
@pytest.mark.parametrize(
    "ticker, expected",
    [
        ("BTC-USD", "BTCUSDT"),
        ("BTCUSD", "BTCUSDT"),
        ("BTC-USDT", "BTCUSDT"),
        ("btcusdt", "BTCUSDT"),
        ("eth-usdc", "ETHUSDT"),
        ("AAPL", None),
        ("EURUSD", None),
    ],
)
def test_to_binance_symbol(ticker, expected):
    assert client.to_binance_symbol(ticker) == expected


@pytest.mark.unit
def test_get_klines_parses_rows_and_passes_params():
    row = [1700000000000, "100.5", "110", "99", "105.25", "12.5", 1700000059999, "1300", 42, "6", "630", "0"]
    with mock.patch.object(client.requests, "get", return_value=_response([row])) as get:
        candles = client.get_klines("btcusdt", "1m", limit=3, start_ms=1, end_ms=2)

    assert candles == [
        {"time": 1700000000, "open": 100.5, "high": 110.0, "low": 99.0, "close": 105.25, "volume": 12.5}
    ]
    url, kwargs = get.call_args.args[0], get.call_args.kwargs
    assert url == f"{client.BASE_URL}/api/v3/klines"
    assert kwargs["params"] == {
        "symbol": "BTCUSDT", "interval": "1m", "limit": 3, "startTime": 1, "endTime": 2,
    }
    assert kwargs["timeout"] == client.REQUEST_TIMEOUT


@pytest.mark.unit
def test_get_24h_tickers_converts_fields_and_encodes_symbols():
    raw = {
        "symbol": "BTCUSDT", "lastPrice": "65000.1", "priceChangePercent": "-1.25",
        "highPrice": "66000", "lowPrice": "64000", "volume": "1000", "quoteVolume": "65000000",
        "openPrice": "65800",  # extra fields are dropped
    }
    with mock.patch.object(client.requests, "get", return_value=_response([raw])) as get:
        tickers = client.get_24h_tickers(["btcusdt", "ETHUSDT"])

    assert tickers == [{
        "symbol": "BTCUSDT", "lastPrice": 65000.1, "priceChangePercent": -1.25,
        "highPrice": 66000.0, "lowPrice": 64000.0, "volume": 1000.0, "quoteVolume": 65000000.0,
    }]
    assert get.call_args.kwargs["params"] == {"symbols": '["BTCUSDT","ETHUSDT"]'}


@pytest.mark.unit
def test_get_24h_tickers_without_symbols_requests_all():
    with mock.patch.object(client.requests, "get", return_value=_response([])) as get:
        assert client.get_24h_tickers() == []
    assert get.call_args.kwargs["params"] is None


@pytest.mark.unit
def test_http_error_raises_vendor_unavailable_with_binance_message():
    bad = _response({"code": -1121, "msg": "Invalid symbol."}, status=400)
    with mock.patch.object(client.requests, "get", return_value=bad), \
            pytest.raises(VendorUnavailableError, match=r"HTTP 400.*Invalid symbol"):
        client.get_klines("NOPE")


@pytest.mark.unit
def test_connection_error_raises_vendor_unavailable():
    with mock.patch.object(client.requests, "get", side_effect=requests.ConnectionError("down")), \
            pytest.raises(VendorUnavailableError, match="request failed"):
        client.get_24h_tickers()
