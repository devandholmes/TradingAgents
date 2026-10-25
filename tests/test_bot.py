"""The strategy bot against the tests' own Postgres and a TradingService on a fake Binance.

Paper fills at the signal price with slippage and fee, live BUYs through the guard as
source "bot", SELLs of the bot's own quantity only, rejected live orders recorded and
never paper-substituted, activation replacing (and closing) the active instance, and
reconciling to the instance's current target on activation, on (re)connect and on demand.
Skipped when Postgres is not running.

The test database is shared with concurrent runs: rows are deleted by this module's own
ids only, and the one bot_config row is held under an advisory lock for each test.
"""
import asyncio
import contextlib
import time

import httpx
import psycopg
import pytest

from tests.test_trading_guard import FakeBinance, audit, make_service
from tradingagents import db
from tradingagents.db import testing
from tradingagents.trading import TradeRejected, service as service_module, streams
from tradingagents.trading.bot import SLIPPAGE, Bot
from tradingagents.trading.fees import DEFAULT_PAPER_FEE as FEE
from tradingagents.web.strategies import Hub

BTC = "ema_cross:BTCUSDT:1h"
OTHER = "sma_cross:BTCUSDT:1h"
PRICE = 100_000.0
OWN = [BTC, OTHER]
BOT_CONFIG_LOCK = 7_402_001   # pg advisory lock key: every test that drives the one bot_config row takes it
RESET_BOT = "UPDATE bot_config SET instance_id = NULL, size_usdt = NULL, activated_at = NULL, enabled = false"


@contextlib.contextmanager
def bot_config_lock(conn):
    """Serialise bot_config across concurrent test runs; the bot is left off on the way out."""
    conn.execute("SELECT pg_advisory_lock(%s)", (BOT_CONFIG_LOCK,))
    try:
        conn.execute(RESET_BOT)
        yield
    finally:
        conn.execute(RESET_BOT)
        conn.execute("SELECT pg_advisory_unlock(%s)", (BOT_CONFIG_LOCK,))


@pytest.fixture(scope="module")
def url():
    return testing.require_test_database()


@pytest.fixture(scope="module")
def run(url):
    # One loop and pool for the module, made before the per-test network block.
    with asyncio.Runner(loop_factory=db.LOOP_FACTORY) as runner:
        assert runner.run(db.open_pool(url)) is not None
        yield runner.run
        runner.run(db.close_pool())


@pytest.fixture
def sql(url):
    with psycopg.connect(url, autocommit=True, **db.SESSION) as conn, bot_config_lock(conn):
        conn.execute("DELETE FROM strategy_instances WHERE id = ANY(%s)", (OWN,))
        conn.execute("DELETE FROM bot_trades WHERE instance_id = ANY(%s)", (OWN,))
        for instance in OWN:
            key, symbol, interval = instance.split(":")
            conn.execute("INSERT INTO strategy_instances (id, strategy_key, symbol, interval) VALUES (%s, %s, %s, %s)",
                         (instance, key, symbol, interval))
        conn.execute("INSERT INTO candles VALUES ('BTCUSDT', '1h', date_trunc('hour', now()), %s, %s, %s, %s, 1) "
                     "ON CONFLICT (symbol, interval, open_time) DO UPDATE SET close = EXCLUDED.close",
                     (PRICE, PRICE, PRICE, PRICE))

        def query(text, *params):
            cur = conn.execute(text, params)
            return cur.fetchall() if cur.description else None
        yield query


def set_target(sql, instance, exposure, pending=None) -> None:
    """The engine's current state for ``instance``: what it holds, and the target it decided at the last close."""
    sql("INSERT INTO instance_state (instance_id, exposure, cash, qty, equity, pending_target) "
        "VALUES (%s, %s, 0, 0, 1000, %s) ON CONFLICT (instance_id) DO UPDATE SET "
        "exposure = EXCLUDED.exposure, pending_target = EXCLUDED.pending_target", instance, exposure, pending)


def trades(sql) -> list[tuple]:
    return sql("SELECT instance_id, side, quote, qty, price, real, status, reason FROM bot_trades "
               "WHERE instance_id = ANY(%s) ORDER BY id", OWN)


@pytest.fixture
def bot(run, sql, tmp_path, monkeypatch):
    def with_service(**kwargs):
        service, fake = make_service(tmp_path, **kwargs)
        monkeypatch.setattr(service_module, "_service", service)
        return fake

    published = []
    bot = Bot(published.append)
    bot.with_service, bot.published = with_service, published
    return bot


def signal(instance=BTC, target=1.0, price=PRICE) -> dict:
    return {"instance_id": instance, "target": target, "previous": 0.0, "price": price, "bar": "2026-10-24T17:00:00Z"}


@pytest.mark.unit
def test_paper_mode_below_min_notional_fills_at_signal_price_with_fee_and_slippage(bot, run, sql):
    fake = bot.with_service()
    status = run(bot.activate(BTC, 0.10))
    assert status["active"]["mode"] == "paper" and status["active"]["size_usdt"] == 0.10

    run(bot.on_signal(signal(target=1.0)))
    run(bot.on_signal(signal(target=0.5, price=110_000.0)))
    buy, sell = trades(sql)
    fill = PRICE * (1 + SLIPPAGE)
    assert buy[:2] == (BTC, "BUY") and buy[5:7] == (False, "filled")
    assert buy[2] == pytest.approx(0.10) and buy[4] == pytest.approx(fill)
    assert buy[3] == pytest.approx(0.10 * (1 - FEE) / fill)
    assert sell[1] == "SELL" and sell[3] == pytest.approx(buy[3] / 2)
    assert sell[4] == pytest.approx(110_000 * (1 - SLIPPAGE))
    assert sell[2] == pytest.approx(sell[3] * sell[4] * (1 - FEE))
    assert "/api/v3/order" not in fake.paths()   # nothing reached Binance

    status = run(bot.status())
    assert status["position"]["exposure"] == pytest.approx(0.5)
    assert status["pnl"]["realized"] == pytest.approx(sell[2] - 0.05)
    assert status["pnl"]["paper_total"] == pytest.approx(status["pnl"]["total"])
    assert [t["id"] for t in status["trades"]] == sorted((t["id"] for t in status["trades"]), reverse=True)
    assert bot.published[-1]["type"] == "bot"


@pytest.mark.unit
def test_live_buy_goes_through_the_trading_service_as_the_bot(bot, run, sql, tmp_path):
    fake = bot.with_service()
    assert run(bot.activate(BTC, 10))["active"]["mode"] == "live"

    run(bot.on_signal(signal(target=1.0)))
    order = [r for r in fake.requests if r.url.path == "/api/v3/order"]
    assert len(order) == 1 and dict(order[0].url.params)["quoteOrderQty"] == "10"
    assert audit(tmp_path)[-1]["source"] == "bot" and audit(tmp_path)[-1]["status"] == "filled"
    ((instance, side, quote, qty, _, real, status, _),) = trades(sql)
    assert (instance, side, real, status, quote) == (BTC, "BUY", True, "filled", 10.0)
    assert qty == pytest.approx(0.0002 * (1 - FEE))   # what Binance credits, net of its fee


@pytest.mark.unit
def test_sell_never_exceeds_the_bots_own_quantity(bot, run, sql):
    fake = bot.with_service()
    run(bot.activate(BTC, 10))
    run(bot.on_signal(signal(target=1.0)))
    run(bot.on_signal(signal(target=0.0)))
    held = trades(sql)[0][3]
    sells = [dict(r.url.params) for r in fake.requests
             if r.url.path == "/api/v3/order" and dict(r.url.params)["side"] == "SELL"]
    assert len(sells) == 1 and 0 < float(sells[0]["quantity"]) <= held
    assert "/api/v3/account" not in fake.paths()   # never sized from the account's balances
    assert trades(sql)[-1][1:2] + trades(sql)[-1][5:7] == ("SELL", True, "filled")


@pytest.mark.unit
def test_live_mode_buy_below_the_minimum_is_paper_mirrored(bot, run, sql):
    fake = bot.with_service()
    run(bot.activate(BTC, 10))
    run(bot.on_signal(signal(target=0.3)))   # 3 USDT < the 5 USDT minNotional
    ((_, side, quote, _, _, real, status, _),) = trades(sql)
    assert (side, quote, real, status) == ("BUY", pytest.approx(3.0), False, "filled")
    assert "/api/v3/order" not in fake.paths()


@pytest.mark.unit
def test_kill_switch_rejection_is_recorded_without_a_paper_fill(bot, run, sql):
    fake = bot.with_service(enabled=False)
    run(bot.activate(BTC, 10))
    run(bot.on_signal(signal(target=1.0)))
    assert trades(sql) == [(BTC, "BUY", 10.0, None, PRICE, True, "rejected", "trading disabled")]
    assert "/api/v3/order" not in fake.paths()
    assert run(bot.status())["position"]["exposure"] == 0   # the next signal retries the whole delta


@pytest.mark.unit
def test_a_signal_for_another_instance_or_an_inactive_bot_is_ignored(bot, run, sql):
    bot.with_service()
    run(bot.on_signal(signal()))
    run(bot.activate(BTC, 1))
    run(bot.on_signal(signal(instance=OTHER)))
    run(bot.on_signal(signal(price=float("nan"))))
    run(bot.on_signal(signal(price=None)))   # no candle to price it: ignored, the next reconcile catches up
    assert trades(sql) == []


@pytest.mark.unit
def test_activation_validates_and_replacing_closes_the_old_position(bot, run, sql):
    bot.with_service()
    with pytest.raises(LookupError):
        run(bot.activate("nope:BTCUSDT:1h", 1))
    with pytest.raises(TradeRejected, match="per-order limit"):
        run(bot.activate(BTC, 50.01))
    run(bot.activate(BTC, 1))
    run(bot.on_signal(signal(target=1.0)))
    status = run(bot.activate(OTHER, 2))
    assert [t[:2] for t in trades(sql)] == [(BTC, "BUY"), (BTC, "SELL")]
    assert status["active"]["instance_id"] == OTHER and status["position"]["qty"] == 0

    status = run(bot.deactivate(close_position=True))
    assert status["active"] is None
    assert sql("SELECT instance_id, enabled FROM bot_config") == [(None, False)]


@pytest.mark.unit
def test_a_real_sell_below_the_minimum_is_never_sent(bot, run, sql):
    fake = bot.with_service()
    run(bot.activate(BTC, 10))
    run(bot.on_signal(signal(target=1.0)))
    # A fifth of the 0.0002 BTC held, at 100k: 4 USDT, below the 5 USDT minNotional that the fake's
    # NOTIONAL filter (applyMinToMarket false) does not apply to MARKET orders.
    run(bot.on_signal(signal(target=0.8)))
    assert trades(sql)[-1][1:2] + trades(sql)[-1][5:8] == (
        "SELL", True, "rejected", "sell value is below the 5 USDT minimum")
    assert not [r for r in fake.requests if r.url.path == "/api/v3/order" and dict(r.url.params)["side"] == "SELL"]


@pytest.mark.unit
def test_replacing_is_refused_while_the_old_real_holding_cannot_be_sold(bot, run, sql):
    bot.with_service()
    run(bot.activate(BTC, 10))
    run(bot.on_signal(signal(target=1.0)))
    service_module._service.guard.set_enabled(False)   # the test service's own kill switch, in tmp_path
    with pytest.raises(TradeRejected, match="could not be sold"):
        run(bot.activate(OTHER, 10))
    assert sql("SELECT instance_id, enabled FROM bot_config") == [(BTC, True)]   # still the bot's to sell
    assert trades(sql)[-1][1:2] + trades(sql)[-1][5:7] == ("SELL", True, "rejected")


# --- reconcile-to-target ---

@pytest.mark.unit
def test_activation_enters_the_current_position_and_reconciling_again_trades_nothing(bot, run, sql):
    bot.with_service()
    set_target(sql, BTC, 1.0)
    status = run(bot.activate(BTC, 0.10))
    ((instance, side, quote, _, price, real, state, _),) = trades(sql)
    assert (instance, side, real, state) == (BTC, "BUY", False, "filled")
    assert quote == pytest.approx(0.10) and price == pytest.approx(PRICE * (1 + SLIPPAGE))
    assert status["in_sync"] and status["drift"] == pytest.approx(0) and status["active"]["target"] == 1.0

    run(bot.reconcile())
    run(bot.reconcile())
    assert len(trades(sql)) == 1   # idempotent
    run(bot.activate(BTC, 0.10))   # re-activating the same instance neither closes nor re-buys
    assert len(trades(sql)) == 1


@pytest.mark.unit
def test_a_flat_strategy_trades_nothing_and_the_pending_target_wins(bot, run, sql):
    bot.with_service()
    set_target(sql, BTC, 0.0)
    assert run(bot.activate(BTC, 0.10))["in_sync"] and trades(sql) == []
    set_target(sql, BTC, 0.0, pending=0.5)   # decided at the last close, fills at the next open
    run(bot.reconcile())
    ((_, side, quote, *_),) = trades(sql)
    assert side == "BUY" and quote == pytest.approx(0.05)


@pytest.mark.unit
def test_a_rejected_entry_is_out_of_sync_until_a_sync_fills_it(bot, run, sql):
    fake = bot.with_service(enabled=False)
    set_target(sql, BTC, 1.0)
    status = run(bot.activate(BTC, 10))
    assert [t[5:7] for t in trades(sql)] == [(True, "rejected")]
    assert not status["in_sync"] and status["drift"] == pytest.approx(1.0)
    assert "/api/v3/order" not in fake.paths()   # no order, and no paper fill in its place

    fake = bot.with_service()   # trading back on
    status = run(bot.reconcile())
    assert [t[5:7] for t in trades(sql)] == [(True, "rejected"), (True, "filled")]
    assert status["in_sync"] and fake.paths().count("/api/v3/order") == 1


class Down(FakeBinance):
    def __call__(self, request):
        self.requests.append(request)
        raise httpx.ConnectError("down", request=request)


@pytest.mark.unit
def test_unknown_filters_never_turn_a_live_size_into_paper_fills(bot, run, sql):
    bot.with_service(warm=False, fake=Down())   # a restart while Binance is unreachable
    set_target(sql, BTC, 1.0)
    status = run(bot.activate(BTC, 10))
    assert trades(sql) == [] and not status["in_sync"]


@pytest.mark.unit
def test_connecting_and_reconnecting_the_listener_applies_targets_missed_while_down(bot, run, sql, url, monkeypatch):
    bot.with_service()
    run(bot.activate(BTC, 0.10))   # no target yet: nothing to enter
    set_target(sql, BTC, 1.0)      # a signal sent while nobody listened
    monkeypatch.setenv(db.URL_ENV, url)
    monkeypatch.setattr(streams, "BACKOFF_MIN", 0.05)

    async def until(count):
        deadline = time.monotonic() + 10
        while len(trades(sql)) < count:
            assert time.monotonic() < deadline, trades(sql)
            await asyncio.sleep(0.05)

    async def scenario():
        hub = Hub(bot)
        hub.start()   # as the web app's startup does
        try:
            await until(1)
            set_target(sql, BTC, 0.0)
            sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() "
                "AND query LIKE 'LISTEN%%' AND pid <> pg_backend_pid()")
            await until(2)
        finally:
            await hub.close()
            await bot.close()

    run(scenario())
    assert [t[1] for t in trades(sql)] == ["BUY", "SELL"]
    assert run(bot.status())["in_sync"]
