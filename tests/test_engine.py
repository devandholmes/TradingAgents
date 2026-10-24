"""The strategy engine against the real Postgres: seeding, live bars, NOTIFY, the bot signal, gap-fill.

Runs in the tests' own database (<name>_test) on synthetic ENGAUSDT candles; skipped
when Postgres is not running. Binance is replaced by a fake kline feed and a fake
REST klines endpoint: nothing leaves the machine.
"""
import asyncio
import contextlib
import json
import math
from datetime import UTC, datetime

import pandas as pd
import psycopg
import pytest

from tests.test_bot import bot_config_lock
from tradingagents import db
from tradingagents.db import backfill, candles, testing
from tradingagents.strategies import CATALOG, backtest, engine as engine_mod

SYMBOL = "ENGAUSDT"
T0 = int(datetime(2001, 1, 1, tzinfo=UTC).timestamp())
HOUR, DAY = 3600, 86400
SEEDED = 760   # 1h bars stored before the engine starts; the rest arrive live
TOTAL = 800    # under LOOKBACK, so a live target sees the same bars as the backtest


def _series(count: int, step: int) -> list[dict]:
    """A wave the trend and reversion strategies all trade, with a drift."""
    rows, prev = [], 100.0
    for i in range(count):
        close = 100 + 12 * math.sin(i / 6) + 0.02 * i
        rows.append({"time": T0 + i * step, "open": prev, "high": max(prev, close) + 1,
                     "low": min(prev, close) - 1, "close": close, "volume": 10.0 + i})
        prev = close
    return rows


HOURLY, DAILY = _series(TOTAL, HOUR), _series(60, DAY)


def _ts(row: dict) -> pd.Timestamp:
    return pd.Timestamp(row["time"], unit="s", tz="UTC")


@pytest.fixture(scope="module")
def url():
    return testing.require_test_database()


@pytest.fixture
def conn(url):
    """A clean ENGAUSDT: the seeded share of the candles stored, no instances, the bot off."""
    with psycopg.connect(url, autocommit=True, **db.SESSION) as conn, bot_config_lock(conn):
        conn.execute("DELETE FROM strategy_instances WHERE symbol = %s", (SYMBOL,))
        conn.execute("DELETE FROM candles WHERE symbol = %s", (SYMBOL,))
        conn.execute("DELETE FROM ai_signals WHERE symbol = %s", (SYMBOL,))
        with conn.transaction():
            candles.upsert_candles(conn, SYMBOL, "1h", HOURLY[:SEEDED])
            candles.upsert_candles(conn, SYMBOL, "1d", DAILY[:-1])
        yield conn


@pytest.fixture
def engine(url, conn):
    eng = engine_mod.Engine(url, symbols=[SYMBOL], intervals=("1h", "1d"))
    yield eng
    eng.close()


def _listen(url, channel):
    listener = psycopg.connect(url, autocommit=True, **db.SESSION)
    listener.execute(f"LISTEN {channel}")
    return listener


def _drain(listener) -> list[dict]:
    return [json.loads(n.payload) for n in listener.notifies(timeout=0.5)]


def _one(conn, sql, *params):
    return conn.execute(sql, params).fetchone()


@pytest.mark.unit
def test_seeding_writes_history_state_and_every_window(engine, conn, url):
    engine.start(fetch=False)
    ids = list(engine.instances)
    assert len(ids) == 12 + 13   # 1h: every strategy but the daily AI; 1d: all of them
    assert _one(conn, "SELECT count(*) FROM strategy_metrics WHERE instance_id = ANY(%s)", ids) == (len(ids) * 5,)
    periods = {p for (p,) in conn.execute("SELECT DISTINCT period FROM strategy_metrics WHERE instance_id = ANY(%s)",
                                          (ids,))}
    assert periods == {"7d", "30d", "90d", "1y", "live"}
    assert _one(conn, "SELECT count(*) FROM instance_state WHERE instance_id = ANY(%s) AND last_bar = %s",
                ids[:12], _ts(HOURLY[SEEDED - 1])) == (12,)

    ema = f"ema_cross:{SYMBOL}:1h"
    days = len({r["time"] // DAY for r in HOURLY[:SEEDED]})
    assert _one(conn, "SELECT count(*), min(source), max(source) FROM equity_points WHERE instance_id = %s",
                ema) == (days, "backtest", "backtest")
    seeded = engine_mod._frame(HOURLY[:SEEDED])
    expected = backtest(seeded, CATALOG["ema_cross"].target(seeded))
    assert _one(conn, "SELECT count(*) FROM paper_fills WHERE instance_id = %s AND source = 'backtest'",
                ema) == (len(expected.fills),)
    equity, = _one(conn, "SELECT equity FROM instance_state WHERE instance_id = %s", ema)
    assert equity == pytest.approx(expected.equity.iloc[-1])
    assert _one(conn, "SELECT backtested_through FROM strategy_instances WHERE id = %s", ema)[0] == _ts(
        HOURLY[SEEDED - 1])

    # A restart resumes: nothing is seeded or written twice, and a second engine is refused.
    fills = _one(conn, "SELECT count(*) FROM paper_fills WHERE instance_id = ANY(%s)", ids)
    with pytest.raises(RuntimeError, match="already running"):
        engine_mod.Engine(url, symbols=[SYMBOL], intervals=("1h", "1d")).start(fetch=False)
    engine.close()
    again = engine_mod.Engine(url, symbols=[SYMBOL], intervals=("1h", "1d"))
    try:
        again.start(fetch=False)
        assert _one(conn, "SELECT count(*) FROM paper_fills WHERE instance_id = ANY(%s)", ids) == fills
        assert again.instances[ema].equity == pytest.approx(expected.equity.iloc[-1])
    finally:
        again.close()


@pytest.mark.unit
def test_closed_bars_trade_at_the_next_open_like_the_backtest(engine, conn, url):
    engine.start(fetch=False)
    listener = _listen(url, "ta_strategy_update")
    try:
        for row in HOURLY[SEEDED:]:
            assert len(engine.on_bar(SYMBOL, "1h", row)) == 12
            assert engine.on_bar(SYMBOL, "1h", row) == []   # the same bar again: already traded
        notes = _drain(listener)
    finally:
        listener.close()

    assert len(notes) == TOTAL - SEEDED
    assert notes[-1]["bar"] == _ts(HOURLY[-1]).isoformat() and len(notes[-1]["instance_ids"]) == 12

    full = engine_mod._frame(HOURLY)
    live_fills = 0
    for key in CATALOG:
        if key == "tradingagents_ai":
            continue
        iid = f"{key}:{SYMBOL}:1h"
        expected = backtest(full, CATALOG[key].target(full))
        want = [f for f in expected.fills if f["ts"] >= _ts(HOURLY[SEEDED])]
        got = conn.execute("SELECT ts, side, price, qty FROM paper_fills WHERE instance_id = %s AND source = 'live'"
                           " ORDER BY ts", (iid,)).fetchall()
        assert [(pd.Timestamp(t), s) for t, s, _, _ in got] == [(f["ts"], f["side"]) for f in want], key
        for (_, _, price, qty), f in zip(got, want, strict=True):
            assert (price, qty) == (pytest.approx(f["price"]), pytest.approx(f["qty"]))
        # Each fill is at its bar's open, decided at the close before.
        for ts, side, price, _ in got:
            bar = next(r for r in HOURLY if _ts(r) == pd.Timestamp(ts))
            assert price == pytest.approx(bar["open"] * (1.0002 if side == "BUY" else 0.9998))
        live_fills += len(got)
        assert engine.instances[iid].equity == pytest.approx(expected.equity.iloc[-1]), key
        assert _one(conn, "SELECT count(*) FROM equity_points WHERE instance_id = %s AND source = 'live'",
                    iid) == (TOTAL - SEEDED,)
        trades, = _one(conn, "SELECT trades FROM strategy_metrics WHERE instance_id = %s AND period = 'live'", iid)
        assert trades == len(got)
    assert live_fills > 5


@pytest.mark.unit
def test_bot_signal_only_for_the_active_instance(engine, conn, url):
    engine.start(fetch=False)
    active = f"ema_cross:{SYMBOL}:1h"
    conn.execute("UPDATE bot_config SET instance_id = %s, size_usdt = 1, activated_at = now(), enabled = true",
                 (active,))
    listener = _listen(url, "ta_bot_signal")
    try:
        for row in HOURLY[SEEDED:]:
            engine.on_bar(SYMBOL, "1h", row)
        signals = _drain(listener)
    finally:
        listener.close()
    assert signals and {s["instance_id"] for s in signals} == {active}
    for s in signals:
        assert s["target"] != s["previous"]
        bar = next(r for r in HOURLY if _ts(r).isoformat() == s["bar"])
        assert s["price"] == pytest.approx(bar["close"])
    assert len(signals) < TOTAL - SEEDED   # only when the target changes


@pytest.mark.unit
def test_ai_instance_follows_ai_signals(engine, conn, url):
    engine.start(fetch=False)
    ai = f"tradingagents_ai:{SYMBOL}:1d"
    conn.execute("UPDATE bot_config SET instance_id = %s, size_usdt = 1, activated_at = now(), enabled = true", (ai,))
    conn.execute("INSERT INTO ai_signals (symbol, rating) VALUES (%s, 'Buy')", (SYMBOL,))
    listener = _listen(url, "ta_bot_signal")
    try:
        engine.sync_ai(SYMBOL)
        (signal,) = _drain(listener)
    finally:
        listener.close()
    assert (signal["instance_id"], signal["target"], signal["previous"]) == (ai, 1.0, 0.0)
    assert _one(conn, "SELECT pending_target FROM instance_state WHERE instance_id = %s", ai) == (1.0,)

    engine.on_bar(SYMBOL, "1d", DAILY[-1])
    side, price = _one(conn, "SELECT side, price FROM paper_fills WHERE instance_id = %s AND source = 'live'", ai)
    assert (side, price) == ("BUY", pytest.approx(DAILY[-1]["open"] * 1.0002))


# --- the process: one combined websocket, gap-fill on reconnect -------------------------------------


@pytest.fixture(scope="module")
def runner():
    with asyncio.Runner(loop_factory=db.LOOP_FACTORY) as runner:
        runner.get_loop()   # made here: on Windows a new loop opens a socket pair, which the network block refuses
        yield runner


def _kline(row: dict, closed: bool = True) -> str:
    return json.dumps({"stream": f"{SYMBOL.lower()}@kline_1h", "data": {"e": "kline", "s": SYMBOL, "k": {
        "t": row["time"] * 1000, "i": "1h", "o": str(row["open"]), "h": str(row["high"]), "l": str(row["low"]),
        "c": str(row["close"]), "v": str(row["volume"]), "x": closed}}})


@pytest.mark.unit
def test_reconnect_fetches_the_missed_bars(engine, conn, runner, monkeypatch):
    visible: list[dict] = []   # what Binance's REST klines endpoint has

    def fake_klines(pair, interval, start_ms, end_ms):
        assert (pair, interval) in {(SYMBOL, "1h"), (SYMBOL, "1d")}
        return [r for r in visible if interval == "1h" and start_ms <= r["time"] * 1000 <= end_ms][:1000]

    monkeypatch.setattr(backfill, "_klines", fake_klines)
    iid = f"ema_cross:{SYMBOL}:1h"
    first, missed, after = HOURLY[SEEDED], HOURLY[SEEDED + 1:SEEDED + 3], HOURLY[SEEDED + 3]

    async def session_one():
        yield _kline(first, closed=False)
        yield _kline(first)
        while engine.instances[iid].last_bar != _ts(first):
            await asyncio.sleep(0.01)
        visible.extend(missed)   # two bars close while the connection is down
        raise ConnectionError("dropped")

    async def session_two():
        yield _kline(after)
        await asyncio.Event().wait()

    sessions, urls = [session_one, session_two], []

    @contextlib.asynccontextmanager
    async def connect(url):
        urls.append(url)
        yield sessions.pop(0)()

    async def scenario():
        task = asyncio.create_task(engine_mod.run(engine, connect=connect, listen=False))
        try:
            while iid not in engine.instances or engine.instances[iid].last_bar != _ts(after):
                if task.done():
                    task.result()
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    runner.run(asyncio.wait_for(scenario(), 30))
    assert urls == [engine_mod.stream_url([(SYMBOL, "1h"), (SYMBOL, "1d")])] * 2
    assert "engausdt@kline_1h/engausdt@kline_1d" in urls[0]
    stored = {r["time"] for r in candles.read_candles(conn, SYMBOL, "1h", datetime(2000, 1, 1, tzinfo=UTC),
                                                     datetime(2002, 1, 1, tzinfo=UTC))}
    assert {r["time"] for r in HOURLY[SEEDED:SEEDED + 4]} <= stored
    live = [pd.Timestamp(t) for (t,) in conn.execute(
        "SELECT ts FROM equity_points WHERE instance_id = %s AND source = 'live' ORDER BY ts", (iid,))]
    assert live == [_ts(r) for r in HOURLY[SEEDED:SEEDED + 4]]
