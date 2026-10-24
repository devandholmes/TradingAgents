"""Strategy catalog: every strategy maps candles to a target exposure in [0, 1].

``candles`` is a DataFrame indexed by bar open time with ``open``, ``high``,
``low``, ``close`` and ``volume`` columns, oldest first. A strategy's value for
a bar is decided from that bar's close and earlier only, and is acted on at the
next bar's open (see ``backtest``). Spot is long/flat: 0 is all cash, 1 is fully
invested, and fractions (DCA, grid) hold part of the capital.

Every indicator is causal (rolling windows, recursive EMAs, a forward loop for
SuperTrend), so truncating future candles never changes a past target. Bars
before an indicator has warmed up are flat.

A stateful strategy (a latch: long from an entry until an exit) can hold a target
for longer than any window, so over a window of recent candles it needs the
target it held before them (see ``Strategy.target``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

Family = Literal["trend", "mean_reversion", "breakout", "momentum", "accumulation", "benchmark", "ai"]

INTERVALS: tuple[str, ...] = ("15m", "1h", "4h", "1d")

# TradingAgents rating -> exposure. Hold (and any unparsed rating) keeps the previous exposure.
RATING_EXPOSURE: dict[str, float] = {"buy": 1.0, "overweight": 0.75, "underweight": 0.25, "sell": 0.0}


@dataclass(frozen=True)
class Strategy:
    key: str
    name: str
    family: Family
    description: str
    params: dict
    intervals: tuple[str, ...]
    fn: Callable[..., pd.Series] = field(repr=False, compare=False)
    stateful: bool = False   # a latch: each target depends on every earlier bar, not on a window of them

    def target(self, candles: pd.DataFrame, initial: float = 0.0, after: pd.Timestamp | None = None) -> pd.Series:
        """Exposure in [0, 1] per bar, indexed like ``candles``, never NaN.

        A stateful strategy holds ``initial`` until its first entry or exit; with
        ``after``, only entries and exits after that bar count (the bars up to it
        just warm the indicators up), so ``initial`` is the target held at ``after``.
        Stateless strategies ignore both.
        """
        latch = {"initial": initial, "after": after} if self.stateful else {}
        exposure = self.fn(candles, **self.params, **latch)
        return exposure.astype(float).fillna(0.0).clip(0.0, 1.0).rename(self.key)


def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def _above(a: pd.Series, b: pd.Series) -> pd.Series:
    """1 where ``a > b``, 0 where not, NaN until both are defined."""
    return (a > b).astype(float).where(a.notna() & b.notna())


def _latch(enter: pd.Series, exit_: pd.Series, initial: float = 0.0, after: pd.Timestamp | None = None) -> pd.Series:
    """Long from an entry bar until the next exit bar (exit wins a tie), ``initial`` before the first.

    Entries and exits at or before ``after`` are ignored.
    """
    state = pd.Series(np.nan, index=enter.index)
    state[enter.to_numpy(bool)] = 1.0
    state[exit_.to_numpy(bool)] = 0.0
    if after is not None:
        state[state.index <= after] = np.nan
    return state.ffill().fillna(initial)


def _bands(close: pd.Series, n: int, k: float) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = _sma(close, n)
    width = k * close.rolling(n, min_periods=n).std(ddof=0)
    return mid - width, mid, mid + width


def sma_cross(c: pd.DataFrame, fast: int, slow: int) -> pd.Series:
    return _above(_sma(c["close"], fast), _sma(c["close"], slow))


def ema_cross(c: pd.DataFrame, fast: int, slow: int) -> pd.Series:
    return _above(_ema(c["close"], fast), _ema(c["close"], slow))


def macd(c: pd.DataFrame, fast: int, slow: int, signal: int) -> pd.Series:
    line = _ema(c["close"], fast) - _ema(c["close"], slow)
    return _above(line, _ema(line, signal))


def rsi_reversion(c: pd.DataFrame, period: int, enter_below: float, exit_above: float, **latch) -> pd.Series:
    delta = c["close"].diff()
    gain = _wilder(delta.clip(lower=0), period)
    loss = _wilder(-delta.clip(upper=0), period)
    # loss == 0 gives rs = inf -> RSI 100, the conventional value for an all-up window.
    rsi = 100 - 100 / (1 + gain / loss)
    return _latch(rsi < enter_below, rsi > exit_above, **latch)


def bollinger_reversion(c: pd.DataFrame, period: int, k: float, **latch) -> pd.Series:
    lower, mid, _ = _bands(c["close"], period, k)
    return _latch(c["close"] < lower, c["close"] >= mid, **latch)


def bollinger_breakout(c: pd.DataFrame, period: int, k: float, **latch) -> pd.Series:
    _, mid, upper = _bands(c["close"], period, k)
    return _latch(c["close"] > upper, c["close"] < mid, **latch)


def donchian_breakout(c: pd.DataFrame, entry: int, exit: int, **latch) -> pd.Series:
    # Channels from the bars before this one, so a close can break them.
    high = c["high"].rolling(entry, min_periods=entry).max().shift(1)
    low = c["low"].rolling(exit, min_periods=exit).min().shift(1)
    return _latch(c["close"] > high, c["close"] < low, **latch)


def supertrend(c: pd.DataFrame, period: int, multiplier: float) -> pd.Series:
    high, low, close = (c[col].to_numpy(float) for col in ("high", "low", "close"))
    prev_close = np.r_[np.nan, close[:-1]]
    tr = np.fmax(high - low, np.fmax(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr = _wilder(pd.Series(tr), period).to_numpy()
    hl2 = (high + low) / 2
    basic_up, basic_dn = hl2 + multiplier * atr, hl2 - multiplier * atr

    # The final bands ratchet, so this is a forward recursion rather than a vector op.
    out = np.full(len(close), np.nan)
    upper = lower = np.nan
    up = False
    for i in range(len(close)):
        if np.isnan(atr[i]):
            continue
        if np.isnan(upper):  # first bar with an ATR
            upper, lower = basic_up[i], basic_dn[i]
            up = close[i] > hl2[i]
        else:
            if close[i - 1] > upper or basic_up[i] < upper:
                upper = basic_up[i]
            if close[i - 1] < lower or basic_dn[i] > lower:
                lower = basic_dn[i]
            if up and close[i] < lower:
                up = False
            elif not up and close[i] > upper:
                up = True
        out[i] = float(up)
    return pd.Series(out, index=c.index)


def momentum_roc(c: pd.DataFrame, roc: int, sma: int) -> pd.Series:
    close = c["close"]
    rate = close / close.shift(roc) - 1
    trend = _sma(close, sma)
    long = (rate > 0) & (close > trend)
    return long.astype(float).where(rate.notna() & trend.notna())


def dca(c: pd.DataFrame, steps: int, every_days: int) -> pd.Series:
    # Steps are timed from the first candle, so at any interval each lasts
    # N = bars in every_days, and the series only ever rises.
    elapsed = (c.index - c.index[0]) // pd.Timedelta(days=every_days)
    return pd.Series(np.minimum(1.0, (np.asarray(elapsed) + 1) / steps), index=c.index)


def grid(c: pd.DataFrame, levels: int, lookback: int) -> pd.Series:
    high = c["high"].rolling(lookback, min_periods=lookback).max()
    low = c["low"].rolling(lookback, min_periods=lookback).min()
    span = (high - low).to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        position = ((c["close"] - low).to_numpy() / span)[:, None]
    # Level i sits at (i + 0.5) / levels of the range: at the low every level is above, at the high none.
    marks = (np.arange(levels) + 0.5) / levels
    share = (marks[None, :] > position).sum(axis=1) / levels
    return pd.Series(share, index=c.index).where(span > 0)


def buy_and_hold(c: pd.DataFrame) -> pd.Series:
    return pd.Series(1.0, index=c.index)


def exposure_from_ratings(ratings: pd.Series, initial: float = 0.0) -> pd.Series:
    """Map a history of TradingAgents ratings to exposure.

    Buy 1.0, Overweight 0.75, Underweight 0.25, Sell 0.0; Hold, an unparsed
    rating or a missing value keeps the previous exposure, starting from
    ``initial``.
    """
    mapped = ratings.astype("string").str.strip().str.lower().map(RATING_EXPOSURE)
    return mapped.astype(float).ffill().fillna(initial)


def tradingagents_ai(c: pd.DataFrame) -> pd.Series:
    if "rating" not in c:
        raise ValueError("tradingagents_ai needs a 'rating' column: the latest ai_signal known at each bar")
    return exposure_from_ratings(c["rating"])


def _strategy(key: str, name: str, family: Family, description: str, fn: Callable[..., pd.Series],
              intervals: tuple[str, ...] = INTERVALS, stateful: bool = False, **params: object) -> Strategy:
    return Strategy(key, name, family, description, params, intervals, fn, stateful)


CATALOG: dict[str, Strategy] = {s.key: s for s in (
    _strategy("sma_cross", "SMA cross 50/200", "trend",
              "Long while the 50-bar SMA is above the 200-bar SMA.", sma_cross, fast=50, slow=200),
    _strategy("ema_cross", "EMA cross 12/26", "trend",
              "Long while the 12-bar EMA is above the 26-bar EMA.", ema_cross, fast=12, slow=26),
    _strategy("macd", "MACD 12/26/9", "trend",
              "Long while the MACD line is above its 9-bar signal line.", macd, fast=12, slow=26, signal=9),
    _strategy("rsi_reversion", "RSI reversion", "mean_reversion",
              "Buy when the 14-bar RSI drops below 30, sell when it rises above 70.",
              rsi_reversion, stateful=True, period=14, enter_below=30, exit_above=70),
    _strategy("bollinger_reversion", "Bollinger reversion", "mean_reversion",
              "Buy a close below the lower 20-bar, 2-sigma band; sell at the middle band.",
              bollinger_reversion, stateful=True, period=20, k=2.0),
    _strategy("bollinger_breakout", "Bollinger breakout", "breakout",
              "Buy a close above the upper 20-bar, 2-sigma band; sell below the middle band.",
              bollinger_breakout, stateful=True, period=20, k=2.0),
    _strategy("donchian_breakout", "Donchian breakout", "breakout",
              "Buy a close above the prior 20-bar high; sell a close below the prior 10-bar low.",
              donchian_breakout, stateful=True, entry=20, exit=10),
    _strategy("supertrend", "SuperTrend 10/3", "trend",
              "Long while the close holds above the SuperTrend line (ATR 10, multiplier 3).",
              supertrend, period=10, multiplier=3.0),
    _strategy("momentum_roc", "Momentum ROC", "momentum",
              "Long while the 20-bar rate of change is positive and the close is above the 50-bar SMA.",
              momentum_roc, roc=20, sma=50),
    _strategy("dca", "Weekly DCA", "accumulation",
              "Adds 1/20 of the capital every 7 days and never sells.", dca, steps=20, every_days=7),
    _strategy("grid", "Range grid", "mean_reversion",
              "Five levels across the rolling 30-bar range; holds the share of levels above the price.",
              grid, levels=5, lookback=30),
    _strategy("buy_and_hold", "Buy and hold", "benchmark",
              "Fully invested from the first bar: the benchmark.", buy_and_hold),
    _strategy("tradingagents_ai", "TradingAgents AI", "ai",
              "Daily TradingAgents analysis: Buy 1.0, Overweight 0.75, Hold keeps, Underweight 0.25, "
              "Sell 0. Live only: each run costs LLM credits, so it is never backtested.",
              tradingagents_ai, intervals=("1d",)),
)}
