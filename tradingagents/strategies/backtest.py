"""Simulate a target-exposure series on candles: fills, equity curve and metrics.

Execution model (identical for backtest and live paper trading): the target
decided at a bar's close is filled at the **next** bar's open, never the same
bar. A buy pays ``slippage`` above the open, a sell receives ``slippage`` below
it, and both pay ``fee`` on the traded notional.

Exposure moves in fractional steps, which is what DCA and grid need. Trades
happen only when the target changes, never to undo price drift, so a rising
target never sells:

- raising exposure from ``e`` to ``t`` spends ``(t - e) / (1 - e)`` of the cash,
  fee included (so ``t = 1`` spends it all);
- lowering it from ``e`` to ``t`` sells ``(e - t) / e`` of the holding
  (so ``t = 0`` sells it all).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
_YEAR_SECONDS = 365 * 86400  # crypto trades every day


def bars_per_year(interval: str) -> float:
    """Bars in a 365-day year for a Binance interval such as ``15m``, ``1h``, ``4h``, ``1d``."""
    count, unit = interval[:-1], interval[-1]
    if unit not in _UNIT_SECONDS or not count.isdigit() or int(count) == 0:
        raise ValueError(f"unsupported interval {interval!r}")
    return _YEAR_SECONDS / (int(count) * _UNIT_SECONDS[unit])


@dataclass(frozen=True)
class BacktestResult:
    equity: pd.Series    # mark-to-market at each bar's close
    exposure: pd.Series  # target exposure held through each bar
    fills: list[dict]    # {ts, side, price, qty, quote, fee, exposure_after}
    metrics: dict


def backtest(
    candles: pd.DataFrame,
    target: pd.Series,
    capital: float = 1000.0,
    fee: float = 0.001,
    slippage: float = 0.0002,
    periods_per_year: float | None = None,
) -> BacktestResult:
    """Trade ``target`` over ``candles``; ``periods_per_year`` defaults to the candle spacing."""
    opens = candles["open"].to_numpy(float)
    closes = candles["close"].to_numpy(float)
    wanted = target.reindex(candles.index).fillna(0.0).clip(0.0, 1.0).to_numpy(float)
    held = np.r_[0.0, wanted[:-1]]  # what each bar holds: the previous bar's decision
    changes = np.flatnonzero(np.diff(held, prepend=0.0))

    cash, qty, level = float(capital), 0.0, 0.0
    # State after each fill, behind the starting state (all cash) at index 0.
    cash_after, qty_after = np.full(len(changes) + 1, cash), np.zeros(len(changes) + 1)
    fills: list[dict] = []
    for j, i in enumerate(changes):
        new = float(held[i])
        if new > level:
            price = opens[i] * (1 + slippage)
            budget = cash * (new - level) / (1 - level)
            quote = budget / (1 + fee)
            traded = quote / price
            qty += traded
            cash -= budget
            side = "BUY"
        else:
            price = opens[i] * (1 - slippage)
            traded = qty * (level - new) / level
            quote = traded * price
            qty -= traded
            cash += quote * (1 - fee)
            side = "SELL"
        level = new
        cash_after[j + 1], qty_after[j + 1] = cash, qty
        fills.append({"ts": candles.index[i], "side": side, "price": price, "qty": traded,
                      "quote": quote, "fee": quote * fee, "exposure_after": new})

    # State after the latest fill at or before each bar (0: none yet, which a target that never invests keeps).
    last = np.searchsorted(changes, np.arange(len(candles)), side="right")
    equity = pd.Series(cash_after[last] + qty_after[last] * closes, index=candles.index, name="equity")

    if periods_per_year is None:
        periods_per_year = pd.Timedelta(days=365) / pd.Series(candles.index).diff().median()
    return BacktestResult(
        equity=equity,
        exposure=pd.Series(held, index=candles.index, name="exposure"),
        fills=fills,
        metrics=metrics(equity, fills, periods_per_year, close=candles["close"]),
    )


def metrics(equity: pd.Series, fills: list[dict], bars_per_year: float,
            close: pd.Series | None = None) -> dict:
    """Performance over the span of ``equity``; undefined ratios are None.

    Sharpe and Sortino use per-bar returns (risk-free 0) annualised by
    ``sqrt(bars_per_year)``. ``max_drawdown_pct`` is <= 0. ``win_rate`` is the
    share of sells that beat the average cost (fees included) of the holding
    they sold. ``exposure_pct`` is the time-weighted target exposure, read from
    ``fills`` (earlier fills may be included to carry exposure into a window).
    ``buy_hold_return_pct`` needs ``close``.
    """
    returns = equity.pct_change().dropna()
    periods = len(equity) - 1
    growth = equity.iloc[-1] / equity.iloc[0] if periods > 0 else 1.0

    std = returns.std()
    downside = math.sqrt((returns.clip(upper=0) ** 2).mean()) if len(returns) else 0.0
    scale = math.sqrt(bars_per_year)

    try:
        cagr = math.expm1(bars_per_year / periods * math.log(growth)) * 100 if periods and growth > 0 else None
    except OverflowError:  # a big move over a few bars has no meaningful annual rate
        cagr = None

    held = pd.Series([f["exposure_after"] for f in fills], index=[f["ts"] for f in fills], dtype=float)
    exposure = held.asof(equity.index).fillna(0.0) if len(held) else pd.Series(0.0, index=equity.index)

    return {
        "return_pct": (growth - 1) * 100,
        "cagr_pct": cagr,
        "sharpe": returns.mean() / std * scale if std > 0 else None,
        "sortino": returns.mean() / downside * scale if downside > 0 else None,
        "max_drawdown_pct": (equity / equity.cummax() - 1).min() * 100,
        "win_rate": _win_rate(fills),
        "trades": len(fills),
        "exposure_pct": float(exposure.mean()) * 100,
        "buy_hold_return_pct": (close.iloc[-1] / close.iloc[0] - 1) * 100 if close is not None else None,
    }


def _win_rate(fills: list[dict]) -> float | None:
    qty = cost = 0.0
    wins = sells = 0
    for f in fills:
        if f["side"] == "BUY":
            qty += f["qty"]
            cost += f["quote"] + f["fee"]
        elif qty > 0:  # a sell of a holding bought before these fills cannot be scored
            basis = cost * f["qty"] / qty
            wins += f["quote"] - f["fee"] > basis
            sells += 1
            cost -= basis
            qty -= f["qty"]
    return wins / sells if sells else None
