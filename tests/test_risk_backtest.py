"""Risk exits in the backtest (stop-loss, take-profit, trailing, the latch), the engine's stop analysis and
equity retention, and GET /api/strategies/instances/{id}/stops.

The backtest tests are pure; the engine and route tests run in the tests' own database (<name>_test) on
synthetic RISKUSDT candles, and are skipped when Postgres is not running.
"""
import math
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import psycopg
import pytest

from tests.test_web_strategies import http, url  # noqa: F401  (fixtures)
from tradingagents import db
from tradingagents.db import candles as candles_db
from tradingagents.strategies import engine as engine_mod
from tradingagents.strategies.backtest import PRESETS, RiskParams, backtest

FEE, SLIP = 0.001, 0.0002
SYMBOL = "RISKUSDT"


def _bars(*ohlc: tuple) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=len(ohlc), freq="1h", tz="UTC")
    return pd.DataFrame(ohlc, columns=["open", "high", "low", "close"], index=index, dtype=float)


def _run(bars: pd.DataFrame, target: list[float], **risk) -> list[dict]:
    return backtest(bars, pd.Series(target, index=bars.index, dtype=float), 1000.0, FEE, SLIP, 8760,
                    risk=RiskParams(**risk)).fills


def _walk(n: int = 600, seed: int = 7) -> tuple[pd.DataFrame, pd.Series]:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    open_ = np.r_[100.0, close[:-1]] * np.exp(rng.normal(0, 0.003, n))   # gaps between bars
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.01, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.01, n))
    bars = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close},
                        index=pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC"))
    target = pd.Series((np.sin(np.arange(n) / 9) > 0).astype(float), index=bars.index)
    target.iloc[::37] = 0.5   # partial steps as well
    return bars, target


@pytest.mark.unit
def test_without_exits_the_risk_path_matches_the_plain_backtest():
    bars, target = _walk()
    plain = backtest(bars, target, 1000.0, FEE, SLIP)
    looped = backtest(bars, target, 1000.0, FEE, SLIP, risk=RiskParams())
    pd.testing.assert_series_equal(plain.equity, looped.equity, check_exact=True)
    pd.testing.assert_series_equal(plain.exposure, looped.exposure, check_exact=True)
    assert plain.fills == looped.fills and plain.metrics == looped.metrics


@pytest.mark.unit
def test_stop_loss_fills_at_the_level_from_the_bar_after_the_entry():
    bars = _bars((100, 100, 100, 100), (100, 101, 90, 99), (99, 99, 94, 96), (96, 97, 95, 96))
    buy, sell = _run(bars, [1, 1, 1, 1], stop_loss_pct=5)
    entry = 100 * (1 + SLIP)
    assert buy["ts"] == bars.index[1] and buy["price"] == entry   # the entry bar's low of 90 does not count
    assert sell["ts"] == bars.index[2] and sell["reason"] == "stop_loss"
    assert sell["price"] == pytest.approx(entry * 0.95 * (1 - SLIP))
    assert sell["qty"] == buy["qty"] and sell["exposure_after"] == 0.0
    assert sell["fee"] == pytest.approx(sell["quote"] * FEE)


@pytest.mark.unit
def test_take_profit_fills_at_the_level():
    bars = _bars((100, 100, 100, 100), (100, 101, 99, 100), (100, 112, 100, 108))
    sell = _run(bars, [1, 1, 1], take_profit_pct=10)[-1]
    assert sell["reason"] == "take_profit" and sell["price"] == pytest.approx(100 * (1 + SLIP) * 1.1 * (1 - SLIP))


@pytest.mark.unit
def test_trailing_stop_follows_the_highs_since_the_entry():
    bars = _bars((100, 100, 100, 100), (100, 110, 100, 108), (108, 120, 107, 118), (118, 119, 112, 113))
    sell = _run(bars, [1, 1, 1, 1], trailing_pct=5)[-1]
    # The peak is 120 (bar 2's high): 114 fires in bar 3; bar 2's own high never moved its own stop.
    assert sell["ts"] == bars.index[3] and sell["reason"] == "trailing_stop"
    assert sell["price"] == pytest.approx(120 * 0.95 * (1 - SLIP))


@pytest.mark.unit
def test_a_gap_through_the_level_fills_at_the_open():
    bars = _bars((100, 100, 100, 100), (100, 101, 99, 100), (80, 85, 78, 84))
    sell = _run(bars, [1, 1, 1], stop_loss_pct=5)[-1]
    assert sell["reason"] == "stop_loss" and sell["price"] == pytest.approx(80 * (1 - SLIP))
    gap_up = _bars((100, 100, 100, 100), (100, 101, 99, 100), (115, 116, 114, 115))
    sell = _run(gap_up, [1, 1, 1], take_profit_pct=10)[-1]
    assert sell["reason"] == "take_profit" and sell["price"] == pytest.approx(115 * (1 - SLIP))


@pytest.mark.unit
def test_stop_loss_comes_before_take_profit_inside_one_bar():
    bars = _bars((100, 100, 100, 100), (100, 101, 99, 100), (100, 115, 90, 110))
    sell = _run(bars, [1, 1, 1], stop_loss_pct=5, take_profit_pct=10)[-1]
    assert sell["reason"] == "stop_loss" and sell["price"] == pytest.approx(100 * (1 + SLIP) * 0.95 * (1 - SLIP))


@pytest.mark.unit
def test_the_latch_holds_until_the_strategy_target_returns_to_zero():
    flat = (100, 100, 100, 100)
    bars = _bars(flat, flat, (100, 100, 90, 92), *[(92, 93, 91, 92)] * 3, flat, flat, flat)
    # Long, stopped out in bar 2; the target stays 1 (latched: flat), is 0 at bar 4's close (clearing
    # the latch) and 1 again from bar 5's: a fresh entry at bar 6's open.
    result = backtest(bars, pd.Series([1, 1, 1, 1, 0, 1, 1, 1, 1], index=bars.index, dtype=float), 1000.0, FEE,
                      SLIP, 8760, risk=RiskParams(stop_loss_pct=5))
    sides = [(f["side"], bars.index.get_loc(f["ts"]), f.get("reason")) for f in result.fills]
    assert sides == [("BUY", 1, None), ("SELL", 2, "stop_loss"), ("BUY", 6, None)]
    assert result.exposure.tolist() == [0, 1, 0, 0, 0, 0, 1, 1, 1]


@pytest.mark.unit
def test_no_look_ahead_a_bar_only_depends_on_bars_up_to_it():
    bars, target = _walk()
    cut = 400
    for preset, (_, risk) in PRESETS.items():
        whole = backtest(bars, target, 1000.0, FEE, SLIP, 8760, risk=risk)
        head = backtest(bars.iloc[:cut], target.iloc[:cut], 1000.0, FEE, SLIP, 8760, risk=risk)
        pd.testing.assert_series_equal(whole.equity.iloc[:cut], head.equity, check_exact=True, obj=preset)
        assert [f for f in whole.fills if f["ts"] < bars.index[cut]] == head.fills, preset


@pytest.mark.unit
def test_every_preset_runs_and_the_exits_fire():
    bars, target = _walk()
    reasons = set()
    for _, risk in PRESETS.values():
        result = backtest(bars, target, 1000.0, FEE, SLIP, 8760, risk=risk)
        assert math.isfinite(result.equity.iloc[-1]) and result.exposure.between(0, 1).all()
        reasons |= {f.get("reason") for f in result.fills}
    assert reasons == {None, "stop_loss", "take_profit", "trailing_stop"}


# --- the engine and the route, in the tests' database --------------------------------------------

T0 = int(datetime(2002, 1, 1, tzinfo=UTC).timestamp())
DAYS = 100   # of 4h bars: enough for the 90d period, not for 1y


def _series(count: int, step: int) -> list[dict]:
    rows, prev = [], 100.0
    for i in range(count):
        close = 100 + 15 * math.sin(i / 8) + 0.01 * i
        rows.append({"time": T0 + i * step, "open": prev, "high": max(prev, close) + 1.5,
                     "low": min(prev, close) - 1.5, "close": close, "volume": 1.0})
        prev = close
    return rows


@pytest.fixture
def conn(url):  # noqa: F811
    """RISKUSDT's rows (instances and everything under them, candles) removed before and after."""
    def clean():
        conn.execute("DELETE FROM strategy_instances WHERE symbol = %s", (SYMBOL,))
        conn.execute("DELETE FROM candles WHERE symbol = %s", (SYMBOL,))

    with psycopg.connect(url, autocommit=True, **db.SESSION) as conn:
        clean()
        yield conn
        clean()


@pytest.mark.unit
def test_engine_writes_the_stop_analysis_after_seeding_and_once_a_day(url, conn):  # noqa: F811
    candles_db.upsert_candles(conn, SYMBOL, "4h", _series(DAYS * 6, 4 * 3600))
    eng = engine_mod.Engine(url, symbols=[SYMBOL], intervals=("4h",))
    try:
        eng.start(fetch=False)
        steps = 1
        while eng.maintain():
            steps += 1
        assert steps == 1 + len(eng.instances)   # the retention step, then one instance per step
        found = conn.execute("SELECT instance_id, preset, period, trades FROM stop_analysis WHERE instance_id = ANY(%s)",
                             (list(eng.instances),)).fetchall()
        assert {(i, p) for i, p, _, _ in found} == {(i, p) for i in eng.instances for p in PRESETS}
        assert {period for _, _, period, _ in found} == {"90d"}   # 100 days of history: no 1y
        assert {p: t for i, p, _, t in found if i == f"bollinger_reversion:{SYMBOL}:4h"}["none"] > 0

        eng.stops_checked = None
        assert eng.maintain() is False   # analysed within the day: nothing due
        conn.execute("UPDATE stop_analysis SET updated_at = now() - interval '25 hours' WHERE instance_id = ANY(%s)",
                     (list(eng.instances),))
        eng.stops_checked = None
        assert eng.maintain() is True and len(eng.stops_due) == len(eng.instances) - 1
    finally:
        eng.close()


@pytest.mark.unit
def test_retention_thins_old_fast_live_points_to_one_per_hour(url, conn, monkeypatch):  # noqa: F811
    monkeypatch.setattr(engine_mod, "RETAIN_BATCH", 7)   # several batches
    now = pd.Timestamp("2026-06-01", tz="UTC")
    ids = {iv: f"ema_cross:{SYMBOL}:{iv}" for iv in ("1m", "5m", "1h")}
    for iv, iid in ids.items():
        conn.execute("INSERT INTO strategy_instances (id, strategy_key, symbol, interval)"
                     " VALUES (%s, 'ema_cross', %s, %s)", (iid, SYMBOL, iv))
    old, recent = now - pd.Timedelta(days=100), now - pd.Timedelta(days=10)
    points = [(ids["1m"], old + pd.Timedelta(minutes=m), "live") for m in range(150)]   # 2.5 hours
    points += [(ids["1m"], recent + pd.Timedelta(minutes=m), "live") for m in range(30)]
    points += [(ids["5m"], old + pd.Timedelta(minutes=5 * m), "live") for m in range(24)]
    points += [(ids["1h"], old + pd.Timedelta(minutes=m), "live") for m in range(90)]   # other intervals: kept
    points += [(ids["1m"], old - pd.Timedelta(days=1, minutes=m), "backtest") for m in range(5)]
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO equity_points VALUES (%s, %s, 1000, 1, %s)", points)
    eng = engine_mod.Engine(url, symbols=[], intervals=())
    eng.conn = db.connect(url)
    try:
        eng.retain(now)
        mine = [(i, pd.Timestamp(t), s) for i, t, s in conn.execute(
            "SELECT instance_id, ts, source FROM equity_points WHERE instance_id = ANY(%s) ORDER BY instance_id, ts",
            (list(ids.values()),))]
        assert [t for i, t, s in mine if i == ids["1m"] and s == "live" and t < recent] == [
            old + pd.Timedelta(minutes=m) for m in (59, 119, 149)]   # each hour's last
        assert sum(i == ids["1m"] and t >= recent for i, t, _ in mine) == 30
        assert [t for i, t, _ in mine if i == ids["5m"]] == [old + pd.Timedelta(minutes=m) for m in (55, 115)]
        assert sum(i == ids["1h"] for i, _, _ in mine) == 90
        assert sum(s == "backtest" for _, _, s in mine) == 5
        assert eng.retain(now) == 0   # idempotent
    finally:
        eng.close()


@pytest.mark.unit
def test_stops_route_lists_the_presets_and_picks_the_best(http, conn):  # noqa: F811
    iid = f"bollinger_reversion:{SYMBOL}:4h"
    conn.execute("INSERT INTO strategy_instances (id, strategy_key, symbol, interval) VALUES (%s, %s, %s, '4h')",
                 (iid, "bollinger_reversion", SYMBOL))
    rows = [("none", "1y", 1.0), ("sl5", "1y", 1.4), ("tr5", "1y", 1.8), ("tp5", "1y", None), ("sl2", "1y", 0.2),
            ("none", "90d", 0.5), ("tr10", "90d", 3.0)]
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO stop_analysis (instance_id, preset, period, return_pct, sharpe,"
                        " max_drawdown_pct, trades, win_rate) VALUES (%s, %s, %s, 5, %s, -3, 4, 0.5)",
                        [(iid, p, period, s) for p, period, s in rows])
    body = http.get(f"/api/strategies/instances/{iid}/stops").json()
    assert body["best"] == "tr5"   # 1y decides, though tr10 leads over 90d
    assert [(r["preset"], r["period"]) for r in body["rows"]][:3] == [("none", "1y"), ("sl2", "1y"), ("sl5", "1y")]
    assert set(body["rows"][0]) == {"preset", "label", "period", "return_pct", "sharpe", "max_drawdown_pct",
                                    "trades", "win_rate"}
    assert body["rows"][0]["label"] == "No risk exits"

    conn.execute("UPDATE stop_analysis SET sharpe = 0 WHERE instance_id = %s AND preset <> 'none'", (iid,))
    assert http.get(f"/api/strategies/instances/{iid}/stops").json()["best"] is None   # none beats them all
    assert http.get(f"/api/strategies/instances/nope:{SYMBOL}:4h/stops").status_code == 404
