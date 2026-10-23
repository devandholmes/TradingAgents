"""Full-analysis endpoint: one TradingAgentsGraph run on a crypto symbol, streamed as SSE.

The run is driven like the CLI's (``cli/run.py``): the same initial state, the
same ``stream_run`` loop, the same report sections and the same decision record.
It runs in a worker thread so a run of several minutes does not hold the event
loop; its events reach the response through an asyncio queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import threading
from collections.abc import AsyncIterable, Callable

from fastapi import APIRouter
from fastapi.sse import EventSourceResponse
from pydantic import BaseModel, Field

from cli.display import MessageBuffer
from cli.models import AnalystType, AssetType
from cli.prompts import filter_analysts_for_asset_type
from tradingagents.agents.rating import run_rating
from tradingagents.dataflows.symbols import crypto_base, normalize_symbol
from tradingagents.default_config import build_default_config
from tradingagents.graph.trading_graph import TradingAgentsGraph

logger = logging.getLogger(__name__)

# Binance first for candles and indicators; the router skips a vendor that is not registered.
MARKET_VENDORS = "binance,yfinance"
ANALYSTS = [a.value for a in filter_analysts_for_asset_type(list(AnalystType), AssetType.CRYPTO)]


class AnalyzeRequest(BaseModel):
    symbol: str = Field(default="BTCUSDT", pattern=r"^[A-Za-z0-9-]{2,20}$")
    date: datetime.date | None = None


def run_config() -> dict:
    config = build_default_config()
    config["data_vendors"] = {
        **config["data_vendors"],
        "core_stock_apis": MARKET_VENDORS,
        "technical_indicators": MARKET_VENDORS,
    }
    return config


def run_analysis(ticker: str, trade_date: str, emit: Callable[[dict], None], stop: threading.Event) -> None:
    """Run the graph on ``ticker``, emitting progress, report and decision events.

    Returns early once ``stop`` is set, between graph steps: a step under way
    (an LLM call) finishes first.
    """
    graph = TradingAgentsGraph(ANALYSTS, config=run_config())
    emit({"type": "progress", "agent": "System", "message": f"Analyzing {ticker} on {trade_date}"})
    init_state = graph.create_run_state(ticker, trade_date, AssetType.CRYPTO.value)
    args = graph.propagator.get_graph_args()

    seen_ids, sent, final_state = set(), {}, {}
    with contextlib.closing(graph.stream_run(init_state, **args)) as stream:
        for messages, chunk in stream:
            if stop.is_set():
                return
            for message in messages:
                msg_id = getattr(message, "id", None)
                if msg_id is not None:
                    if msg_id in seen_ids:
                        continue
                    seen_ids.add(msg_id)
                for call in getattr(message, "tool_calls", None) or []:
                    name = call["name"] if isinstance(call, dict) else call.name
                    agent = getattr(message, "name", None) or "Agent"
                    emit({"type": "progress", "agent": agent, "message": f"Calling {name}"})

            if chunk is None:   # a step inside an analyst's graph: messages only
                continue
            # Streamed chunks are per-node deltas; merged, they are the final state.
            final_state.update(chunk)
            for section, (_, agent) in MessageBuffer.REPORT_SECTIONS.items():
                content = chunk.get(section)
                if isinstance(content, str) and content.strip() and sent.get(section) != content:
                    sent[section] = content
                    emit({"type": "progress", "agent": agent, "message": f"{agent} finished"})
                    emit({"type": "report", "section": section, "content": content})

    graph.record_decision(ticker, trade_date, final_state)
    emit({
        "type": "decision",
        "rating": run_rating(final_state),
        "content": final_state.get("final_trade_decision", ""),
    })


router = APIRouter(prefix="/api", tags=["analyze"])


@router.post("/analyze", response_class=EventSourceResponse)
async def analyze(request: AnalyzeRequest) -> AsyncIterable[dict]:
    if crypto_base(request.symbol) is None:
        yield {"type": "error", "message": f"{request.symbol} is not a supported crypto symbol"}
        yield {"type": "done"}
        return
    ticker = normalize_symbol(request.symbol)
    trade_date = str(request.date or datetime.date.today())

    loop = asyncio.get_running_loop()
    events: asyncio.Queue[dict | None] = asyncio.Queue()
    stop = threading.Event()

    def emit(event: dict | None) -> None:
        if not stop.is_set():
            loop.call_soon_threadsafe(events.put_nowait, event)

    def work() -> None:
        try:
            run_analysis(ticker, trade_date, emit, stop)
        except Exception as exc:
            logger.exception("Analysis of %s on %s failed", ticker, trade_date)
            emit({"type": "error", "message": str(exc) or type(exc).__name__})
        finally:
            emit(None)

    threading.Thread(target=work, name=f"analyze-{ticker}", daemon=True).start()
    try:
        while (event := await events.get()) is not None:
            yield event
    finally:
        # The client went away (or the run ended): the worker stops at its next step.
        stop.set()
    yield {"type": "done"}
