"""Chat endpoint: the quick-think model answers questions about a symbol, streamed as SSE.

The system prompt carries a live snapshot of the symbol (24h ticker, indicators,
recent candles) from ``market.market_snapshot``, taken once per request.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable
from typing import Literal

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.sse import EventSourceResponse
from pydantic import BaseModel, Field

from tradingagents.default_config import build_default_config
from tradingagents.llm_clients import create_tier_client
from tradingagents.web import market

SYSTEM_PROMPT = """You are the analysis assistant of a crypto market dashboard.
You explain market data and technical indicators, and only that. You are not a
financial adviser: your answers are not financial advice; say so when asked what to buy or sell.
You cannot place, change or cancel trades, and have no access to any account.
Base your answers on the live snapshot below; say when it cannot answer a question.

Live snapshot for {symbol} ({interval} candles):
24h ticker: {ticker}
Indicators: {indicators}
Last {count} candles as [time (unix s), open, high, low, close, volume]:
{candles}"""


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)
    symbol: str = Field(default="BTCUSDT", pattern=r"^[A-Za-z0-9]{2,20}$")
    interval: market.Interval = "1h"


def _compact(value) -> str:
    return json.dumps(value, separators=(",", ":"))


def system_prompt(symbol: str, interval: str) -> str:
    snapshot = market.market_snapshot(symbol, interval)
    candles = [[c["time"], c["open"], c["high"], c["low"], c["close"], c["volume"]]
               for c in snapshot["candles"]]
    return SYSTEM_PROMPT.format(
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


router = APIRouter(prefix="/api", tags=["chat"])


@router.post("/chat", response_class=EventSourceResponse)
async def chat(request: ChatRequest) -> AsyncIterable[dict]:
    try:
        prompt = await run_in_threadpool(system_prompt, request.symbol, request.interval)
        llm = create_tier_client(build_default_config(), "quick").get_llm()
        messages = [("system", prompt), *((m.role, m.content) for m in request.messages)]
        async for chunk in llm.astream(messages):
            if text := _delta_text(chunk.content):
                yield {"delta": text}
    except Exception as exc:
        yield {"error": str(exc) or type(exc).__name__}
        return
    yield {"done": True}
