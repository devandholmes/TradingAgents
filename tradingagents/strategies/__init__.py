"""Rule-based strategies and the backtester shared by the engine and the web API."""

from tradingagents.strategies.backtest import BacktestResult, backtest, bars_per_year, metrics
from tradingagents.strategies.library import CATALOG, INTERVALS, Strategy, exposure_from_ratings

__all__ = [
    "CATALOG", "INTERVALS", "BacktestResult", "Strategy",
    "backtest", "bars_per_year", "exposure_from_ratings", "metrics",
]
