"""Chat endpoint: the quick-think model answers questions about a symbol, streamed as SSE.

The system prompt carries a live snapshot of the symbol (24h ticker, indicators,
recent candles) from ``market.market_snapshot``, taken once per request. While the
TradingService runs, the model also gets account tools (balances, open orders,
placing and cancelling orders). Every tool goes through the service with
``source="agent"``, so the service's guard and audit log apply unchanged; the tool
loop only reports what the service did, as ``tool`` and ``trade`` events.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterable
from typing import Annotated, Literal

from fastapi import APIRouter, Header
from fastapi.concurrency import run_in_threadpool
from fastapi.sse import EventSourceResponse
from langchain_core.messages import ToolMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tradingagents.default_config import build_default_config
from tradingagents.llm_clients import create_tier_client
from tradingagents.trading import OrderRequest, TradeRejected, TradingService, get_service
from tradingagents.trading.guard import SYMBOL_PATTERN
from tradingagents.web import market, trading

SYSTEM_PROMPT = """You are the analysis assistant of a crypto market dashboard.
You explain market data and technical indicators. You are not a financial
adviser: your answers are not financial advice; say so when asked what to buy or sell.
{account}
Base your answers on the live snapshot below; say when it cannot answer a question.

Live snapshot for {symbol} ({interval} candles):
24h ticker: {ticker}
Indicators: {indicators}
Last {count} candles as [time (unix s), open, high, low, close, volume]:
{candles}"""

NO_ACCOUNT = "You cannot place, change or cancel trades, and have no access to any account."

TRADING_PROMPT = """You can read and trade the user's Binance Spot account through your tools.
Account: {mode}. Trading is {enabled}{configured}.
Server-side limits: at most {max_order} USDT per order, {daily_cap} USDT per UTC day
({used} USDT used today), symbols {symbols} only. The server enforces them and rejects
anything outside them.
Place or cancel an order only when the user's latest message explicitly asks you to;
never trade on your own initiative or as a suggestion. After a trade tool returns,
state its exact result: the status, executed quantity and price, or the rejection
reason, word for word. Never invent fills, prices or order IDs."""

MAX_TOOL_ROUNDS = 5


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)
    symbol: str = Field(default="BTCUSDT", pattern=SYMBOL_PATTERN)
    interval: market.Interval = "1h"


# Tool schemas: the title is the tool name, the docstring its description.
class GetBalances(BaseModel):
    """The account's non-zero Spot balances."""

    model_config = ConfigDict(title="get_balances")


class GetOpenOrders(BaseModel):
    """The account's open Spot orders, for one symbol or all."""

    model_config = ConfigDict(title="get_open_orders")
    symbol: str | None = Field(default=None, pattern=SYMBOL_PATTERN, description="e.g. BTCUSDT; omit for all")


class PlaceOrder(BaseModel):
    """Place a Spot order. Give exactly one of quantity (base asset) or quote_amount (USDT,
    MARKET only); a LIMIT order needs a price, a MARKET order takes none."""

    model_config = ConfigDict(title="place_order")
    symbol: str = Field(pattern=SYMBOL_PATTERN, description="e.g. BTCUSDT")
    side: Literal["BUY", "SELL"]
    type: Literal["MARKET", "LIMIT"]
    quantity: float | None = None
    quote_amount: float | None = None
    price: float | None = None


class CancelOrder(BaseModel):
    """Cancel an open Spot order."""

    model_config = ConfigDict(title="cancel_order")
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    order_id: int = Field(ge=0)


TOOLS = {schema.model_config["title"]: schema for schema in (GetBalances, GetOpenOrders, PlaceOrder, CancelOrder)}


def _compact(value) -> str:
    return json.dumps(value, separators=(",", ":"))


def account_prompt(status: dict | None) -> str:
    if status is None:
        return NO_ACCOUNT
    limits = status["limits"]
    return TRADING_PROMPT.format(
        mode=status["mode"].upper(),
        enabled="ENABLED" if status["enabled"] else "DISABLED (kill switch off: every order is rejected)",
        configured="" if status["configured"] else ", but no API keys are configured",
        max_order=limits["max_order_usdt"],
        daily_cap=limits["daily_cap_usdt"],
        used=status["used_today_usdt"],
        symbols=", ".join(limits["allowed_symbols"]),
    )


def system_prompt(symbol: str, interval: str, status: dict | None = None) -> str:
    snapshot = market.market_snapshot(symbol, interval)
    candles = [[c["time"], c["open"], c["high"], c["low"], c["close"], c["volume"]]
               for c in snapshot["candles"]]
    return SYSTEM_PROMPT.format(
        account=account_prompt(status),
        symbol=symbol.upper(),
        interval=interval,
        ticker=_compact(snapshot["ticker"]),
        indicators=_compact(snapshot["indicators"]),
        count=len(candles),
        candles="\n".join(_compact(c) for c in candles),
    )


def _delta_text(content) -> str:
    # Streamed chunks keep their whitespace: it is what separates the tokens.
    # Some providers stream a list of content blocks instead of a string.
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return content or ""


def _running_service() -> TradingService | None:
    try:
        return get_service()
    except RuntimeError:
        return None


async def _call(service: TradingService, name: str, args: dict):
    """Run one tool through the service; raises ValidationError, TradeRejected or the service's errors."""
    if name not in TOOLS:
        raise TradeRejected(f"unknown tool {name}")
    parsed = TOOLS[name].model_validate(args)
    match parsed:
        case GetBalances():
            return await service.balances()
        case GetOpenOrders():
            return await service.open_orders(parsed.symbol)
        case PlaceOrder():
            return await service.place_order(OrderRequest(**parsed.model_dump()), "agent")
        case CancelOrder():
            return await service.cancel_order(parsed.symbol, parsed.order_id, "agent")


async def run_tool(service: TradingService, name: str, args: dict, authorized: bool) -> tuple[bool, object]:
    """``(ok, result)``; a failure's result is ``{"error": reason}``, never an exception."""
    try:
        if not authorized:
            raise TradeRejected("this chat request carries no valid X-TA-Session token, so account tools are refused")
        # Shielded: a client disconnecting mid-order must not cut the order's audit short.
        return True, await asyncio.shield(_call(service, name, args))
    except TradeRejected as exc:
        return False, {"error": exc.reason, "rejected": True}
    except ValidationError as exc:
        reason = "; ".join(e["msg"] for e in exc.errors())
        return False, {"error": f"invalid arguments: {reason}", "rejected": True}
    except Exception as exc:
        return False, {"error": str(exc) or type(exc).__name__}


def trade_event(ok: bool, result) -> dict:
    if ok:
        return {"status": "filled" if result.get("status") == "FILLED" else "new", "order": result}
    return {"status": "rejected" if result.get("rejected") else "error", "reason": result["error"]}


router = APIRouter(prefix="/api", tags=["chat"])


@router.post("/chat", response_class=EventSourceResponse)
async def chat(
    request: ChatRequest,
    x_ta_session: Annotated[str | None, Header()] = None,
) -> AsyncIterable[dict]:
    try:
        service = _running_service()
        status = await service.status() if service else None
        prompt = await run_in_threadpool(system_prompt, request.symbol, request.interval, status)
        llm = create_tier_client(build_default_config(), "quick").get_llm()
        if service:
            # No tool_choice: DeepSeek thinking models reject it (llm_clients/capabilities.py),
            # and the default lets every model choose between answering and calling a tool.
            llm = llm.bind_tools(list(TOOLS.values()))
        authorized = trading.valid_token(x_ta_session)
        messages: list = [("system", prompt), *((m.role, m.content) for m in request.messages)]
        for round_ in range(MAX_TOOL_ROUNDS + 1):
            reply = None
            async for chunk in llm.astream(messages):
                if text := _delta_text(chunk.content):
                    yield {"delta": text}
                reply = chunk if reply is None else reply + chunk
            if reply is None or not reply.tool_calls:
                break
            if round_ == MAX_TOOL_ROUNDS:
                yield {"error": f"Stopped: the model asked for more than {MAX_TOOL_ROUNDS} rounds of tools"}
                return
            messages.append(reply)
            for call in reply.tool_calls:
                name, args = call["name"], call["args"]
                yield {"tool": {"name": name, "args": args, "status": "running"}}
                ok, result = await run_tool(service, name, args, authorized)
                yield {"tool": {"name": name, "args": args, "status": "ok" if ok else "error", "result": result}}
                if name == "place_order":
                    yield {"trade": trade_event(ok, result)}
                messages.append(ToolMessage(_compact(result), tool_call_id=call["id"]))
    except Exception as exc:
        yield {"error": str(exc) or type(exc).__name__}
        return
    yield {"done": True}
