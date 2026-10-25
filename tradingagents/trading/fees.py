"""Fees: the modelled paper fee rate, the maker-first wait, and real commissions read from Binance fills.

``paper_fee()`` is the one source of the fee rate every simulation pays per side:
engine paper fills, bot paper fills and the backtests' default. It is
``TRADING_PAPER_FEE`` (default 0.001, Binance's 0.1 % base rate).
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from decimal import Decimal

DEFAULT_PAPER_FEE = 0.001
DEFAULT_MAKER_WAIT_S = 15.0


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.getenv(name) or default)
    except ValueError:
        return default
    return value if low <= value <= high else default


def paper_fee() -> float:
    """The modelled fee rate per side; an unparseable or out-of-range value falls back to the default."""
    return _env_float("TRADING_PAPER_FEE", DEFAULT_PAPER_FEE, 0.0, 0.01)


def maker_wait_s() -> float:
    """How long a maker-first order rests before the remainder is cancelled (``TRADING_MAKER_WAIT_S``)."""
    return _env_float("TRADING_MAKER_WAIT_S", DEFAULT_MAKER_WAIT_S, 0.0, 300.0)


def commissions(fills: Iterable[dict]) -> dict[str, float]:
    """Commission per asset summed over Binance fills or trades (``commission`` + ``commissionAsset``)."""
    total: dict[str, Decimal] = {}
    for fill in fills:
        asset = fill.get("commissionAsset")
        if asset:
            total[asset] = total.get(asset, Decimal(0)) + Decimal(str(fill.get("commission") or 0))
    return {asset: float(amount) for asset, amount in total.items()}


def merge(*parts: dict[str, float]) -> dict[str, float]:
    total: dict[str, float] = {}
    for part in parts:
        for asset, amount in part.items():
            total[asset] = total.get(asset, 0.0) + amount
    return total


def fee_columns(commission: dict[str, float]) -> tuple[float | None, str | None]:
    """``(fee, fee_asset)`` for one ``bot_trades`` row.

    One order is charged in one asset in practice (BNB, or the asset received). If
    BNB ran out mid-order and the fills mix assets, the amounts cannot be summed:
    the fee is left null and ``fee_asset`` lists the assets (the audit log keeps the detail).
    """
    if not commission:
        return 0.0, None
    if len(commission) == 1:
        [(asset, amount)] = commission.items()
        return amount, asset
    return None, ",".join(sorted(commission))


def net_fill(side: str, qty: float, quote: float, commission: dict[str, float],
             base_asset: str, quote_asset: str) -> tuple[float, float]:
    """``(qty, quote)`` the account actually got or gave: a fee in the received asset comes off it.

    A BUY charged in the base asset receives ``qty - fee``; a SELL charged in the quote
    asset receives ``quote - fee``. A fee paid in BNB leaves both untouched.
    """
    if side == "BUY":
        return qty - commission.get(base_asset, 0.0), quote
    return qty, quote - commission.get(quote_asset, 0.0)
