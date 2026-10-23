"""Dashboard full-analysis endpoint: a graph run streamed as SSE events.

TradingAgentsGraph is replaced with a fake that replays a scripted stream, so
no model is called and no request leaves the process.
"""
import datetime
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from tradingagents.web import analyze
from tradingagents.web.app import app


@pytest.fixture(scope="module")
def http():
    # Started once, before the per-test network block (see test_web_market).
    with TestClient(app) as client:
        yield client


def _events(response):
    return [json.loads(line[len("data: "):]) for line in response.text.splitlines() if line.startswith("data: ")]


TOOL_CALL = AIMessage(content="", id="m1", tool_calls=[{"name": "get_stock_data", "args": {}, "id": "c1"}])
FINAL = {
    "messages": [],
    "market_report": "Market is up",
    "investment_plan": "Plan: accumulate",
    "final_trade_decision": "**Rating: Buy**",
    "final_rating": "Buy",
}


class FakeGraph:
    instances = []
    steps = []

    def __init__(self, selected_analysts, config):
        self.selected_analysts, self.config = selected_analysts, config
        self.propagator = SimpleNamespace(get_graph_args=lambda: {"config": {}})
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
    monkeypatch.setattr(analyze, "TradingAgentsGraph", FakeGraph)
    return FakeGraph


@pytest.mark.unit
def test_analyze_streams_progress_reports_and_decision(http, graph):
    graph.steps = [
        ([TOOL_CALL], None),
        # The analyst's report, with its message repeated: the call is reported once.
        ([TOOL_CALL], {"market_report": "Market is up"}),
        ([], FINAL),
    ]
    response = http.post("/api/analyze", json={"symbol": "BTCUSDT", "date": "2026-10-20"})
    assert response.headers["content-type"].startswith("text/event-stream")
    assert _events(response) == [
        {"type": "progress", "agent": "System", "message": "Analyzing BTC-USD on 2026-10-20"},
        {"type": "progress", "agent": "Agent", "message": "Calling get_stock_data"},
        {"type": "progress", "agent": "Market Analyst", "message": "Market Analyst finished"},
        {"type": "report", "section": "market_report", "content": "Market is up"},
        # The full state repeats the market report; only new sections are sent.
        {"type": "progress", "agent": "Research Manager", "message": "Research Manager finished"},
        {"type": "report", "section": "investment_plan", "content": "Plan: accumulate"},
        {"type": "progress", "agent": "Portfolio Manager", "message": "Portfolio Manager finished"},
        {"type": "report", "section": "final_trade_decision", "content": "**Rating: Buy**"},
        {"type": "decision", "rating": "Buy", "content": "**Rating: Buy**"},
        {"type": "done"},
    ]

    [run] = graph.instances
    assert run.run == ("BTC-USD", "2026-10-20", "crypto")
    assert "fundamentals" not in run.selected_analysts
    assert run.config["data_vendors"]["core_stock_apis"] == "binance,yfinance"
    assert run.config["data_vendors"]["technical_indicators"] == "binance,yfinance"
    assert run.recorded[0:2] == ("BTC-USD", "2026-10-20")
    assert run.recorded[2]["final_rating"] == "Buy"


@pytest.mark.unit
def test_analyze_date_defaults_to_today(http, graph):
    graph.steps = []
    http.post("/api/analyze", json={"symbol": "ETHUSDT"})
    assert graph.instances[0].run[:2] == ("ETH-USD", str(datetime.date.today()))


@pytest.mark.unit
def test_analyze_failure_is_an_error_event_then_done(http, graph):
    graph.steps = [([TOOL_CALL], None), RuntimeError("model unavailable")]
    events = _events(http.post("/api/analyze", json={"symbol": "BTCUSDT"}))
    assert events[-2:] == [{"type": "error", "message": "model unavailable"}, {"type": "done"}]
    assert graph.instances[0].recorded is None
    assert not any(e["type"] == "decision" for e in events)


@pytest.mark.unit
def test_analyze_rejects_non_crypto_symbols(http, graph):
    events = _events(http.post("/api/analyze", json={"symbol": "AAPL"}))
    assert events == [{"type": "error", "message": "AAPL is not a supported crypto symbol"}, {"type": "done"}]
    assert graph.instances == []


@pytest.mark.unit
def test_client_disconnect_stops_the_run(http, graph):
    # The run blocks after its first step until the client has gone away.
    gone = threading.Event()
    graph.steps = [([TOOL_CALL], None), lambda: gone.wait(5), ([], FINAL)]

    async def read_one_then_leave():
        stream = analyze.analyze(analyze.AnalyzeRequest(symbol="BTCUSDT"))
        first = await stream.__anext__()
        await stream.aclose()
        return first

    first = http.portal.call(read_one_then_leave)
    gone.set()
    assert first["type"] == "progress"
    for worker in [t for t in threading.enumerate() if t.name == "analyze-BTC-USD"]:
        worker.join(5)
        assert not worker.is_alive()
    [run] = graph.instances
    assert run.closed.is_set()
    assert run.recorded is None
