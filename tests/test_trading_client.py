"""Signed Binance Spot client: signing, time offset, -1021 retry, filters parsing and redaction.

Requests go to an ``httpx.MockTransport``, so nothing leaves the process.
"""
import asyncio
from decimal import Decimal
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from tradingagents.trading.client import BinanceError, SpotClient, SymbolRules, sign

# Binance's documented HMAC example (rest-api.md, "SIGNED endpoint examples").
DOC_SECRET = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
DOC_PAYLOAD = "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"
DOC_SIGNATURE = "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"

KEY, SECRET = "test-key-123", "test-secret-456"

BTC_INFO = {
    "symbol": "BTCUSDT",
    "quoteAssetPrecision": 8,
    "filters": [
        {"filterType": "PRICE_FILTER", "minPrice": "0.01000000", "maxPrice": "1000000.00000000", "tickSize": "0.01000000"},
        {"filterType": "LOT_SIZE", "minQty": "0.00001000", "maxQty": "9000.00000000", "stepSize": "0.00001000"},
        {"filterType": "MARKET_LOT_SIZE", "minQty": "0.00000000", "maxQty": "100.00000000", "stepSize": "0.00000000"},
        {"filterType": "NOTIONAL", "minNotional": "5.00000000", "applyMinToMarket": True,
         "maxNotional": "9000000.00000000", "applyMaxToMarket": False, "avgPriceMins": 5},
    ],
}


@pytest.fixture(scope="module")
def run():
    # One event loop for the module, made before the per-test network block (see test_web_market).
    with asyncio.Runner() as runner:
        runner.get_loop()
        yield runner.run


def _client(handler, key=KEY, secret=SECRET) -> SpotClient:
    http = httpx.AsyncClient(base_url="https://testnet.binance.vision", transport=httpx.MockTransport(handler))
    return SpotClient(http, key, secret)


def _split(request: httpx.Request) -> tuple[str, str]:
    """The signed part of the sent query and its signature."""
    unsigned, signature = urlsplit(str(request.url)).query.rsplit("&signature=", 1)
    return unsigned, signature


@pytest.mark.unit
def test_signature_matches_binance_documented_example():
    assert sign(DOC_PAYLOAD, DOC_SECRET) == DOC_SIGNATURE


@pytest.mark.unit
def test_signed_request_sends_key_header_and_signs_the_sent_query(run):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"balances": []})

    run(_client(handler).account())

    assert seen[0].headers["X-MBX-APIKEY"] == KEY
    unsigned, signature = _split(seen[0])
    assert signature == sign(unsigned, SECRET)
    params = dict(parse_qsl(unsigned))
    assert params["omitZeroBalances"] == "true" and params["recvWindow"] == "5000"


@pytest.mark.unit
def test_percent_encoded_query_is_what_gets_signed(run):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[])

    run(_client(handler).signed("GET", "/api/v3/openOrders", {"symbol": "１２３"}))
    unsigned, signature = _split(seen[0])
    assert unsigned.startswith("symbol=%EF%BC%91%EF%BC%92%EF%BC%93&")
    assert signature == sign(unsigned, SECRET)


@pytest.mark.unit
def test_timestamp_carries_the_cached_server_offset(run, monkeypatch):
    monkeypatch.setattr("tradingagents.trading.client.time.time", lambda: 1000.0)
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": 1_003_000})
        return httpx.Response(200, json={"balances": []})

    client = _client(handler)
    run(client.sync_time())
    assert client.time_offset_ms == 3000
    run(client.account())
    assert dict(parse_qsl(_split(seen[-1])[0]))["timestamp"] == "1003000"


@pytest.mark.unit
def test_invalid_timestamp_resyncs_and_retries_once(run):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": 0})
        if paths.count("/api/v3/order") == 1:
            return httpx.Response(400, json={"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."})
        return httpx.Response(200, json={"orderId": 1, "status": "NEW"})

    assert run(_client(handler).new_order({"symbol": "BTCUSDT"}))["orderId"] == 1
    assert paths == ["/api/v3/order", "/api/v3/time", "/api/v3/order"]


@pytest.mark.unit
def test_invalid_timestamp_twice_raises(run):
    def handler(request):
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": 0})
        return httpx.Response(400, json={"code": -1021, "msg": "outside of the recvWindow"})

    with pytest.raises(BinanceError) as caught:
        run(_client(handler).new_order({"symbol": "BTCUSDT"}))
    assert caught.value.code == -1021


@pytest.mark.unit
def test_errors_never_carry_key_secret_or_signature(run):
    def handler(request):
        raise httpx.ConnectError(f"failed {request.url} {KEY} {SECRET}")

    with pytest.raises(BinanceError) as caught:
        run(_client(handler).account())
    message = str(caught.value)
    assert KEY not in message and SECRET not in message
    assert "signature=***" in message
    assert caught.value.status is None


@pytest.mark.unit
def test_binance_error_body_is_surfaced(run):
    client = _client(lambda r: httpx.Response(400, json={"code": -2010, "msg": "Account has insufficient balance."}))
    with pytest.raises(BinanceError) as caught:
        run(client.new_order({"symbol": "BTCUSDT"}))
    assert caught.value.code == -2010 and caught.value.status == 400
    assert "insufficient balance" in str(caught.value)


@pytest.mark.unit
def test_signed_call_without_keys_is_refused_locally(run):
    calls = []
    client = _client(lambda r: calls.append(r) or httpx.Response(200, json={}), key="", secret="")
    with pytest.raises(BinanceError):
        run(client.account())
    assert calls == []


@pytest.mark.unit
def test_symbol_rules_are_fetched_once_then_cached(run):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"symbols": [BTC_INFO]})

    client = _client(handler)
    run(client.symbol_rules("BTCUSDT"))
    run(client.symbol_rules("BTCUSDT"))
    assert len(calls) == 1
    assert dict(calls[0].url.params) == {"symbols": '["BTCUSDT"]'}
    assert "X-MBX-APIKEY" not in calls[0].headers


@pytest.mark.unit
def test_symbol_rules_parse_filters():
    rules = SymbolRules.parse(BTC_INFO)
    assert rules.step == Decimal("0.00001") and rules.tick == Decimal("0.01")
    # A zeroed MARKET_LOT_SIZE step falls back to LOT_SIZE; its max still binds.
    assert rules.market_step == Decimal("0.00001")
    assert rules.market_min_qty == Decimal("0.00001") and rules.market_max_qty == Decimal("100")
    assert rules.min_notional == Decimal("5") and rules.min_notional_market is True
    assert rules.quote_precision == 8


@pytest.mark.unit
def test_symbol_rules_parse_legacy_min_notional():
    info = {**BTC_INFO, "filters": [{"filterType": "MIN_NOTIONAL", "minNotional": "10", "applyToMarket": False}]}
    rules = SymbolRules.parse(info)
    assert rules.min_notional == Decimal("10") and rules.min_notional_market is False
