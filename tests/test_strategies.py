"""Strategy library and backtester: exposure bounds, no look-ahead, exact costs, metrics."""

from __future__ import annotations

import math
import time

import numpy as np
import pandas as pd
import pytest

from tradingagents.strategies import (
    CATALOG,
    INTERVALS,
    backtest,
    bars_per_year,
    exposure_from_ratings,
    metrics,
)
from tradingagents.strategies.engine import Engine, Instance

RATINGS = ["Buy", "Overweight", "Hold", "Underweight", "Sell", None, None, None]


def make_candles(n: int, freq: str = "15min", seed: int = 7) -> pd.DataFrame:
    """A seeded random walk with consistent OHLC and a sparse AI rating column."""
    rng = np.random.default_rng(seed)
    close = 30_000 * np.exp(np.cumsum(rng.normal(0, 0.004, n)))
    open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.0005, n))
    wick = np.abs(rng.normal(0, 0.002, n))
    index = pd.date_range("2025-01-01", periods=n, freq=freq, tz="UTC", name="open_time")
    return pd.DataFrame({
        "open": open_,
        "high": np.maximum(open_, close) * (1 + wick),
        "low": np.minimum(open_, close) * (1 - wick),
        "close": close,
        "volume": rng.uniform(1, 100, n),
        "rating": rng.choice(np.array(RATINGS, dtype=object), n),
    }, index=index)


@pytest.fixture(scope="module")
def candles() -> pd.DataFrame:
    return make_candles(3000)


@pytest.mark.unit
def test_catalog_matches_contract():
    assert {key: s.params for key, s in CATALOG.items()} == {
        "sma_cross": {"fast": 50, "slow": 200},
        "ema_cross": {"fast": 12, "slow": 26},
        "macd": {"fast": 12, "slow": 26, "signal": 9},
        "rsi_reversion": {"period": 14, "enter_below": 30, "exit_above": 70},
        "bollinger_reversion": {"period": 20, "k": 2.0},
        "bollinger_breakout": {"period": 20, "k": 2.0},
        "donchian_breakout": {"entry": 20, "exit": 10},
        "supertrend": {"period": 10, "multiplier": 3.0},
        "momentum_roc": {"roc": 20, "sma": 50},
        "dca": {"steps": 20, "every_days": 7},
        "grid": {"levels": 5, "lookback": 30},
        "buy_and_hold": {},
        "tradingagents_ai": {},
    }
    assert CATALOG["tradingagents_ai"].intervals == ("1d",)
    assert CATALOG["sma_cross"].intervals == ("1m", "5m", "15m", "1h", "4h", "1d")


@pytest.mark.unit
@pytest.mark.parametrize("key", CATALOG)
def test_target_is_bounded_and_never_nan(candles, key):
    target = CATALOG[key].target(candles)
    assert target.index.equals(candles.index)
    assert target.notna().all()
    assert target.between(0.0, 1.0).all()
    # Past the longest warm-up (200 bars) every strategy takes positions.
    assert target.iloc[250:].gt(0).any()


@pytest.mark.unit
@pytest.mark.parametrize("key", CATALOG)
def test_truncating_future_candles_never_changes_past_targets(candles, key):
    full = CATALOG[key].target(candles)
    for cut in (201, 777, 1999):
        pd.testing.assert_series_equal(CATALOG[key].target(candles.iloc[:cut]), full.iloc[:cut])


def held_longer_than(window: int) -> pd.DataFrame:
    """Random walks around a crash, a zigzag, a steady fall, a rally, a steady rise and a flat:
    each latch takes a position and then sees no entry or exit for longer than ``window`` bars."""
    rng = np.random.default_rng(3)
    moves = [
        rng.normal(0, 0.004, 600),
        np.full(15, -0.01),                    # crash: RSI under 30, a close under the lower band and the 10-bar low
        np.tile([0.002, -0.002], 350),         # zigzag: RSI stays mid-range (rsi_reversion holds)
        np.full(700, -0.0005),                 # steady fall: under the middle band, over the lower (bollinger_reversion)
        rng.normal(0, 0.004, 300),
        np.full(15, 0.0133),                   # rally: over the upper band and the 20-bar high
        np.full(700, 0.0005),                  # steady rise: over the middle band, under the upper (bollinger_breakout)
        np.zeros(700),                         # flat: no new high or low (donchian_breakout)
        rng.normal(0, 0.004, 300),
    ]
    close = 30_000 * np.exp(np.cumsum(np.concatenate(moves)))
    open_ = np.r_[close[0], close[:-1]]
    index = pd.date_range("2025-01-01", periods=len(close), freq="15min", tz="UTC", name="open_time")
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close), "low": np.minimum(open_, close),
                         "close": close, "volume": 1.0}, index=index)


@pytest.mark.unit
@pytest.mark.parametrize("key", [k for k, s in CATALOG.items() if s.stateful])
def test_a_latch_carries_its_state_across_the_live_window_edge(key):
    """The engine's sliding window, a few bars at a time, matches the full history bar for bar."""
    window, step = 300, 7
    c = held_longer_than(window)
    full = CATALOG[key].target(c)
    held = full.groupby((full != full.shift()).cumsum()).transform("size")[full == 1]
    assert len(c) >= 3000 and held.max() > window

    engine = Engine(symbols=["BTCUSDT"], intervals=("15m",))
    inst = Instance(f"{key}:BTCUSDT:15m", CATALOG[key], "BTCUSDT", "15m",
                    pending=float(full.iloc[window - 1]), last_bar=c.index[window - 1])
    carried, restarted = [], []
    for end in range(window, len(c), step):
        frame = c.iloc[:end + step].iloc[-window:]   # the engine's window: the last bars, ending at the new ones
        new = frame.index > inst.last_bar
        target = engine._target(inst, frame, carry=True)[new]
        carried.append(target)
        restarted.append(CATALOG[key].target(frame)[new])   # flat at the window's edge, as before
        inst.pending, inst.last_bar = float(target.iloc[-1]), target.index[-1]
    pd.testing.assert_series_equal(pd.concat(carried), full.iloc[window:])
    assert not pd.concat(restarted).equals(full.iloc[window:])


@pytest.mark.unit
def test_ai_strategy_needs_ratings(candles):
    with pytest.raises(ValueError, match="rating"):
        CATALOG["tradingagents_ai"].target(candles.drop(columns="rating"))


@pytest.mark.unit
def test_rating_history_maps_to_exposure():
    ratings = pd.Series(["Buy", None, "Hold", " underweight", "REVIEW", "Sell", "Overweight"])
    assert exposure_from_ratings(ratings).tolist() == [1.0, 1.0, 1.0, 0.25, 0.25, 0.0, 0.75]
    assert exposure_from_ratings(pd.Series(["Hold", "Buy"]), initial=0.5).tolist() == [0.5, 1.0]


def _signal(candles: pd.DataFrame, on: int, off: int) -> pd.Series:
    target = pd.Series(0.0, index=candles.index)
    target.iloc[on:off] = 1.0
    return target


@pytest.mark.unit
def test_fills_happen_at_the_next_bar_open(candles):
    c = candles.iloc[:40]
    result = backtest(c, _signal(c, 5, 10), fee=0.001, slippage=0.0002)
    buy, sell = result.fills
    assert (buy["side"], buy["ts"], buy["exposure_after"]) == ("BUY", c.index[6], 1.0)
    assert (sell["side"], sell["ts"], sell["exposure_after"]) == ("SELL", c.index[11], 0.0)
    assert buy["price"] == c["open"].iloc[6] * 1.0002
    assert sell["price"] == c["open"].iloc[11] * 0.9998
    assert result.exposure.iloc[6:11].eq(1.0).all() and result.exposure.iloc[[5, 11]].eq(0.0).all()
    # Flat until the fill bar: the signal bar itself is still all cash.
    assert result.equity.iloc[:6].eq(1000.0).all()


@pytest.mark.unit
@pytest.mark.parametrize("last", [0.0, 1.0])
def test_a_target_that_never_fills_stays_all_cash(candles, last):
    # Regression: no fills (a flat target, or one that only rises on the last bar) raised IndexError.
    c = candles.iloc[:40]
    target = pd.Series(0.0, index=c.index)
    target.iloc[-1] = last
    result = backtest(c, target)
    assert result.fills == [] and result.equity.eq(1000.0).all()
    assert result.metrics["trades"] == 0 and result.metrics["return_pct"] == 0.0


@pytest.mark.unit
def test_fees_and_slippage_reduce_equity_exactly(candles):
    c = candles.iloc[:40]
    fee, slip = 0.001, 0.0002
    o6, o11 = c["open"].iloc[6], c["open"].iloc[11]
    qty = 1000.0 / (1 + fee) / (o6 * (1 + slip))

    result = backtest(c, _signal(c, 5, 10), fee=fee, slippage=slip)
    buy, sell = result.fills
    assert buy["qty"] == pytest.approx(qty, rel=1e-12)
    assert buy["quote"] + buy["fee"] == pytest.approx(1000.0, rel=1e-12)
    assert result.equity.iloc[8] == pytest.approx(qty * c["close"].iloc[8], rel=1e-12)
    expected = qty * o11 * (1 - slip) * (1 - fee)
    assert result.equity.iloc[-1] == pytest.approx(expected, rel=1e-12)
    assert sell["fee"] == pytest.approx(qty * o11 * (1 - slip) * fee, rel=1e-12)

    free = backtest(c, _signal(c, 5, 10), fee=0.0, slippage=0.0)
    assert free.equity.iloc[-1] == pytest.approx(1000.0 * o11 / o6, rel=1e-12)
    assert result.equity.iloc[-1] < free.equity.iloc[-1]


@pytest.mark.unit
def test_dca_steps_weekly_and_never_sells():
    c = make_candles(24 * 200, freq="1h")
    target = CATALOG["dca"].target(c)
    steps = np.flatnonzero(np.diff(target.to_numpy()))
    assert target.iloc[0] == 0.05 and target.iloc[-1] == 1.0
    assert np.all(np.diff(steps) == 24 * 7)  # N = bars in 7 days

    result = backtest(c, target)
    assert len(result.fills) == 20
    assert {f["side"] for f in result.fills} == {"BUY"}
    # Each step spends 1/(20 - k) of the remaining cash: equal slices of the capital.
    assert [f["quote"] + f["fee"] for f in result.fills] == pytest.approx([50.0] * 20)


@pytest.mark.unit
def test_grid_exposure_is_the_share_of_levels_above_price():
    # Range 100..110 for every bar; levels sit at 101, 103, 105, 107, 109.
    closes = [105.0] * 29 + [100.5, 102.0, 104.0, 106.0, 108.0, 109.5, 102.5]
    index = pd.date_range("2025-01-01", periods=len(closes), freq="1h", tz="UTC")
    c = pd.DataFrame({"open": closes, "high": 110.0, "low": 100.0, "close": closes, "volume": 1.0},
                     index=index)
    target = CATALOG["grid"].target(c)
    assert target.iloc[:29].eq(0.0).all()  # warm-up
    assert target.iloc[29:].tolist() == pytest.approx([1.0, 0.8, 0.6, 0.4, 0.2, 0.0, 0.8])


@pytest.mark.unit
def test_metrics_match_hand_calculation():
    index = pd.date_range("2025-01-01", periods=4, freq="1D", tz="UTC")
    equity = pd.Series([100.0, 110.0, 99.0, 121.0], index=index)
    fills = [
        {"ts": index[0], "side": "BUY", "price": 100.0, "qty": 1.0, "quote": 100.0, "fee": 0.1,
         "exposure_after": 1.0},
        {"ts": index[2], "side": "SELL", "price": 120.0, "qty": 0.5, "quote": 60.0, "fee": 0.06,
         "exposure_after": 0.5},
        {"ts": index[3], "side": "SELL", "price": 80.0, "qty": 0.5, "quote": 40.0, "fee": 0.04,
         "exposure_after": 0.0},
    ]
    close = pd.Series([50.0, 52.0, 51.0, 55.0], index=index)
    m = metrics(equity, fills, 365, close=close)

    r = [0.1, -0.1, 22 / 99]
    mean = sum(r) / 3
    std = math.sqrt(sum((x - mean) ** 2 for x in r) / 2)
    assert m["return_pct"] == pytest.approx(21.0)
    assert m["cagr_pct"] == pytest.approx((1.21 ** (365 / 3) - 1) * 100)
    assert m["sharpe"] == pytest.approx(mean / std * math.sqrt(365))
    assert m["sortino"] == pytest.approx(mean / math.sqrt(0.01 / 3) * math.sqrt(365))
    assert m["max_drawdown_pct"] == pytest.approx(-10.0)
    assert m["win_rate"] == 0.5
    assert m["trades"] == 3
    assert m["exposure_pct"] == pytest.approx(62.5)
    assert m["buy_hold_return_pct"] == pytest.approx(10.0)


@pytest.mark.unit
def test_metrics_of_a_flat_curve_have_no_ratios():
    index = pd.date_range("2025-01-01", periods=5, freq="1D", tz="UTC")
    m = metrics(pd.Series(1000.0, index=index), [], 365)
    assert m["return_pct"] == 0.0 and m["max_drawdown_pct"] == 0.0 and m["exposure_pct"] == 0.0
    assert m["sharpe"] is None and m["sortino"] is None and m["win_rate"] is None
    assert m["buy_hold_return_pct"] is None


@pytest.mark.unit
def test_bars_per_year():
    assert [bars_per_year(i) for i in INTERVALS] == [525600, 105120, 35040, 8760, 2190, 365]
    for bad in ("1M", "h", "0h", "15x"):
        with pytest.raises(ValueError):
            bars_per_year(bad)


@pytest.mark.unit
def test_whole_catalog_backtests_a_year_of_15m_candles_in_under_2s():
    c = make_candles(35_040)
    timings = {}
    start = time.perf_counter()
    for key, strategy in CATALOG.items():
        if key == "tradingagents_ai":  # live only
            continue
        t0 = time.perf_counter()
        backtest(c, strategy.target(c), periods_per_year=bars_per_year("15m"))
        timings[key] = time.perf_counter() - t0
    total = time.perf_counter() - start
    print("\n" + "\n".join(f"{k:22s} {v * 1000:7.1f} ms" for k, v in timings.items()))
    print(f"{'total':22s} {total * 1000:7.1f} ms")
    assert total < 2.0
