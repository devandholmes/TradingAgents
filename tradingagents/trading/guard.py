"""Server-side trading limits: kill switch, allowed symbols, order and daily caps, filters, audit log.

The kill switch persists in ``trading_state.json`` and starts disabled. Every order
attempt, accepted or not, is one JSON line in ``trades.jsonl``. The daily cap sums
the accepted notional of the current UTC day, kept in memory and seeded from the
log at startup. Limits are in the quote asset, which is USDT for the allowed symbols.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from collections import deque
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from tradingagents.trading.client import SymbolRules

logger = logging.getLogger(__name__)

Mode = Literal["testnet", "live"]
Source = Literal["user", "agent", "bot"]
COUNTED = ("filled", "new", "error")   # audit statuses whose notional counts toward the daily cap
SYMBOL_PATTERN = r"^[A-Za-z0-9]{2,20}$"

RECENT_KEPT = 500


class TradeRejected(Exception):
    """An order the guard refused before it reached Binance; ``reason`` says why."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class OrderRequest(BaseModel):
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    side: Literal["BUY", "SELL"]
    # LIMIT_MAKER is post-only: Binance rejects it rather than let it take liquidity.
    type: Literal["MARKET", "LIMIT", "LIMIT_MAKER"]
    quantity: float | None = Field(default=None, gt=0)
    quote_amount: float | None = Field(default=None, gt=0)
    price: float | None = Field(default=None, gt=0)
    time_in_force: Literal["GTC", "IOC", "FOK"] = "GTC"

    @field_validator("symbol")
    @classmethod
    def _upper(cls, symbol: str) -> str:
        return symbol.upper()

    @model_validator(mode="after")
    def _consistent(self) -> OrderRequest:
        if (self.quantity is None) == (self.quote_amount is None):
            raise ValueError("give exactly one of quantity or quote_amount")
        if self.quote_amount is not None and self.type != "MARKET":
            raise ValueError("quote_amount is only for MARKET orders")
        if self.type != "MARKET" and self.price is None:
            raise ValueError(f"a {self.type} order needs a price")
        if self.type == "MARKET" and self.price is not None:
            raise ValueError("a MARKET order takes no price")
        return self


@dataclass(frozen=True)
class Limits:
    max_order_usdt: float = 50.0
    daily_cap_usdt: float = 200.0
    allowed_symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")

    @classmethod
    def from_env(cls) -> Limits:
        default = cls()
        symbols = os.getenv("TRADING_ALLOWED_SYMBOLS")
        return cls(
            max_order_usdt=float(os.getenv("TRADING_MAX_ORDER_USDT") or default.max_order_usdt),
            daily_cap_usdt=float(os.getenv("TRADING_DAILY_CAP_USDT") or default.daily_cap_usdt),
            allowed_symbols=tuple(s.strip().upper() for s in symbols.split(",") if s.strip())
            if symbols else default.allowed_symbols,
        )


def _round(value: Decimal, step: Decimal, rounding: str = ROUND_FLOOR) -> Decimal:
    return (value / step).to_integral_value(rounding) * step if step else value


def _text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def order_params(req: OrderRequest, rules: SymbolRules, price: float, reference: float | None = None) -> dict:
    """Binance order parameters, rounded to the symbol's filters; rejects what Binance would.

    ``price`` is the limit price, or the last price for a MARKET order by quantity.
    ``reference`` is the cached market price a LIMIT price must stay near; without it
    the band is left to Binance.
    """
    params = {"symbol": req.symbol, "side": req.side, "type": req.type}
    if req.quote_amount is not None:
        quote = _round(Decimal(str(req.quote_amount)), Decimal(1).scaleb(-rules.quote_precision))
        if rules.min_notional_market and quote < rules.min_notional:
            raise TradeRejected(f"order value {_text(quote)} is below the {_text(rules.min_notional)} minimum")
        return {**params, "quoteOrderQty": _text(quote)}

    market = req.type == "MARKET"
    if market:
        step, low, high = rules.market_step, rules.market_min_qty, rules.market_max_qty
    else:
        step, low, high = rules.step, rules.min_qty, rules.max_qty
    qty = _round(Decimal(str(req.quantity)), step)
    if qty <= 0 or qty < low:
        raise TradeRejected(f"quantity {_text(qty)} is below the {_text(low)} minimum")
    if high and qty > high:
        raise TradeRejected(f"quantity {_text(qty)} is above the {_text(high)} maximum")
    params["quantity"] = _text(qty)

    unit = Decimal(str(price))
    if not market:
        # Round toward the safe side of the user's limit: never pay more, never sell for less.
        unit = _round(unit, rules.tick, ROUND_FLOOR if req.side == "BUY" else ROUND_CEILING)
        if unit < rules.min_price or (rules.max_price and unit > rules.max_price):
            raise TradeRejected(f"price {_text(unit)} is outside the symbol's price range")
        down, up = (rules.bid_down, rules.bid_up) if req.side == "BUY" else (rules.ask_down, rules.ask_up)
        if reference and down and up:
            ref = Decimal(str(reference))
            low, high = ref * down, ref * up
            if not low <= unit <= high:
                raise TradeRejected(
                    f"limit price {_text(unit)} is too far from the market {_text(ref)}: "
                    f"Binance accepts {req.side} limits between {low:.2f} and {high:.2f}"
                )
        params["price"] = _text(unit)
        if req.type == "LIMIT":   # Binance refuses a timeInForce on LIMIT_MAKER
            params["timeInForce"] = req.time_in_force
    if (not market or rules.min_notional_market) and qty * unit < rules.min_notional:
        raise TradeRejected(f"order value {_text(qty * unit)} is below the {_text(rules.min_notional)} minimum")
    return params


def _today() -> datetime.date:
    return datetime.datetime.now(datetime.UTC).date()


@dataclass
class Guard:
    """Limits plus the persisted kill switch, the audit log and the day's accepted notional."""

    mode: Mode
    limits: Limits
    state_path: Path
    audit_path: Path
    enabled: bool = False
    _day: datetime.date = field(default_factory=_today)
    _used: float = 0.0
    _recent: deque = field(default_factory=lambda: deque(maxlen=RECENT_KEPT))

    def __post_init__(self) -> None:
        try:
            self.enabled = json.loads(self.state_path.read_text())["enabled"] is True
        except (OSError, ValueError, KeyError, TypeError):
            self.enabled = False
        self._seed()

    def _seed(self) -> None:
        try:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        today = self._day.isoformat()
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            self._recent.append(entry)
            # An "error" carries a notional only when the order was sent and got no answer.
            if (entry.get("mode") == self.mode and entry.get("status") in COUNTED
                    and str(entry.get("ts", "")).startswith(today)):
                self._used += float(entry.get("notional") or 0)

    def set_enabled(self, enabled: bool) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"enabled": enabled}))
        tmp.replace(self.state_path)
        self.enabled = enabled
        logger.warning("Trading %s (%s)", "enabled" if enabled else "disabled", self.mode)

    @property
    def used_today(self) -> float:
        if (today := _today()) != self._day:
            self._day, self._used = today, 0.0
        return self._used

    def check_access(self, symbol: str, configured: bool) -> None:
        if not self.enabled:
            raise TradeRejected("trading disabled")
        if not configured:
            raise TradeRejected("Binance API keys not configured")
        if symbol not in self.limits.allowed_symbols:
            raise TradeRejected(f"{symbol} is not an allowed symbol")

    def reserve(self, notional: float) -> None:
        """Check the order and daily caps and count ``notional`` against today's total.

        Check and count happen without an await between them, so concurrent orders
        cannot both slip under the cap.
        """
        if notional > self.limits.max_order_usdt:
            raise TradeRejected(
                f"order value {notional:.2f} USDT exceeds the {self.limits.max_order_usdt:g} USDT per-order limit"
            )
        if self.used_today + notional > self.limits.daily_cap_usdt:
            raise TradeRejected(
                f"daily cap: {self._used:.2f} + {notional:.2f} USDT exceeds {self.limits.daily_cap_usdt:g} USDT"
            )
        self._used += notional

    def release(self, notional: float) -> None:
        self._used = max(0.0, self._used - notional)

    def record(self, source: Source, req: OrderRequest, status: str, **extra) -> dict:
        entry = {
            "ts": datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds"),
            "mode": self.mode,
            "source": source,
            "request": req.model_dump(exclude_none=True),
            "status": status,
            **{k: v for k, v in extra.items() if v is not None},
        }
        # ponytail: a one-line append on the event loop costs microseconds; move to a writer task if it ever shows.
        # A failed write must not turn an executed order into an error the caller might retry.
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as log:
                log.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except OSError:
            logger.exception("Could not write the trade audit log %s", self.audit_path)
        self._recent.append(entry)
        return entry

    def recent(self, limit: int = 50) -> list[dict]:
        return list(self._recent)[::-1][:limit]
