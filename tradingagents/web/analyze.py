"""Analysis runs: TradingAgentsGraph runs on crypto symbols, in the background.

A run is driven like the CLI's (``cli/run.py``): the same initial state, the
same ``stream_run`` loop, the same report sections and the same decision record.
Each run works in a thread of its own, so it outlives the request that started
it and the page that watches it. Its events are kept in memory and in a JSON
file per run; any number of clients replay them over SSE and follow the rest live.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
import os
import threading
import uuid
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field

from cli.display import MessageBuffer
from cli.models import AnalystType, AssetType
from cli.prompts import filter_analysts_for_asset_type
from tradingagents.agents.rating import run_rating
from tradingagents.dataflows.symbols import crypto_base, normalize_symbol
from tradingagents.default_config import build_default_config
from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS
from tradingagents.graph.trading_graph import TradingAgentsGraph

logger = logging.getLogger(__name__)

# Binance first for candles and indicators; the router skips a vendor that is not registered.
MARKET_VENDORS = "binance,yfinance"
ANALYSTS = [a.value for a in filter_analysts_for_asset_type(list(AnalystType), AssetType.CRYPTO)]
MAX_RUNNING = 2

TEAM_STAGES = {"investment_plan": "research", "trader_investment_plan": "trader", "final_trade_decision": "risk"}
# Each report section: the stage it ends and the agent that writes it.
SECTION_STAGES = {section: key or TEAM_STAGES[section] for section, (key, _) in MessageBuffer.REPORT_SECTIONS.items()}
SECTION_AGENTS = {section: agent for section, (_, agent) in MessageBuffer.REPORT_SECTIONS.items()}
SECTION_TITLES = {
    "market_report": "Market Analysis",
    "sentiment_report": "Social Sentiment",
    "news_report": "News Analysis",
    "fundamentals_report": "Fundamentals Analysis",
    "investment_plan": "Research Team Decision",
    "trader_investment_plan": "Trading Team Plan",
    "final_trade_decision": "Portfolio Management Decision",
}
# Each analyst runs its tools in a graph node of its own, and no two analysts
# share a tool: a tool call names the analyst node that made it.
TOOL_AGENTS = {tool.name: spec.agent_node for spec in ANALYST_NODE_SPECS.values() for tool in spec.tools}
# The debates: their state, and the graph's own routing to the next speaker.
DEBATES = {
    "research": ("investment_debate_state", "should_continue_debate"),
    "risk": ("risk_debate_state", "should_continue_risk_analysis"),
}
# The stage after a team's, and the agent that starts it.
NEXT_STAGES = {"research": ("trader", "Trader"), "trader": ("risk", "Aggressive Analyst"), "risk": ("decision", None)}
TEAMS = ("research", "trader", "risk", "decision")


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds")


def run_config() -> dict:
    config = build_default_config()
    config["data_vendors"] = {
        **config["data_vendors"],
        "core_stock_apis": MARKET_VENDORS,
        "technical_indicators": MARKET_VENDORS,
    }
    return config


def run_analysis(
    ticker: str, trade_date: str, analysts: list[str], emit: Callable[[dict], None], stop: threading.Event
) -> str | None:
    """Run the graph on ``ticker``, emitting stage, activity, report and decision events.

    Returns the run's rating, or None once ``stop`` is set: it is checked between
    graph steps, so a step under way (an LLM call) finishes first.
    """
    graph = TradingAgentsGraph(analysts, config=run_config())
    init_state = graph.create_run_state(ticker, trade_date, AssetType.CRYPTO.value)
    args = graph.propagator.get_graph_args()

    stages: dict[str, tuple[str, str | None]] = {}

    def stage(key: str, status: str, agent: str | None = None) -> None:
        if stages.get(key) != (status, agent):
            stages[key] = (status, agent)
            emit({"type": "stage", "stage": key, "status": status, **({"agent": agent} if agent else {})})

    # The analysts start together.
    for key in analysts:
        stage(key, "running", ANALYST_NODE_SPECS[key].agent_node)

    seen_ids, turns, sent, final_state = set(), {}, {}, {}
    with contextlib.closing(graph.stream_run(init_state, **args)) as stream:
        for messages, chunk in stream:
            if stop.is_set():
                return None
            for message in messages:
                msg_id = getattr(message, "id", None)
                if msg_id is not None:
                    if msg_id in seen_ids:
                        continue
                    seen_ids.add(msg_id)
                for call in getattr(message, "tool_calls", None) or []:
                    name = call["name"] if isinstance(call, dict) else call.name
                    if agent := TOOL_AGENTS.get(name):
                        emit({"type": "activity", "agent": agent, "tool": name})

            if chunk is None:   # a step inside an analyst's graph: messages only
                continue
            # Streamed chunks are per-node deltas; merged, they are the final state.
            final_state.update(chunk)

            # A debate turn has landed: the graph's routing names who speaks next.
            for key, (debate_key, route) in DEBATES.items():
                count = (chunk.get(debate_key) or {}).get("count")
                if count and turns.get(key) != count:
                    turns[key] = count
                    stage(key, "running", getattr(graph.conditional_logic, route)(chunk))

            for section, key in SECTION_STAGES.items():
                content = chunk.get(section)
                if not (isinstance(content, str) and content.strip()) or sent.get(section) == content:
                    continue
                sent[section] = content
                emit({"type": "report", "stage": key, "section": section,
                      "title": SECTION_TITLES[section], "content": content})
                stage(key, "done", SECTION_AGENTS[section])
                if key in NEXT_STAGES:
                    next_key, agent = NEXT_STAGES[key]
                    stage(next_key, "running", agent)

            # The research debate starts once every analyst has filed.
            if "research" not in stages and all(stages[key][0] == "done" for key in analysts):
                stage("research", "running", "Bull Researcher")

    if stop.is_set():
        return None
    graph.record_decision(ticker, trade_date, final_state)
    rating = run_rating(final_state)
    emit({"type": "decision", "rating": rating, "summary": final_state.get("final_trade_decision", "")})
    stage("decision", "done")
    return rating


@dataclass
class Run:
    id: str
    symbol: str
    ticker: str
    date: str
    analysts: list[str]
    started_at: str
    status: str = "running"
    rating: str | None = None
    finished_at: str | None = None
    error: str | None = None
    events: list[dict] = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event, repr=False)
    # (loop, event) of each SSE client following the run, woken on each new event.
    waiters: set = field(default_factory=set, repr=False)

    def state(self) -> dict:
        return {k: v for k, v in vars(self).items() if k not in ("stop", "waiters")}

    def summary(self) -> dict:
        duration = None
        if self.finished_at:
            started, finished = (datetime.datetime.fromisoformat(t) for t in (self.started_at, self.finished_at))
            duration = round((finished - started).total_seconds(), 3)
        return {k: getattr(self, k) for k in ("id", "symbol", "ticker", "date", "analysts", "status", "rating",
                                              "started_at", "finished_at")} | {"duration_s": duration}


class Runs:
    """The runs of this server and of earlier ones, each saved as ``<directory>/<id>.json``."""

    # ponytail: one lock for every run; per-run locks if many runs ever stream at once.
    def __init__(self, directory: Path):
        self.directory = directory
        self.lock = threading.Lock()
        self.save_lock = threading.Lock()
        self.runs: dict[str, Run] = {}
        for path in directory.glob("*.json"):
            try:
                run = Run(**json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                logger.warning("Skipping unreadable analysis run %s", path)
                continue
            self.runs[run.id] = run
            if run.status == "running":   # its thread went with the server that ran it
                self.finish(run, "error", error="server restarted")

    def list(self, limit: int) -> list[dict]:
        with self.lock:
            runs = sorted(self.runs.values(), key=lambda r: r.started_at, reverse=True)[:limit]
            return [run.summary() for run in runs]

    def start(self, symbol: str, ticker: str, trade_date: str, analysts: list[str]) -> Run | None:
        """A new run, under way; None when ``MAX_RUNNING`` runs already are."""
        with self.lock:
            if sum(r.status == "running" for r in self.runs.values()) >= MAX_RUNNING:
                return None
            run = Run(id=uuid.uuid4().hex[:12], symbol=symbol, ticker=ticker, date=trade_date,
                      analysts=analysts, started_at=_now())
            self.runs[run.id] = run
        self.save(run)
        threading.Thread(target=self._work, args=(run,), name=f"analysis-{run.id}", daemon=True).start()
        return run

    def _work(self, run: Run) -> None:
        try:
            rating = run_analysis(run.ticker, run.date, run.analysts, lambda e: self.emit(run, e), run.stop)
        except Exception as exc:
            logger.exception("Analysis of %s on %s failed", run.ticker, run.date)
            self.finish(run, "error", error=str(exc) or type(exc).__name__)
        else:
            if rating is not None:
                self.finish(run, "done", rating=rating)

    def emit(self, run: Run, event: dict) -> None:
        # Tool calls are many and small: they are saved with the next stage or report.
        if self._record(run, [event]) and event["type"] != "activity":
            self.save(run)

    def finish(self, run: Run, status: str, error: str | None = None, rating: str | None = None) -> None:
        events = [{"type": "error", "message": error}] if error else []
        events.append({"type": "status", "status": status})
        if self._record(run, events, status=status, error=error, rating=rating, finished_at=_now()):
            self.save(run)

    def cancel(self, run: Run) -> dict:
        run.stop.set()
        self.finish(run, "cancelled")
        with self.lock:
            return run.summary()

    def _record(self, run: Run, events: list[dict], **changes) -> bool:
        """Append events to a run still running, and wake its followers; False once it has ended."""
        with self.lock:
            if run.status != "running":
                return False
            for event in events:
                run.events.append({"id": len(run.events) + 1, "ts": _now(), **event})
            for name, value in changes.items():
                setattr(run, name, value)
            waiters = list(run.waiters)
        for loop, wake in waiters:
            with contextlib.suppress(RuntimeError):   # the client's loop has closed
                loop.call_soon_threadsafe(wake.set)
        return True

    def save(self, run: Run) -> None:
        # The snapshot is taken under the save lock, so the last write is the newest state.
        with self.save_lock:
            with self.lock:
                text = json.dumps(run.state())
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / f"{run.id}.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)

    def detail(self, run: Run) -> dict:
        with self.lock:
            summary, events, error = run.summary(), list(run.events), run.error
        stages = {key: {"key": key, "status": "pending", "agent": None, "started_at": None, "finished_at": None}
                  for key in (*run.analysts, *TEAMS)}
        reports, decision = {}, None
        for event in events:
            if event["type"] == "stage":
                entry = stages[event["stage"]]
                entry["status"] = event["status"]
                entry["agent"] = event.get("agent") or entry["agent"]
                if event["status"] == "running":
                    entry["started_at"] = entry["started_at"] or event["ts"]
                else:
                    entry["finished_at"] = event["ts"]
            elif event["type"] == "report":
                reports[event["section"]] = event["content"]
            elif event["type"] == "decision":
                decision = {"rating": event["rating"], "summary": event["summary"]}
        return {**summary, "stages": list(stages.values()), "reports": reports, "decision": decision, "error": error}


# Beside the results directory: ~/.tradingagents/web_runs unless TRADINGAGENTS_RESULTS_DIR moves it.
RUNS = Runs(Path(build_default_config()["results_dir"]).parent / "web_runs")


class AnalysisRequest(BaseModel):
    symbol: str = Field(default="BTCUSDT", pattern=r"^[A-Za-z0-9-]{2,20}$")
    date: datetime.date | None = None
    analysts: list[str] | None = None


def get_run(run_id: str) -> Run:
    if (run := RUNS.runs.get(run_id)) is None:
        raise HTTPException(404, f"No analysis {run_id}")
    return run


RunDep = Annotated[Run, Depends(get_run)]

router = APIRouter(prefix="/api/analyses", tags=["analyses"])


@router.post("", status_code=201)
def start_analysis(request: AnalysisRequest) -> dict:
    if crypto_base(request.symbol) is None:
        raise HTTPException(422, f"{request.symbol} is not a supported crypto symbol")
    analysts = ANALYSTS if request.analysts is None else [a for a in ANALYSTS if a in request.analysts]
    unknown = set(request.analysts or ()) - set(ANALYSTS)
    if unknown or not analysts:
        raise HTTPException(422, f"Choose analysts from {', '.join(ANALYSTS)}")
    run = RUNS.start(request.symbol.upper(), normalize_symbol(request.symbol),
                     str(request.date or datetime.date.today()), analysts)
    if run is None:
        raise HTTPException(429, f"{MAX_RUNNING} analyses are already running: wait for one to finish or cancel it")
    return RUNS.detail(run)


@router.get("")
def list_analyses(limit: Annotated[int, Query(ge=1, le=200)] = 30) -> list[dict]:
    return RUNS.list(limit)


@router.get("/{run_id}")
def get_analysis(run: RunDep) -> dict:
    return RUNS.detail(run)


@router.delete("/{run_id}")
def cancel_analysis(run: RunDep) -> dict:
    return RUNS.cancel(run)


@router.get("/{run_id}/events", response_class=EventSourceResponse)
async def analysis_events(
    run: RunDep, last_event_id: Annotated[int | None, Header(ge=0)] = None
) -> AsyncIterable[ServerSentEvent]:
    """Every event after ``Last-Event-ID`` (all of them without one), then the live ones, then ``end``."""
    waiter = (asyncio.get_running_loop(), asyncio.Event())
    sent = last_event_id or 0
    with RUNS.lock:
        run.waiters.add(waiter)
    try:
        while True:
            waiter[1].clear()
            with RUNS.lock:
                events, running = run.events[sent:], run.status == "running"
            for event in events:
                yield ServerSentEvent(data=event, id=str(event["id"]))
            sent += len(events)
            if not running:   # its last events were appended with its final status
                break
            if not events:
                await waiter[1].wait()
    finally:
        with RUNS.lock:
            run.waiters.discard(waiter)
    yield ServerSentEvent(data={"type": "end"})
