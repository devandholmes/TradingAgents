"""Trading guard and service: every rejection path, rounding to filters, kill switch, daily cap, audit log.

Binance is an ``httpx.MockTransport`` fake; state and audit files live in tmp_path.
"""
import asyncio
import datetime
import json

import httpx
import pytest
from pydantic import ValidationError

from tests.test_trading_client import BTC_INFO
from tradingagents.trading import BinanceError, OrderRequest, TradeRejected
from tradingagents.trading.client import SpotClient, SymbolRules
from tradingagents.trading.guard import Guard, Limits, order_params
from tradingagents.trading.service import TradingService

RULES = SymbolRules.parse(BTC_INFO)


@pytest.fixture(scope="module")
def run():
    # One event loop for the module, made before the per-test network block (see test_web_market).
    with asyncio.Runner() as runner:
        runner.get_loop()
        yield runner.run


class FakeBinance:
    """Answers the Spot endpoints the service uses and records every request."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.order_status = "FILLED"
        self.order_error: dict | None = None
        self.price = 100_000.0

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        params = dict(request.url.params)
        if path == "/api/v3/exchangeInfo":
            return httpx.Response(200, json={"symbols": [BTC_INFO]})
        if path == "/api/v3/ticker/price":
            return httpx.Response(200, json={"symbol": params["symbol"], "price": str(self.price)})
        if path == "/api/v3/order" and method == "POST":
            if self.order_error:
                return httpx.Response(400, json=self.order_error)
            filled = self.order_status == "FILLED"
            quote = params.get("quoteOrderQty") or str(float(params["quantity"]) * self.price)
            return httpx.Response(200, json={
                "symbol": params["symbol"], "orderId": 42, "transactTime": 1_700_000_000_000,
                "price": params.get("price", "0.00000000"), "origQty": params.get("quantity", "0.0002"),
                "executedQty": "0.0002" if filled else "0", "cummulativeQuoteQty": quote if filled else "0",
                "status": self.order_status, "type": params["type"], "side": params["side"],
            })
        if path == "/api/v3/order" and method == "DELETE":
            return httpx.Response(200, json={
                "symbol": params["symbol"], "orderId": int(params["orderId"]), "price": "90000.00",
                "origQty": "0.001", "executedQty": "0", "cummulativeQuoteQty": "0",
                "status": "CANCELED", "type": "LIMIT", "side": "BUY", "transactTime": 1_700_000_000_000,
            })
        if path == "/api/v3/account/commission":
            return httpx.Response(200, json={
                "symbol": params["symbol"],
                "standardCommission": {"maker": "0.00100000", "taker": "0.00100000", "buyer": "0", "seller": "0"},
                "taxCommission": {"maker": "0", "taker": "0", "buyer": "0", "seller": "0"},
                "specialCommission": {"maker": "0", "taker": "0", "buyer": "0", "seller": "0"},
                "discount": {"enabledForAccount": True, "enabledForSymbol": True,
                             "discountAsset": "BNB", "discount": "0.75000000"},
            })
        if path == "/api/v3/openOrders":
            return httpx.Response(200, json=[{
                "symbol": "BTCUSDT", "orderId": 7, "price": "90000.00", "origQty": "0.001",
                "executedQty": "0", "cummulativeQuoteQty": "0", "status": "NEW", "type": "LIMIT",
                "side": "BUY", "time": 1_700_000_000_000,
            }])
        if path == "/api/v3/account":
            return httpx.Response(200, json={"updateTime": 1_700_000_000_000, "balances": [
                {"asset": "USDT", "free": "10000.0", "locked": "0.0"},
                {"asset": "BTC", "free": "1.0", "locked": "0.5"},
                {"asset": "XRP", "free": "0.0", "locked": "0.0"},
            ]})
        return httpx.Response(404, json={"code": -1, "msg": f"unexpected {path}"})


def make_service(tmp_path, fake=None, *, enabled=True, keys=True, warm=True, limits=None):
    """A TradingService on a fake Binance; ``warm`` preloads the BTCUSDT filters as the refresher would."""
    fake = fake or FakeBinance()
    http = httpx.AsyncClient(base_url="https://testnet.binance.vision", transport=httpx.MockTransport(fake))
    client = SpotClient(http, "key" if keys else "", "secret" if keys else "")
    if warm:
        client.rules["BTCUSDT"] = RULES
    guard = Guard("testnet", limits or Limits(), tmp_path / "trading_state.json", tmp_path / "trades.jsonl")
    guard.enabled = enabled
    return TradingService("testnet", client, guard), fake


def audit(tmp_path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "trades.jsonl").read_text().splitlines()]


BUY_20 = OrderRequest(symbol="btcusdt", side="BUY", type="MARKET", quote_amount=20)


# --- OrderRequest ---------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("fields", [
    {"type": "MARKET"},                                     # neither amount
    {"type": "MARKET", "quantity": 1, "quote_amount": 5},   # both
    {"type": "LIMIT", "quote_amount": 5, "price": 1},       # quote_amount on LIMIT
    {"type": "LIMIT", "quantity": 1},                       # LIMIT without price
    {"type": "MARKET", "quantity": 1, "price": 1},          # MARKET with price
    {"type": "MARKET", "quantity": -1},
])
def test_order_request_rejects_inconsistent_fields(fields):
    with pytest.raises(ValidationError):
        OrderRequest(symbol="BTCUSDT", side="BUY", **fields)


# --- rounding and filters -------------------------------------------------------

@pytest.mark.unit
def test_quantity_floors_to_step_and_limit_price_rounds_to_safe_side():
    buy = OrderRequest(symbol="BTCUSDT", side="BUY", type="LIMIT", quantity=0.0012345, price=90000.019)
    assert order_params(buy, RULES, buy.price) == {
        "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "quantity": "0.00123",
        "price": "90000.01", "timeInForce": "GTC",
    }
    sell = OrderRequest(symbol="BTCUSDT", side="SELL", type="LIMIT", quantity=0.001, price=90000.011)
    assert order_params(sell, RULES, sell.price)["price"] == "90000.02"


@pytest.mark.unit
def test_market_quantity_and_quote_amount_rounding():
    by_qty = OrderRequest(symbol="BTCUSDT", side="SELL", type="MARKET", quantity=0.000129)
    assert order_params(by_qty, RULES, 100_000) == {
        "symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "quantity": "0.00012",
    }
    by_quote = OrderRequest(symbol="BTCUSDT", side="BUY", type="MARKET", quote_amount=12.123456789)
    assert order_params(by_quote, RULES, 0)["quoteOrderQty"] == "12.12345678"


@pytest.mark.unit
@pytest.mark.parametrize("fields, price, reason", [
    ({"type": "MARKET", "quantity": 0.000001}, 100_000, "below the 0.00001 minimum"),
    ({"type": "MARKET", "quantity": 101}, 100_000, "above the 100 maximum"),
    ({"type": "MARKET", "quantity": 0.00004}, 100_000, "order value 4 is below the 5 minimum"),
    ({"type": "MARKET", "quote_amount": 4.99}, 0, "below the 5 minimum"),
    ({"type": "LIMIT", "quantity": 0.001, "price": 2_000_000}, 2_000_000, "price range"),
])
def test_filters_reject_what_binance_would(fields, price, reason):
    with pytest.raises(TradeRejected, match=reason):
        order_params(OrderRequest(symbol="BTCUSDT", side="BUY", **fields), RULES, price)


@pytest.mark.unit
def test_min_notional_skipped_for_market_when_not_applied():
    notional = {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": False}
    rules = SymbolRules.parse({**BTC_INFO, "filters": [*BTC_INFO["filters"][:3], notional]})
    req = OrderRequest(symbol="BTCUSDT", side="BUY", type="MARKET", quantity=0.00001)
    assert order_params(req, rules, 100_000)["quantity"] == "0.00001"


# --- every guard rejection path: audited, nothing sent ---------------------------

@pytest.mark.unit
@pytest.mark.parametrize("setup, req, reason", [
    ({"enabled": False}, BUY_20, "trading disabled"),
    ({"keys": False}, BUY_20, "API keys not configured"),
    ({}, OrderRequest(symbol="DOGEUSDT", side="BUY", type="MARKET", quote_amount=20), "DOGEUSDT is not an allowed"),
    ({}, OrderRequest(symbol="BTCUSDT", side="BUY", type="MARKET", quote_amount=51), "per-order limit"),
    ({"limits": Limits(daily_cap_usdt=15)}, BUY_20, "daily cap"),
    ({}, OrderRequest(symbol="BTCUSDT", side="BUY", type="MARKET", quote_amount=4), "below the 5 minimum"),
])
def test_rejections_are_audited_and_never_reach_binance(run, tmp_path, setup, req, reason):
    service, fake = make_service(tmp_path, **setup)
    with pytest.raises(TradeRejected, match=reason):
        run(service.place_order(req, "agent"))
    assert "/api/v3/order" not in fake.paths()
    [entry] = audit(tmp_path)
    assert entry["status"] == "rejected" and reason in entry["reason"]
    assert entry["source"] == "agent" and entry["mode"] == "testnet"
    assert entry["request"]["symbol"] == req.symbol
    assert service.guard.used_today == 0


@pytest.mark.unit
def test_kill_switch_is_checked_first(run, tmp_path):
    service, _ = make_service(tmp_path, enabled=False, keys=False)
    big = OrderRequest(symbol="DOGEUSDT", side="BUY", type="MARKET", quote_amount=10_000)
    with pytest.raises(TradeRejected, match="trading disabled"):
        run(service.place_order(big, "user"))


# --- accepted orders --------------------------------------------------------------

@pytest.mark.unit
def test_market_order_with_warm_cache_costs_exactly_one_round_trip(run, tmp_path):
    service, fake = make_service(tmp_path)
    order = run(service.place_order(BUY_20, "user"))
    assert fake.paths() == ["/api/v3/order"]
    assert dict(fake.requests[0].url.params)["quoteOrderQty"] == "20"
    assert order["status"] == "FILLED" and order["order_id"] == 42
    assert order["price"] == pytest.approx(100_000)   # average fill
    [entry] = audit(tmp_path)
    assert entry["status"] == "filled" and entry["notional"] == 20 and entry["order"]["order_id"] == 42
    assert service.guard.used_today == 20


@pytest.mark.unit
def test_market_order_by_quantity_prices_from_the_lookup(run, tmp_path):
    service, fake = make_service(tmp_path)
    service.price_lookup = {"BTCUSDT": 100_000.0}.get
    run(service.place_order(OrderRequest(symbol="BTCUSDT", side="BUY", type="MARKET", quantity=0.0002), "user"))
    assert fake.paths() == ["/api/v3/order"]
    assert audit(tmp_path)[0]["notional"] == pytest.approx(20)


@pytest.mark.unit
def test_cold_caches_fall_back_to_rest(run, tmp_path):
    service, fake = make_service(tmp_path, warm=False)
    run(service.place_order(OrderRequest(symbol="BTCUSDT", side="BUY", type="MARKET", quantity=0.0002), "user"))
    assert fake.paths() == ["/api/v3/ticker/price", "/api/v3/exchangeInfo", "/api/v3/order"]


@pytest.mark.unit
def test_limit_order_is_audited_as_new(run, tmp_path):
    fake = FakeBinance()
    fake.order_status = "NEW"
    service, _ = make_service(tmp_path, fake)
    req = OrderRequest(symbol="BTCUSDT", side="BUY", type="LIMIT", quantity=0.0005, price=90_000)
    assert run(service.place_order(req, "user"))["status"] == "NEW"
    assert audit(tmp_path)[0]["status"] == "new"
    assert service.guard.used_today == pytest.approx(45)


@pytest.mark.unit
def test_binance_rejection_is_audited_as_error_and_releases_the_cap(run, tmp_path):
    fake = FakeBinance()
    fake.order_error = {"code": -2010, "msg": "Account has insufficient balance."}
    service, _ = make_service(tmp_path, fake)
    with pytest.raises(BinanceError):
        run(service.place_order(BUY_20, "user"))
    [entry] = audit(tmp_path)
    assert entry["status"] == "error" and "insufficient balance" in entry["reason"]
    assert service.guard.used_today == 0


@pytest.mark.unit
def test_unanswered_order_keeps_its_notional_counted(run, tmp_path):
    def handler(request):
        raise httpx.ReadTimeout("timed out")

    service, _ = make_service(tmp_path, handler)
    with pytest.raises(BinanceError):
        run(service.place_order(BUY_20, "user"))
    assert audit(tmp_path)[0]["status"] == "error"
    assert audit(tmp_path)[0]["notional"] == 20
    assert service.guard.used_today == 20
    # A restart still counts it.
    reseeded = Guard("testnet", Limits(), tmp_path / "trading_state.json", tmp_path / "trades.jsonl")
    assert reseeded.used_today == 20


@pytest.mark.unit
def test_limit_price_far_from_the_market_is_rejected_locally(run, tmp_path):
    rules = SymbolRules.parse({**BTC_INFO, "filters": [*BTC_INFO["filters"], {
        "filterType": "PERCENT_PRICE_BY_SIDE", "bidMultiplierUp": "1.2", "bidMultiplierDown": "0.5",
        "askMultiplierUp": "2", "askMultiplierDown": "0.8", "avgPriceMins": 5}]})
    far = OrderRequest(symbol="BTCUSDT", side="BUY", type="LIMIT", quantity=0.001, price=40_000)
    with pytest.raises(TradeRejected, match="too far from the market"):
        order_params(far, rules, 40_000, reference=100_000)
    near = far.model_copy(update={"price": 60_000})
    assert order_params(near, rules, 60_000, reference=100_000)["price"] == "60000"
    # A cold price cache leaves the band to Binance instead of adding a round trip.
    assert order_params(far, rules, 40_000, reference=None)["price"] == "40000"

    service, fake = make_service(tmp_path)
    service.client.rules["BTCUSDT"] = rules
    service.price_lookup = lambda symbol: 100_000.0
    with pytest.raises(TradeRejected, match="too far"):
        run(service.place_order(far, "agent"))
    assert "/api/v3/order" not in fake.paths()


@pytest.mark.unit
def test_failed_audit_write_still_returns_the_executed_order(run, tmp_path, monkeypatch):
    service, _ = make_service(tmp_path)

    def broken_open(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(type(service.guard.audit_path), "open", broken_open)
    order = run(service.place_order(BUY_20, "user"))
    assert order["status"] == "FILLED"
    assert service.recent_trades(1)[0]["status"] == "filled"


@pytest.mark.unit
def test_daily_cap_accumulates_across_orders(run, tmp_path):
    service, _ = make_service(tmp_path, limits=Limits(daily_cap_usdt=50))
    run(service.place_order(BUY_20, "user"))
    run(service.place_order(BUY_20, "agent"))
    with pytest.raises(TradeRejected, match="daily cap"):
        run(service.place_order(BUY_20, "user"))
    assert [e["status"] for e in audit(tmp_path)] == ["filled", "filled", "rejected"]
    assert service.recent_trades(2)[0]["status"] == "rejected"   # newest first


# --- kill switch and seeding ------------------------------------------------------

@pytest.mark.unit
def test_kill_switch_defaults_off_and_persists(tmp_path):
    state, log = tmp_path / "trading_state.json", tmp_path / "trades.jsonl"
    guard = Guard("testnet", Limits(), state, log)
    assert guard.enabled is False
    guard.set_enabled(True)
    assert json.loads(state.read_text()) == {"enabled": True}
    assert Guard("testnet", Limits(), state, log).enabled is True
    guard.set_enabled(False)
    assert Guard("testnet", Limits(), state, log).enabled is False


@pytest.mark.unit
def test_corrupt_state_file_means_disabled(tmp_path):
    state = tmp_path / "trading_state.json"
    state.write_text("{not json")
    assert Guard("testnet", Limits(), state, tmp_path / "trades.jsonl").enabled is False


@pytest.mark.unit
def test_daily_cap_is_seeded_from_todays_accepted_entries_in_this_mode(tmp_path):
    now = datetime.datetime.now(datetime.UTC)
    today, yesterday = now.isoformat(), (now - datetime.timedelta(days=1)).isoformat()
    lines = [
        {"ts": today, "mode": "testnet", "status": "filled", "notional": 20},
        {"ts": today, "mode": "testnet", "status": "new", "notional": 15.5},
        {"ts": today, "mode": "testnet", "status": "rejected", "notional": 99},
        {"ts": today, "mode": "live", "status": "filled", "notional": 40},
        {"ts": yesterday, "mode": "testnet", "status": "filled", "notional": 30},
    ]
    log = tmp_path / "trades.jsonl"
    log.write_text("\n".join(json.dumps(line) for line in lines) + "\nnot json\n")
    guard = Guard("testnet", Limits(), tmp_path / "trading_state.json", log)
    assert guard.used_today == pytest.approx(35.5)
    assert len(guard.recent(50)) == 5


# --- reads and configuration ------------------------------------------------------

@pytest.mark.unit
def test_account_orders_and_cancel_shapes(run, tmp_path):
    service, _ = make_service(tmp_path, enabled=False)   # reads and cancels ignore the kill switch
    assert run(service.account()) == {"mode": "testnet", "update_time": 1_700_000_000_000, "balances": [
        {"asset": "USDT", "free": 10000.0, "locked": 0.0}, {"asset": "BTC", "free": 1.0, "locked": 0.5}]}
    assert run(service.open_orders("btcusdt")) == [{
        "symbol": "BTCUSDT", "order_id": 7, "side": "BUY", "type": "LIMIT", "price": 90000.0,
        "orig_qty": 0.001, "executed_qty": 0.0, "quote_qty": 0.0, "status": "NEW", "time": 1_700_000_000_000,
    }]
    assert run(service.cancel_order("BTCUSDT", 7, "user"))["status"] == "CANCELED"


@pytest.mark.unit
def test_reads_without_keys_are_rejected(run, tmp_path):
    service, fake = make_service(tmp_path, keys=False)
    with pytest.raises(TradeRejected, match="not configured"):
        run(service.account())
    assert fake.requests == []


@pytest.mark.unit
def test_limits_from_env(monkeypatch):
    monkeypatch.setenv("TRADING_MAX_ORDER_USDT", "25")
    monkeypatch.setenv("TRADING_DAILY_CAP_USDT", "")
    monkeypatch.setenv("TRADING_ALLOWED_SYMBOLS", " btcusdt, ethusdt ,")
    assert Limits.from_env() == Limits(max_order_usdt=25, daily_cap_usdt=200, allowed_symbols=("BTCUSDT", "ETHUSDT"))


@pytest.mark.unit
@pytest.mark.parametrize("value, mode, url", [
    ("", "testnet", "https://testnet.binance.vision"),
    ("LIVE", "live", "https://api.binance.com"),
    ("production", "testnet", "https://testnet.binance.vision"),
])
def test_live_only_when_mode_says_live(run, tmp_path, monkeypatch, value, mode, url):
    monkeypatch.setenv("TRADING_MODE", value)
    service = TradingService.from_env(tmp_path)
    assert service.mode == mode and str(service.client._http.base_url).rstrip("/") == url
    assert service.client.configured is False
    run(service.close())
