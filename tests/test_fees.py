"""Fees: paper fee config, commission parsing, LIMIT_MAKER guarding, commission rates and maker-first.

Binance is an ``httpx.MockTransport`` fake (see test_trading_guard); no order leaves the process.
"""
import asyncio
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from tests.test_trading_client import BTC_INFO
from tests.test_trading_guard import RULES, FakeBinance, audit, make_service
from tradingagents.trading import OrderRequest, TradeRejected, service as service_module
from tradingagents.trading.client import BinanceError, SymbolRules
from tradingagents.trading.fees import commissions, fee_columns, maker_wait_s, net_fill, paper_fee
from tradingagents.trading.guard import Limits, order_params
from tradingagents.trading.service import order_view

BID, ASK = 100_000.0, 100_001.0


@pytest.fixture(scope="module")
def run():
    with asyncio.Runner() as runner:
        runner.get_loop()
        yield runner.run


@pytest.fixture(autouse=True)
def _no_poll_wait(monkeypatch):
    monkeypatch.setattr(service_module, "POLL_SECONDS", 0)


# --- paper fee and maker wait -------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize(("value", "expected"), [
    (None, 0.001), ("0.00075", 0.00075), ("0", 0.0), ("abc", 0.001), ("-0.1", 0.001), ("0.5", 0.001),
])
def test_paper_fee_from_env(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("TRADING_PAPER_FEE", raising=False)
    else:
        monkeypatch.setenv("TRADING_PAPER_FEE", value)
    assert paper_fee() == expected


@pytest.mark.unit
def test_maker_wait_from_env(monkeypatch):
    monkeypatch.delenv("TRADING_MAKER_WAIT_S", raising=False)
    assert maker_wait_s() == 15.0
    monkeypatch.setenv("TRADING_MAKER_WAIT_S", "3")
    assert maker_wait_s() == 3.0


# --- commission parsing ---------------------------------------------------------------------------

@pytest.mark.unit
def test_commissions_sum_per_asset_including_bnb():
    fills = [
        {"price": "4000", "qty": "1", "commission": "0.00010000", "commissionAsset": "BNB"},
        {"price": "3999", "qty": "5", "commission": "0.00020000", "commissionAsset": "BNB"},
        {"price": "3998", "qty": "2", "commission": "7.996", "commissionAsset": "USDT"},
    ]
    assert commissions(fills) == {"BNB": pytest.approx(0.0003), "USDT": pytest.approx(7.996)}
    assert commissions([]) == {}


@pytest.mark.unit
def test_fee_columns_single_mixed_and_none():
    assert fee_columns({"BNB": 0.0003}) == (0.0003, "BNB")
    assert fee_columns({"USDT": 1.0, "BNB": 0.1}) == (None, "BNB,USDT")
    assert fee_columns({}) == (0.0, None)


@pytest.mark.unit
def test_net_fill_takes_the_fee_off_the_received_asset_only():
    assert net_fill("BUY", 1.0, 100.0, {"BTC": 0.001}, "BTC", "USDT") == (0.999, 100.0)
    assert net_fill("SELL", 1.0, 100.0, {"USDT": 0.1}, "BTC", "USDT") == (1.0, pytest.approx(99.9))
    assert net_fill("BUY", 1.0, 100.0, {"BNB": 0.0002}, "BTC", "USDT") == (1.0, 100.0)


@pytest.mark.unit
def test_order_view_reads_commission_from_full_fills_only():
    raw = {"symbol": "BTCUSDT", "orderId": 1, "executedQty": "0.0002", "cummulativeQuoteQty": "20",
           "status": "FILLED", "fills": [{"commission": "0.00002", "commissionAsset": "BNB"}]}
    assert order_view(raw)["commission"] == {"BNB": pytest.approx(0.00002)}
    assert "commission" not in order_view({k: v for k, v in raw.items() if k != "fills"})


@pytest.mark.unit
def test_orders_request_full_fills(run, tmp_path):
    service, fake = make_service(tmp_path)
    run(service.place_order(OrderRequest(symbol="BTCUSDT", side="BUY", type="MARKET", quote_amount=20), "user"))
    [post] = [r for r in fake.requests if r.method == "POST"]
    assert post.url.params["newOrderRespType"] == "FULL"


@pytest.mark.unit
def test_symbol_rules_carry_the_assets():
    rules = SymbolRules.parse({**BTC_INFO, "baseAsset": "BTC", "quoteAsset": "USDT"})
    assert (rules.base_asset, rules.quote_asset) == ("BTC", "USDT")


# --- LIMIT_MAKER in the guard ---------------------------------------------------------------------

@pytest.mark.unit
def test_limit_maker_params_round_like_limit_without_time_in_force():
    buy = OrderRequest(symbol="BTCUSDT", side="BUY", type="LIMIT_MAKER", quantity=0.0012345, price=90000.019)
    assert order_params(buy, RULES, 90000.019) == {
        "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT_MAKER", "quantity": "0.00123", "price": "90000.01",
    }


@pytest.mark.unit
def test_limit_maker_needs_a_price_and_no_quote_amount():
    with pytest.raises(ValidationError):
        OrderRequest(symbol="BTCUSDT", side="BUY", type="LIMIT_MAKER", quantity=0.001)
    with pytest.raises(ValidationError):
        OrderRequest(symbol="BTCUSDT", side="BUY", type="LIMIT_MAKER", quote_amount=10, price=1)


@pytest.mark.unit
def test_limit_maker_obeys_min_notional_and_price_band():
    tiny = OrderRequest(symbol="BTCUSDT", side="BUY", type="LIMIT_MAKER", quantity=0.00002, price=90_000)
    with pytest.raises(TradeRejected, match="minimum"):
        order_params(tiny, RULES, 90_000)
    banded = replace(RULES, bid_down=Decimal("0.8"), bid_up=Decimal("1.2"))
    far = OrderRequest(symbol="BTCUSDT", side="BUY", type="LIMIT_MAKER", quantity=0.001, price=40_000)
    with pytest.raises(TradeRejected, match="too far from the market"):
        order_params(far, banded, 40_000, reference=100_000)


# --- commission rates ---------------------------------------------------------------------------

class BurnBinance(FakeBinance):
    """Live-style answers: SAPI's bnbBurn, or a failing commission endpoint."""

    def __init__(self, burn=True, fail=False):
        super().__init__()
        self.burn, self.fail = burn, fail

    def __call__(self, request):
        if self.fail and request.url.path == "/api/v3/account/commission":
            self.requests.append(request)
            return httpx.Response(500, json={"code": -1000, "msg": "boom"})
        if request.url.path == "/sapi/v1/bnbBurn":
            self.requests.append(request)
            return httpx.Response(200, json={"spotBNBBurn": self.burn, "interestBNBBurn": False})
        return super().__call__(request)


@pytest.mark.unit
def test_fees_testnet_reads_rates_once_and_leaves_bnb_unknown(run, tmp_path):
    service, fake = make_service(tmp_path)
    first = run(service.fees())
    assert first == {"maker": 0.001, "taker": 0.001, "bnb_discount": None, "source": "binance"}
    assert run(service.fees()) == first
    assert fake.paths().count("/api/v3/account/commission") == 1   # cached
    assert "/sapi/v1/bnbBurn" not in fake.paths()                    # the testnet has no SAPI


@pytest.mark.unit
@pytest.mark.parametrize("burn", [True, False])
def test_fees_live_reports_bnb_burn(run, tmp_path, burn):
    service, _ = make_service(tmp_path, BurnBinance(burn=burn))
    service.mode = "live"
    assert run(service.fees())["bnb_discount"] is burn


@pytest.mark.unit
def test_fees_fall_back_to_paper_without_keys_or_on_errors(run, tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_PAPER_FEE", "0.00075")
    unkeyed, fake = make_service(tmp_path, keys=False)
    assert run(unkeyed.fees()) == {"maker": 0.00075, "taker": 0.00075, "bnb_discount": None, "source": "paper"}
    assert fake.requests == []
    failing, _ = make_service(tmp_path, BurnBinance(fail=True))
    assert run(failing.fees())["source"] == "paper"
    assert failing._fees[0] - service_module.time.monotonic() <= service_module.FEES_RETRY


# --- maker-first ----------------------------------------------------------------------------------

class MakerBinance(FakeBinance):
    """A resting LIMIT_MAKER: ``polls`` are its statuses on each query, ``maker_filled`` what filled."""

    def __init__(self, *, polls=(), maker_filled=0.0, would_take=False):
        super().__init__()
        self.polls, self.maker_filled, self.would_take = list(polls), maker_filled, would_take
        self.price = BID
        self.maker: dict = {}

    def _order(self, status: str, executed: float) -> dict:
        return {**self.maker, "status": status, "executedQty": f"{executed:.8f}",
                "cummulativeQuoteQty": f"{executed * float(self.maker['price']):.8f}"}

    def __call__(self, request):
        path, method, params = request.url.path, request.method, dict(request.url.params)
        if path == "/api/v3/ticker/bookTicker":
            self.requests.append(request)
            return httpx.Response(200, json={"symbol": params["symbol"], "bidPrice": str(BID), "bidQty": "1",
                                             "askPrice": str(ASK), "askQty": "1"})
        if path == "/api/v3/order" and method == "POST" and params["type"] == "LIMIT_MAKER":
            self.requests.append(request)
            if self.would_take:
                return httpx.Response(400, json={"code": -2010, "msg": "Order would immediately match and take."})
            self.maker = {"symbol": params["symbol"], "orderId": 1, "side": params["side"], "type": "LIMIT_MAKER",
                          "price": params["price"], "origQty": params["quantity"], "transactTime": 1}
            return httpx.Response(200, json={**self._order("NEW", 0), "fills": []})
        if path == "/api/v3/order" and method == "GET":
            self.requests.append(request)
            status = self.polls.pop(0) if self.polls else "NEW"
            filled = float(self.maker["origQty"]) if status == "FILLED" else self.maker_filled
            return httpx.Response(200, json=self._order(status, filled))
        if path == "/api/v3/order" and method == "DELETE":
            self.requests.append(request)
            return httpx.Response(200, json=self._order("CANCELED", self.maker_filled))
        if path == "/api/v3/myTrades":
            self.requests.append(request)
            return httpx.Response(200, json=[{"orderId": 1, "commission": "0.00000750", "commissionAsset": "BNB",
                                              "isMaker": True}])
        if path == "/api/v3/order" and method == "POST":   # the MARKET finish, with FULL fills
            response = super().__call__(request)
            body = response.json()
            if body.get("status") == "FILLED":
                body["executedQty"] = params.get("quantity", body["executedQty"])
                body["fills"] = [{"price": str(self.price), "qty": body["executedQty"],
                                  "commission": "0.00001000", "commissionAsset": "BNB"}]
            return httpx.Response(response.status_code, json=body)
        return super().__call__(request)

    def posted(self) -> list[str]:
        return [r.url.params["type"] for r in self.requests if r.method == "POST"]


BUY_20 = OrderRequest(symbol="BTCUSDT", side="BUY", type="MARKET", quote_amount=20)


@pytest.mark.unit
def test_maker_first_full_fill_never_takes(run, tmp_path):
    fake = MakerBinance(polls=["NEW", "FILLED"])
    service, _ = make_service(tmp_path, fake)
    result = run(service.place_maker_first(BUY_20, wait_s=5))
    assert fake.posted() == ["LIMIT_MAKER"]
    [post] = [r for r in fake.requests if r.method == "POST"]
    assert post.url.params["price"] == "100000" and post.url.params["quantity"] == "0.0002"
    assert "timeInForce" not in post.url.params
    assert "DELETE" not in [r.method for r in fake.requests]
    assert result == {"filled_qty": pytest.approx(0.0002), "quote": pytest.approx(20),
                      "fee": pytest.approx(0.0000075), "fee_asset": "BNB",
                      "commission": {"BNB": pytest.approx(0.0000075)},
                      "maker_fill_ratio": 1.0, "order_ids": [1], "remainder_qty": 0.0}
    assert service.guard.used_today == pytest.approx(20)


@pytest.mark.unit
def test_maker_first_sell_rests_at_the_ask(run, tmp_path):
    fake = MakerBinance(polls=["FILLED"])
    service, _ = make_service(tmp_path, fake)
    run(service.place_maker_first(OrderRequest(symbol="BTCUSDT", side="SELL", type="MARKET", quantity=0.0002), 5))
    [post] = [r for r in fake.requests if r.method == "POST"]
    assert post.url.params["side"] == "SELL" and post.url.params["price"] == "100001"


@pytest.mark.unit
def test_maker_first_partial_fill_then_market_for_the_rest(run, tmp_path):
    fake = MakerBinance(maker_filled=0.0001)
    service, _ = make_service(tmp_path, fake)
    result = run(service.place_maker_first(BUY_20, wait_s=0))
    assert fake.posted() == ["LIMIT_MAKER", "MARKET"]
    market = [r for r in fake.requests if r.method == "POST"][1]
    assert market.url.params["quantity"] == "0.0001"
    assert result["filled_qty"] == pytest.approx(0.0002)
    assert result["maker_fill_ratio"] == pytest.approx(0.5)
    assert result["fee"] == pytest.approx(0.0000175) and result["fee_asset"] == "BNB"
    assert result["order_ids"] == [1, 42] and result["remainder_qty"] == pytest.approx(0)
    # The cap counts what filled on each leg, not the cancelled part twice.
    assert service.guard.used_today == pytest.approx(20)
    assert [e["status"] for e in audit(tmp_path)] == ["new", "filled"]


@pytest.mark.unit
def test_maker_first_nothing_filled_goes_all_market(run, tmp_path):
    fake = MakerBinance()
    service, _ = make_service(tmp_path, fake)
    result = run(service.place_maker_first(BUY_20, wait_s=0))
    assert fake.posted() == ["LIMIT_MAKER", "MARKET"]
    assert "/api/v3/myTrades" not in fake.paths()
    assert result["maker_fill_ratio"] == 0.0 and result["filled_qty"] == pytest.approx(0.0002)
    assert result["fee"] == pytest.approx(0.00001)


@pytest.mark.unit
def test_maker_first_would_take_goes_straight_to_market(run, tmp_path):
    fake = MakerBinance(would_take=True)
    service, _ = make_service(tmp_path, fake)
    result = run(service.place_maker_first(BUY_20, wait_s=5))
    assert fake.posted() == ["LIMIT_MAKER", "MARKET"]
    assert result["maker_fill_ratio"] == 0.0 and result["order_ids"] == [42]


@pytest.mark.unit
def test_maker_first_leaves_a_remainder_below_min_notional(run, tmp_path):
    fake = MakerBinance(maker_filled=0.00016)   # 0.00004 left = 4 USDT < the 5 USDT minimum
    service, _ = make_service(tmp_path, fake)
    result = run(service.place_maker_first(BUY_20, wait_s=0))
    assert fake.posted() == ["LIMIT_MAKER"]
    assert result["filled_qty"] == pytest.approx(0.00016) and result["maker_fill_ratio"] == 1.0
    assert result["remainder_qty"] == pytest.approx(0.00004)
    assert service.guard.used_today == pytest.approx(16)


@pytest.mark.unit
def test_maker_first_cancel_race_reads_the_final_order(run, tmp_path):
    class Raced(MakerBinance):
        def __call__(self, request):
            if request.method == "DELETE":
                self.requests.append(request)
                self.polls = ["FILLED"]
                return httpx.Response(400, json={"code": -2011, "msg": "Unknown order sent."})
            return super().__call__(request)

    fake = Raced()
    service, _ = make_service(tmp_path, fake)
    result = run(service.place_maker_first(BUY_20, wait_s=0))
    assert fake.posted() == ["LIMIT_MAKER"] and result["maker_fill_ratio"] == 1.0


@pytest.mark.unit
def test_maker_first_failed_poll_cancels_the_resting_order(run, tmp_path, monkeypatch):
    class Broken(MakerBinance):
        def __call__(self, request):
            if request.method == "GET" and request.url.path == "/api/v3/order":
                return httpx.Response(500, json={"code": -1000, "msg": "internal"})
            return super().__call__(request)

    monkeypatch.setattr(service_module, "POLL_SECONDS", 0)
    fake = Broken()
    service, _ = make_service(tmp_path, fake)
    with pytest.raises(BinanceError):
        run(service.place_maker_first(BUY_20, wait_s=5))
    assert "DELETE" in [r.method for r in fake.requests]   # never left resting
    assert fake.posted() == ["LIMIT_MAKER"]                 # and no taker leg beside it


@pytest.mark.unit
def test_maker_first_unanswered_placement_is_cancelled_by_client_id(run, tmp_path):
    class Silent(MakerBinance):
        def __call__(self, request):
            if request.method == "POST" and request.url.params.get("type") == "LIMIT_MAKER":
                self.requests.append(request)
                raise httpx.ReadTimeout("no reply", request=request)
            return super().__call__(request)

    fake = Silent()
    service, _ = make_service(tmp_path, fake)
    with pytest.raises(BinanceError):
        run(service.place_maker_first(BUY_20, wait_s=5))
    post, cancel = [r for r in fake.requests if r.url.path == "/api/v3/order"]
    client_id = post.url.params["newClientOrderId"]
    assert client_id.startswith("ta-") and len(client_id) <= 36
    assert cancel.method == "DELETE" and cancel.url.params["origClientOrderId"] == client_id
    assert fake.posted() == ["LIMIT_MAKER"]                 # no taker leg after an unanswered maker


@pytest.mark.unit
def test_maker_first_stop_cancels_at_once_and_skips_the_taker_leg(run, tmp_path):
    fake = MakerBinance(maker_filled=0.0001)
    service, _ = make_service(tmp_path, fake)
    stop = asyncio.Event()

    async def exit_fires():
        task = asyncio.create_task(service.place_maker_first(BUY_20, wait_s=60, stop=stop))
        await asyncio.sleep(0.05)
        stop.set()   # a risk exit is waiting for the bot's lock
        return await asyncio.wait_for(task, 2)

    result = run(exit_fires())
    assert "DELETE" in [r.method for r in fake.requests]
    assert fake.posted() == ["LIMIT_MAKER"]   # the rest is left for the exit, not bought
    assert result["filled_qty"] == pytest.approx(0.0001)


@pytest.mark.unit
def test_maker_first_unread_fills_model_the_fee(run, tmp_path, monkeypatch):
    class NoTrades(MakerBinance):
        def __call__(self, request):
            if request.url.path == "/api/v3/myTrades":
                return httpx.Response(500, json={"code": -1000, "msg": "internal"})
            return super().__call__(request)

    monkeypatch.setattr(service_module, "POLL_SECONDS", 0)
    service, _ = make_service(tmp_path, NoTrades(polls=["FILLED"]))
    result = run(service.place_maker_first(BUY_20, wait_s=5))
    assert result["filled_qty"] == pytest.approx(0.0002)   # the fill is still booked
    [amount] = result["commission"].values()   # in the base asset (the fixture's rules name none)
    assert amount == pytest.approx(0.0002 * paper_fee())


@pytest.mark.unit
@pytest.mark.parametrize(("kwargs", "reason"), [
    ({"enabled": False}, "trading disabled"),
    ({"limits": Limits(max_order_usdt=10)}, "per-order limit"),
])
def test_maker_first_guard_rejection_sends_nothing(run, tmp_path, kwargs, reason):
    fake = MakerBinance()
    service, _ = make_service(tmp_path, fake, **kwargs)
    with pytest.raises(TradeRejected, match=reason):
        run(service.place_maker_first(BUY_20, wait_s=0))
    assert fake.posted() == []
    assert service.guard.used_today == 0


@pytest.mark.unit
def test_maker_first_only_takes_market_requests(run, tmp_path):
    service, _ = make_service(tmp_path, MakerBinance())
    limit = OrderRequest(symbol="BTCUSDT", side="BUY", type="LIMIT", quantity=0.0002, price=BID)
    with pytest.raises(TradeRejected):
        run(service.place_maker_first(limit, wait_s=0))
