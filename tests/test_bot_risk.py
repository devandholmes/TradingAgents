"""The bot's risk exits on the live price, and maker-first execution, against the tests' own Postgres.

The price "cache" is a dict standing in for ``TradingService.price_lookup``; Binance is
the fake from test_trading_guard (or test_fees' maker fake). Covered: each trigger,
the latch from exit to the strategy's own exit and re-entry, only the bot's quantity
sold (always MARKET), a paper exit below minNotional, a kill-switch rejection that still
latches, maker-first entries with their real fee, repeated ticks exiting once, and a
restart resuming the peak and the latch from the database. Skipped without Postgres.
"""
import asyncio
import time

import pytest

# ruff: noqa: F811  (the bot tests' fixtures are imported, then named as test arguments)
from tests.test_bot import BTC, PRICE, bot, run, set_target, signal, sql, url  # noqa: F401
from tests.test_fees import MakerBinance
from tradingagents.trading import service as service_module
from tradingagents.trading.bot import SLIPPAGE, Bot, risk_levels, risk_trigger
from tradingagents.trading.fees import DEFAULT_PAPER_FEE as FEE

ENTRY = PRICE * (1 + SLIPPAGE)   # a paper BUY's fill: the entry the exits measure from, fees left out


def feed(prices: dict) -> dict:
    """Point the running service's price lookup at ``prices`` (symbol -> last price)."""
    service_module._service.price_lookup = prices.get
    return prices


def rows(sql) -> list[dict]:
    found = sql("SELECT side, qty, price, real, status, reason, fee, fee_asset FROM bot_trades "
                "WHERE instance_id = %s ORDER BY id", BTC)
    keys = ("side", "qty", "price", "real", "status", "reason", "fee", "fee_asset")
    return [dict(zip(keys, r, strict=True)) for r in found]


def config(sql) -> tuple:
    return sql("SELECT peak_price, risk_latched, risk_reason FROM bot_config")[0]


@pytest.mark.unit
def test_levels_and_trigger_order():
    levels = risk_levels(100.0, 120.0, 5, 10, 10)
    assert levels == {"stop_price": 95.0, "take_profit_price": pytest.approx(110.0), "trailing_stop_price": 108.0}
    assert risk_trigger(95.0, levels) == "stop_loss"          # at the level fires (95 is exact here)
    assert risk_trigger(95.01, levels) == "trailing_stop"     # below the peak's trail, above the stop
    assert risk_trigger(110.01, levels) == "take_profit"
    assert risk_trigger(109.0, levels) is None
    # Both crossed at once (a trail above the take-profit): stop-loss and take-profit come first.
    assert risk_trigger(94.0, risk_levels(100.0, 200.0, 5, None, 10)) == "stop_loss"
    assert risk_levels(None, None, 5, 5, 5) == {"stop_price": None, "take_profit_price": None,
                                                 "trailing_stop_price": None}


@pytest.mark.unit
def test_paper_stop_loss_sells_everything_once_and_latches(bot, run, sql):
    fake = bot.with_service()
    prices = feed({"BTCUSDT": PRICE})
    set_target(sql, BTC, 1.0)
    status = run(bot.activate(BTC, 0.10, stop_loss_pct=5))   # below the 5 USDT minimum: paper
    assert status["active"]["mode"] == "paper" and status["active"]["execution"] == "market"
    risk = status["risk"]
    assert risk["stop_price"] == pytest.approx(ENTRY * 0.95) and risk["take_profit_price"] is None
    assert (risk["latched"], risk["reason"], risk["peak_price"]) == (False, None, pytest.approx(ENTRY))
    assert rows(sql)[0]["fee"] == pytest.approx(0.10 * FEE) and rows(sql)[0]["fee_asset"] == "USDT"

    prices["BTCUSDT"] = ENTRY * 0.951
    assert run(bot.check_risk()) is None and len(rows(sql)) == 1
    prices["BTCUSDT"] = ENTRY * 0.9499
    assert run(bot.check_risk()) == "stop_loss"
    buy, sell = rows(sql)
    assert (sell["side"], sell["real"], sell["status"], sell["reason"]) == ("SELL", False, "filled", "stop_loss")
    assert sell["qty"] == pytest.approx(buy["qty"]) and sell["price"] == pytest.approx(ENTRY * 0.9499 * (1 - SLIPPAGE))
    assert sell["fee"] == pytest.approx(sell["qty"] * sell["price"] * FEE) and sell["fee_asset"] == "USDT"
    for _ in range(3):   # later ticks: nothing left to watch
        assert run(bot.check_risk()) is None
    assert len(rows(sql)) == 2 and "/api/v3/order" not in fake.paths()
    status = run(bot.status())
    assert status["position"]["qty"] == 0 and status["risk"]["latched"] and status["risk"]["reason"] == "stop_loss"
    assert bot.published[-1]["risk"]["latched"]


@pytest.mark.unit
def test_take_profit_fires_above_the_entry(bot, run, sql):
    bot.with_service()
    prices = feed({"BTCUSDT": PRICE})
    set_target(sql, BTC, 1.0)
    run(bot.activate(BTC, 0.10, take_profit_pct=10))
    prices["BTCUSDT"] = ENTRY * 1.0999
    assert run(bot.check_risk()) is None
    prices["BTCUSDT"] = ENTRY * 1.1001
    assert run(bot.check_risk()) == "take_profit"
    assert rows(sql)[-1]["reason"] == "take_profit"


@pytest.mark.unit
def test_trailing_stop_follows_the_saved_peak(bot, run, sql):
    bot.with_service()
    prices = feed({"BTCUSDT": PRICE})
    set_target(sql, BTC, 1.0)
    run(bot.activate(BTC, 0.10, trailing_pct=3))
    prices["BTCUSDT"] = 105_000.0
    assert run(bot.check_risk()) is None
    prices["BTCUSDT"] = 104_000.0   # lower: the peak stays
    assert run(bot.check_risk()) is None
    assert config(sql) == (105_000.0, False, None)
    assert run(bot.status())["risk"]["trailing_stop_price"] == pytest.approx(105_000 * 0.97)
    prices["BTCUSDT"] = 101_900.0
    assert run(bot.check_risk()) is None
    prices["BTCUSDT"] = 101_850.0
    assert run(bot.check_risk()) == "trailing_stop"
    assert rows(sql)[-1]["reason"] == "trailing_stop" and config(sql)[1:] == (True, "trailing_stop")


@pytest.mark.unit
def test_the_latch_holds_until_the_strategy_exits_then_it_re_enters(bot, run, sql):
    bot.with_service()
    prices = feed({"BTCUSDT": PRICE})
    set_target(sql, BTC, 1.0)
    run(bot.activate(BTC, 0.10, stop_loss_pct=5))
    prices["BTCUSDT"] = 90_000.0
    assert run(bot.check_risk()) == "stop_loss"

    run(bot.reconcile())                   # target still 1: stays flat
    run(bot.on_signal(signal(target=1.0)))
    run(bot.activate(BTC, 0.10, stop_loss_pct=5))   # re-activating the same instance keeps the latch
    assert [r["side"] for r in rows(sql)] == ["BUY", "SELL"] and config(sql)[1]

    set_target(sql, BTC, 0.0)              # the strategy's own exit clears it
    status = run(bot.reconcile())
    assert config(sql)[1:] == (False, None) and not status["risk"]["latched"]
    assert len(rows(sql)) == 2

    run(bot.on_signal(signal(target=1.0, price=90_000.0)))   # a fresh entry
    assert [r["side"] for r in rows(sql)] == ["BUY", "SELL", "BUY"]
    assert config(sql)[0] is None   # the new position's peak starts over
    assert run(bot.status())["risk"]["peak_price"] == pytest.approx(90_000 * (1 + SLIPPAGE))


@pytest.mark.unit
def test_live_exit_sells_only_the_bots_quantity_by_market_even_when_maker_first(bot, run, sql):
    fake = bot.with_service()
    prices = feed({"BTCUSDT": PRICE})
    run(bot.activate(BTC, 20))   # the fake fills 0.0002 BTC: 20 USDT keeps the entry at the 100k price
    run(bot.on_signal(signal(target=1.0)))   # a real MARKET BUY, net of the modelled fee
    run(bot.set_risk(5, None, None, "maker_first"))
    held = rows(sql)[0]["qty"]
    prices["BTCUSDT"] = 90_000.0
    assert run(bot.check_risk()) == "stop_loss"
    [sell] = [dict(r.url.params) for r in fake.requests
              if r.url.path == "/api/v3/order" and dict(r.url.params)["side"] == "SELL"]
    assert sell["type"] == "MARKET" and 0 < float(sell["quantity"]) <= held
    assert "/api/v3/account" not in fake.paths() and "/api/v3/ticker/bookTicker" not in fake.paths()
    last = rows(sql)[-1]
    assert (last["side"], last["real"], last["status"], last["reason"]) == ("SELL", True, "filled", "stop_loss")


@pytest.mark.unit
def test_kill_switch_off_rejects_the_exit_but_still_latches(bot, run, sql):
    fake = bot.with_service()
    prices = feed({"BTCUSDT": PRICE})
    run(bot.activate(BTC, 20, stop_loss_pct=5))
    run(bot.on_signal(signal(target=1.0)))
    service_module._service.guard.set_enabled(False)   # the test service's own switch, in tmp_path
    prices["BTCUSDT"] = 90_000.0
    assert run(bot.check_risk()) == "stop_loss"
    assert run(bot.check_risk()) is None   # latched: not fired again
    last = rows(sql)[-1]
    assert (last["side"], last["real"], last["status"]) == ("SELL", True, "rejected")
    assert last["reason"] == "stop_loss: trading disabled"
    assert len(rows(sql)) == 2 and config(sql)[1:] == (True, "stop_loss")
    assert not [r for r in fake.requests if r.url.path == "/api/v3/order" and dict(r.url.params)["side"] == "SELL"]
    assert run(bot.status())["position"]["qty"] > 0   # still held, so the next reconcile retries the exit


@pytest.mark.unit
def test_repeated_concurrent_ticks_exit_once(bot, run, sql):
    bot.with_service()
    prices = feed({"BTCUSDT": PRICE})
    set_target(sql, BTC, 1.0)
    run(bot.activate(BTC, 0.10, stop_loss_pct=5))
    prices["BTCUSDT"] = 90_000.0

    async def burst():
        return await asyncio.gather(*(bot.check_risk() for _ in range(5)))

    run(burst())
    assert [r["side"] for r in rows(sql)] == ["BUY", "SELL"]


@pytest.mark.unit
def test_a_restart_resumes_the_peak_and_the_latch_from_the_database(bot, run, sql):
    bot.with_service()
    prices = feed({"BTCUSDT": PRICE})
    set_target(sql, BTC, 1.0)
    run(bot.activate(BTC, 0.10, trailing_pct=3))
    prices["BTCUSDT"] = 110_000.0
    run(bot.check_risk())

    restarted = Bot()
    run(restarted.reconcile())   # what the LISTEN connection's first connect does
    prices["BTCUSDT"] = 106_000.0   # 3.6 % under the saved peak, still far above the entry
    assert run(restarted.check_risk()) == "trailing_stop"

    again = Bot()
    status = run(again.reconcile())
    assert status["risk"]["latched"] and status["risk"]["reason"] == "trailing_stop"
    run(again.on_signal(signal(target=1.0)))
    assert [r["side"] for r in rows(sql)] == ["BUY", "SELL"]


@pytest.mark.unit
def test_maker_first_entry_books_the_real_fee_and_exits_by_market(bot, run, sql, monkeypatch):
    monkeypatch.setattr(service_module, "POLL_SECONDS", 0)
    fake = bot.with_service(fake=MakerBinance(polls=["FILLED"]))
    prices = feed({"BTCUSDT": PRICE})
    run(bot.activate(BTC, 10, stop_loss_pct=5, execution="maker_first"))
    run(bot.on_signal(signal(target=1.0)))
    assert fake.posted() == ["LIMIT_MAKER"]
    [buy] = rows(sql)
    assert (buy["side"], buy["real"], buy["status"], buy["reason"]) == ("BUY", True, "filled", None)
    assert buy["qty"] == pytest.approx(0.0001) and buy["price"] == pytest.approx(100_000)   # BNB fee: qty untouched
    assert (buy["fee"], buy["fee_asset"]) == (pytest.approx(0.0000075), "BNB")

    prices["BTCUSDT"] = 90_000.0
    assert run(bot.check_risk()) == "stop_loss"
    assert fake.posted() == ["LIMIT_MAKER", "MARKET"]
    sell = rows(sql)[-1]
    assert (sell["side"], sell["status"], sell["reason"], sell["fee_asset"]) == ("SELL", "filled", "stop_loss", "BNB")


@pytest.mark.unit
def test_set_risk_needs_an_active_bot(bot, run, sql):
    bot.with_service()
    with pytest.raises(LookupError):
        run(bot.set_risk(5, None, None))
    run(bot.activate(BTC, 0.10, stop_loss_pct=5, execution="maker_first"))
    status = run(bot.set_risk(None, 10, 2))
    assert (status["risk"]["stop_loss_pct"], status["risk"]["take_profit_pct"], status["risk"]["trailing_pct"]) \
        == (None, 10, 2)
    assert status["active"]["execution"] == "maker_first"   # unchanged when not given


@pytest.mark.unit
def test_the_monitor_checks_the_cache_on_its_own(bot, run, sql):
    bot.with_service()
    prices = feed({"BTCUSDT": PRICE})
    set_target(sql, BTC, 1.0)
    run(bot.activate(BTC, 0.10, stop_loss_pct=5))

    async def scenario():
        bot.start()
        try:
            prices["BTCUSDT"] = 90_000.0
            began = time.monotonic()
            while len(rows(sql)) < 2:
                assert time.monotonic() - began < 3, "the monitor never fired"
                await asyncio.sleep(0.05)
            return time.monotonic() - began
        finally:
            await bot.close()

    assert run(scenario()) < 1.5   # within a tick or two of the price move
    assert rows(sql)[-1]["reason"] == "stop_loss"
