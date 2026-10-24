"""Dashboard analysis runs: background graph runs, their events over SSE, cancel and history.

TradingAgentsGraph is replaced with a fake that replays a scripted stream, so
no model is called and no request leaves the process. Runs are saved in a
directory of the test's own.
"""
import datetime
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.web import analyze
from tradingagents.web.app import app
from tradingagents.web.trading import SESSION_TOKEN


@pytest.fixture(scope="module")
def http():
    # Started once, before the per-test network block (see test_web_market).
    with TestClient(app, base_url="http://127.0.0.1", headers={"X-TA-Session": SESSION_TOKEN}) as client:
        yield client


@pytest.fixture
def runs(monkeypatch, tmp_path):
    runs = analyze.Runs(tmp_path)
    monkeypatch.setattr(analyze, "RUNS", runs)
    return runs


def _call(tool, msg_id):
    return AIMessage(content="", id=msg_id, tool_calls=[{"name": tool, "args": {}, "id": f"c-{msg_id}"}])


MARKET_CALL = _call("get_stock_data", "m1")
NEWS_CALL = _call("get_news", "n1")


def _states(*deltas):
    """The run's state after each top-level step: each delta on top of the ones before."""
    state, out = {"messages": [], "market_report": "", "news_report": ""}, []
    for delta in deltas:
        state = {**state, **delta}
        out.append(([], state))
    return out


def _debate(count, speaker):
    return {"investment_debate_state": {"count": count, "current_response": f"{speaker} Analyst: argument"}}


def _risk(count, speaker):
    return {"risk_debate_state": {"count": count, "latest_speaker": speaker}}


ANALYST_STEPS = [
    ([MARKET_CALL], None),
    ([NEWS_CALL], None),
    # The analyst's report, with its message repeated: the call is reported once.
    ([MARKET_CALL], {"market_report": "Market is up"}),
    ([], {"news_report": "News is calm"}),
]
TEAM_STEPS = _states(
    {"market_report": "Market is up", "news_report": "News is calm"},
    _debate(1, "Bull"),
    _debate(2, "Bear"),
    {"investment_plan": "Plan: accumulate"},
    {"trader_investment_plan": "Buy 0.1 BTC"},
    _risk(1, "Aggressive"),
    _risk(2, "Conservative"),
    _risk(3, "Neutral"),
    {"final_trade_decision": "**Rating: Buy**", "final_rating": "Buy"},
)


class FakeGraph:
    instances = []
    steps = []

    def __init__(self, selected_analysts, config):
        self.selected_analysts, self.config = selected_analysts, config
        self.propagator = SimpleNamespace(get_graph_args=lambda: {"config": {}})
        self.conditional_logic = ConditionalLogic()
        self.recorded = None
        self.closed = threading.Event()
        FakeGraph.instances.append(self)

    def create_run_state(self, ticker, trade_date, asset_type):
        self.run = (ticker, trade_date, asset_type)
        return {"company_of_interest": ticker}

    def stream_run(self, graph_input, **args):
        try:
            for step in self.steps:
                if isinstance(step, Exception):
                    raise step
                if callable(step):
                    step()
                    continue
                yield step
        finally:
            self.closed.set()

    def record_decision(self, ticker, trade_date, final_state):
        self.recorded = (ticker, trade_date, final_state)


@pytest.fixture
def graph(monkeypatch):
    FakeGraph.instances = []
    FakeGraph.steps = []
    monkeypatch.setattr(analyze, "TradingAgentsGraph", FakeGraph)
    return FakeGraph


def _start(http, **body):
    response = http.post("/api/analyses", json={"symbol": "BTCUSDT", "date": "2026-10-20", **body})
    assert response.status_code == 201, response.text
    return response.json()


def _wait(run_id):
    for worker in [t for t in threading.enumerate() if t.name == f"analysis-{run_id}"]:
        worker.join(5)
        assert not worker.is_alive()


def _wait_for_events(run, count):
    for _ in range(100):
        if len(run.events) >= count:
            return
        threading.Event().wait(0.05)
    raise AssertionError(f"{run.id} has {len(run.events)} events, not {count}")


def _sse(response):
    return [json.loads(line[len("data: "):]) for line in response.text.splitlines() if line.startswith("data: ")]


def _bare(events):
    return [{k: v for k, v in e.items() if k not in ("id", "ts")} for e in events]


def _follow(http, run, on_event):
    """Follow a run's events the way the SSE endpoint sends them."""
    async def follow():
        out = []
        async for sse in analyze.analysis_events(run, None):
            out.append(sse.data)
            on_event(out)
        return out
    return http.portal.call(follow)


FULL_RUN = [
    {"type": "stage", "stage": "market", "status": "running", "agent": "Market Analyst"},
    {"type": "stage", "stage": "news", "status": "running", "agent": "News Analyst"},
    {"type": "activity", "agent": "Market Analyst", "tool": "get_stock_data"},
    {"type": "activity", "agent": "News Analyst", "tool": "get_news"},
    {"type": "report", "stage": "market", "section": "market_report", "title": "Market Analysis",
     "content": "Market is up"},
    {"type": "stage", "stage": "market", "status": "done", "agent": "Market Analyst"},
    {"type": "report", "stage": "news", "section": "news_report", "title": "News Analysis", "content": "News is calm"},
    {"type": "stage", "stage": "news", "status": "done", "agent": "News Analyst"},
    {"type": "stage", "stage": "research", "status": "running", "agent": "Bull Researcher"},
    {"type": "stage", "stage": "research", "status": "running", "agent": "Bear Researcher"},
    {"type": "stage", "stage": "research", "status": "running", "agent": "Research Manager"},
    {"type": "report", "stage": "research", "section": "investment_plan", "title": "Research Team Decision",
     "content": "Plan: accumulate"},
    {"type": "stage", "stage": "research", "status": "done", "agent": "Research Manager"},
    {"type": "stage", "stage": "trader", "status": "running", "agent": "Trader"},
    {"type": "report", "stage": "trader", "section": "trader_investment_plan", "title": "Trading Team Plan",
     "content": "Buy 0.1 BTC"},
    {"type": "stage", "stage": "trader", "status": "done", "agent": "Trader"},
    {"type": "stage", "stage": "risk", "status": "running", "agent": "Aggressive Analyst"},
    {"type": "stage", "stage": "risk", "status": "running", "agent": "Conservative Analyst"},
    {"type": "stage", "stage": "risk", "status": "running", "agent": "Neutral Analyst"},
    {"type": "stage", "stage": "risk", "status": "running", "agent": "Portfolio Manager"},
    {"type": "report", "stage": "risk", "section": "final_trade_decision", "title": "Portfolio Management Decision",
     "content": "**Rating: Buy**"},
    {"type": "stage", "stage": "risk", "status": "done", "agent": "Portfolio Manager"},
    {"type": "stage", "stage": "decision", "status": "running"},
    {"type": "decision", "rating": "Buy", "summary": "**Rating: Buy**"},
    {"type": "stage", "stage": "decision", "status": "done"},
    {"type": "status", "status": "done"},
]


@pytest.mark.unit
def test_tool_calls_name_their_analyst():
    # The attribution relies on no two analysts sharing a tool.
    assert len(analyze.TOOL_AGENTS) == sum(len(spec.tools) for spec in ANALYST_NODE_SPECS.values())
    assert analyze.TOOL_AGENTS["get_indicators"] == "Market Analyst"
    assert analyze.TOOL_AGENTS["get_global_news"] == "News Analyst"


@pytest.mark.unit
def test_run_streams_stages_activity_reports_and_decision(http, graph, runs):
    graph.steps = ANALYST_STEPS + TEAM_STEPS
    started = _start(http, analysts=["news", "market"])
    assert started["status"] == "running"
    assert started["analysts"] == ["market", "news"]
    _wait(started["id"])

    response = http.get(f"/api/analyses/{started['id']}/events")
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _sse(response)
    assert _bare(events) == [*FULL_RUN, {"type": "end"}]
    assert [e["id"] for e in events[:-1]] == list(range(1, len(FULL_RUN) + 1))

    [run] = graph.instances
    assert run.run == ("BTC-USD", "2026-10-20", "crypto")
    assert run.selected_analysts == ["market", "news"]
    assert run.config["data_vendors"]["core_stock_apis"] == "binance,yfinance"
    assert run.recorded[0:2] == ("BTC-USD", "2026-10-20")
    assert run.recorded[2]["final_rating"] == "Buy"

    detail = http.get(f"/api/analyses/{started['id']}").json()
    assert detail["status"] == "done" and detail["rating"] == "Buy" and detail["duration_s"] >= 0
    assert detail["decision"] == {"rating": "Buy", "summary": "**Rating: Buy**"}
    assert detail["reports"]["market_report"] == "Market is up"
    assert [(s["key"], s["status"], s["agent"]) for s in detail["stages"]] == [
        ("market", "done", "Market Analyst"), ("news", "done", "News Analyst"),
        ("research", "done", "Research Manager"), ("trader", "done", "Trader"),
        ("risk", "done", "Portfolio Manager"), ("decision", "done", None),
    ]
    assert all(s["started_at"] and s["finished_at"] for s in detail["stages"])

    [summary] = http.get("/api/analyses").json()
    assert summary["id"] == started["id"] and summary["status"] == "done" and summary["ticker"] == "BTC-USD"


@pytest.mark.unit
def test_date_defaults_to_today_and_analysts_to_all(http, graph, runs):
    started = http.post("/api/analyses", json={"symbol": "ETHUSDT"}).json()
    _wait(started["id"])
    assert graph.instances[0].run[:2] == ("ETH-USD", str(datetime.date.today()))
    assert graph.instances[0].selected_analysts == analyze.ANALYSTS


@pytest.mark.unit
@pytest.mark.parametrize("body", [{"symbol": "AAPL"}, {"analysts": ["fundamentals"]}, {"analysts": []}])
def test_bad_requests_start_nothing(http, graph, runs, body):
    assert http.post("/api/analyses", json={"symbol": "BTCUSDT", **body}).status_code == 422
    assert graph.instances == [] and runs.runs == {}


@pytest.mark.unit
def test_events_replay_then_follow_live(http, graph, runs):
    gate = threading.Event()
    graph.steps = [([MARKET_CALL], None), lambda: gate.wait(5), *ANALYST_STEPS[1:], *TEAM_STEPS]
    started = _start(http, analysts=["market", "news"])
    run = runs.runs[started["id"]]
    _wait_for_events(run, 3)   # the run is held after its first step
    released_after = []

    def release(out):
        if not gate.is_set():
            released_after.append(len(out))
            gate.set()

    events = _follow(http, run, release)
    assert released_after == [1]   # the first events were replayed, the rest came live
    assert _bare(events) == [*FULL_RUN, {"type": "end"}]


@pytest.mark.unit
def test_last_event_id_resumes_after_it(http, graph, runs):
    graph.steps = ANALYST_STEPS + TEAM_STEPS
    started = _start(http, analysts=["market", "news"])
    _wait(started["id"])
    events = _sse(http.get(f"/api/analyses/{started['id']}/events", headers={"Last-Event-ID": "20"}))
    assert [e.get("id") for e in events] == [21, 22, 23, 24, 25, 26, None]
    assert _bare(events) == [*FULL_RUN[20:], {"type": "end"}]


@pytest.mark.unit
def test_cancel_stops_the_run_at_its_next_step(http, graph, runs):
    gate = threading.Event()
    graph.steps = [([MARKET_CALL], None), lambda: gate.wait(5), *ANALYST_STEPS[1:], *TEAM_STEPS]
    started = _start(http, analysts=["market"])
    _wait_for_events(runs.runs[started["id"]], 2)
    cancelled = http.delete(f"/api/analyses/{started['id']}")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    gate.set()
    _wait(started["id"])

    [run] = graph.instances
    assert run.closed.is_set() and run.recorded is None
    events = _bare(_sse(http.get(f"/api/analyses/{started['id']}/events")))
    assert events == [
        {"type": "stage", "stage": "market", "status": "running", "agent": "Market Analyst"},
        {"type": "activity", "agent": "Market Analyst", "tool": "get_stock_data"},
        {"type": "status", "status": "cancelled"},
        {"type": "end"},
    ]
    # Cancelling an ended run changes nothing.
    assert http.delete(f"/api/analyses/{started['id']}").json()["status"] == "cancelled"


@pytest.mark.unit
def test_a_third_concurrent_run_is_refused(http, graph, runs):
    gate = threading.Event()
    graph.steps = [lambda: gate.wait(5)]
    try:
        first, second = _start(http), _start(http)
        response = http.post("/api/analyses", json={"symbol": "BTCUSDT"})
        assert response.status_code == 429
        assert "already running" in response.json()["detail"]
    finally:
        gate.set()
    _wait(first["id"])
    _wait(second["id"])
    assert _start(http)["status"] == "running"   # a finished run frees its place


@pytest.mark.unit
def test_failure_is_an_error_event_and_status(http, graph, runs):
    graph.steps = [([MARKET_CALL], None), RuntimeError("model unavailable")]
    started = _start(http, analysts=["market"])
    _wait(started["id"])
    events = _bare(_sse(http.get(f"/api/analyses/{started['id']}/events")))
    assert events[-3:] == [{"type": "error", "message": "model unavailable"},
                           {"type": "status", "status": "error"}, {"type": "end"}]
    assert http.get(f"/api/analyses/{started['id']}").json()["error"] == "model unavailable"
    assert graph.instances[0].recorded is None


@pytest.mark.unit
def test_runs_survive_a_restart(http, graph, runs, tmp_path):
    graph.steps = ANALYST_STEPS + TEAM_STEPS
    done = _start(http, analysts=["market", "news"])
    _wait(done["id"])
    gate = threading.Event()
    graph.steps = [([MARKET_CALL], None), lambda: gate.wait(5)]
    held = _start(http, analysts=["market"])
    _wait_for_events(runs.runs[held["id"]], 2)

    restarted = analyze.Runs(tmp_path)   # a new server, while the held run is still going
    assert restarted.detail(restarted.runs[done["id"]]) == runs.detail(runs.runs[done["id"]])
    reloaded = restarted.runs[held["id"]]
    assert reloaded.status == "error" and reloaded.error == "server restarted"
    # Tool calls are saved with the next stage or report: this one was not yet.
    assert _bare(reloaded.events) == [
        {"type": "stage", "stage": "market", "status": "running", "agent": "Market Analyst"},
        {"type": "error", "message": "server restarted"},
        {"type": "status", "status": "error"},
    ]
    # The restarted server saved what it found.
    assert json.loads((tmp_path / f"{held['id']}.json").read_text())["status"] == "error"
    runs.cancel(runs.runs[held["id"]])
    gate.set()
    _wait(held["id"])


@pytest.mark.unit
def test_starting_and_cancelling_need_the_session_token(http, graph, runs):
    no_token = {"X-TA-Session": ""}
    assert http.post("/api/analyses", json={"symbol": "BTCUSDT"}, headers=no_token).status_code == 403
    assert graph.instances == []
    started = _start(http, analysts=["market"])
    _wait(started["id"])
    assert http.delete(f"/api/analyses/{started['id']}", headers=no_token).status_code == 403
    assert http.get("/api/analyses", headers=no_token).status_code == 200
    assert http.get(f"/api/analyses/{started['id']}", headers=no_token).status_code == 200
    assert http.get("/api/analyses/nope", headers=no_token).status_code == 404
