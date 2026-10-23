"""Chat agent trading: the tool loop over a scripted tool-calling LLM and a fake TradingService.

Nothing reaches Binance or a model: the service records what it was asked and
answers from a script, and the LLM replays fixed rounds of text and tool calls.
"""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessageChunk, ToolMessage

from tests.test_web_chat import SNAPSHOT
from tradingagents.trading import BinanceError, TradeRejected
from tradingagents.web import chat
from tradingagents.web.app import app
from tradingagents.web.trading import SESSION_TOKEN

FILLED = {"symbol": "BTCUSDT", "order_id": 7, "side": "BUY", "type": "MARKET", "price": 65000.0,
          "orig_qty": 0.00015, "executed_qty": 0.00015, "quote_qty": 9.75, "status": "FILLED", "time": 1}
BUY = {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quote_amount": 10}


@pytest.fixture(scope="module")
def http():
    # Started once, before the per-test network block (see test_web_market).
    with TestClient(app, base_url="http://127.0.0.1", headers={"X-TA-Session": SESSION_TOKEN}) as client:
        yield client


def _events(response):
    return [json.loads(line[len("data: "):]) for line in response.text.splitlines() if line.startswith("data: ")]


class FakeService:
    """Answers like TradingService; ``outcome`` is what place_order and balances return or raise."""

    def __init__(self, outcome=FILLED, enabled=True):
        self.outcome, self.enabled = outcome, enabled
        self.calls = []

    async def status(self):
        return {"mode": "testnet", "enabled": self.enabled, "configured": True, "can_trade": self.enabled,
                "limits": {"max_order_usdt": 50.0, "daily_cap_usdt": 200.0, "allowed_symbols": ["BTCUSDT", "ETHUSDT"]},
                "used_today_usdt": 12.5}

    async def balances(self):
        self.calls.append(("balances",))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return [{"asset": "USDT", "free": 100.0, "locked": 0.0}]

    async def open_orders(self, symbol=None):
        self.calls.append(("open_orders", symbol))
        return []

    async def place_order(self, req, source):
        self.calls.append(("place_order", req, source))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def cancel_order(self, symbol, order_id, source):
        self.calls.append(("cancel_order", symbol, order_id, source))
        return {**FILLED, "order_id": order_id, "status": "CANCELED"}


def call(name, args, id_="c1"):
    return AIMessageChunk(content="", tool_call_chunks=[{"name": name, "args": json.dumps(args), "id": id_, "index": 0}])


class ScriptedLLM:
    """Each astream call replays the next round (text pieces or tool-call chunks); the last round repeats."""

    def __init__(self, rounds):
        self.rounds = rounds
        self.requests, self.tools = [], None

    def bind_tools(self, tools):
        self.tools = tools
        return self

    async def astream(self, messages):
        self.requests.append(list(messages))
        for chunk in self.rounds[min(len(self.requests), len(self.rounds)) - 1]:
            yield AIMessageChunk(content=chunk) if isinstance(chunk, str) else chunk


@pytest.fixture
def setup(monkeypatch):
    def install(rounds, service):
        llm = ScriptedLLM(rounds)
        client = type("Client", (), {"get_llm": lambda self: llm})()
        monkeypatch.setattr(chat, "create_tier_client", lambda config, tier: client)
        monkeypatch.setattr(chat.market, "market_snapshot", lambda symbol, interval: SNAPSHOT)
        monkeypatch.setattr(chat, "_running_service", lambda: service)
        return llm
    return install


def ask(http, text="buy 10 USDT of BTC"):
    return _events(http.post("/api/chat", json={"messages": [{"role": "user", "content": text}]}))


@pytest.mark.unit
def test_place_order_runs_once_as_agent_and_feeds_the_result_back(http, setup):
    service = FakeService()
    llm = setup([[call("place_order", BUY)], ["Bought ", "0.00015 BTC."]], service)
    events = ask(http)

    [(_, req, source)] = service.calls
    assert source == "agent" and req.symbol == "BTCUSDT" and req.quote_amount == 10
    assert events == [
        {"tool": {"name": "place_order", "args": BUY, "status": "running"}},
        {"tool": {"name": "place_order", "args": BUY, "status": "ok", "result": FILLED}},
        {"trade": {"status": "filled", "order": FILLED}},
        {"delta": "Bought "}, {"delta": "0.00015 BTC."}, {"done": True},
    ]
    assert {t.model_config["title"] for t in llm.tools} == {"get_balances", "get_open_orders", "place_order", "cancel_order"}
    tool_message = llm.requests[1][-1]
    assert isinstance(tool_message, ToolMessage) and tool_message.tool_call_id == "c1"
    assert json.loads(tool_message.content) == FILLED


@pytest.mark.unit
def test_rejection_is_reported_to_model_and_user(http, setup):
    service = FakeService(TradeRejected("trading disabled"), enabled=False)
    llm = setup([[call("place_order", BUY)], ["Rejected: trading disabled."]], service)
    events = ask(http)

    assert {"trade": {"status": "rejected", "reason": "trading disabled"}} in events
    assert {"tool": {"name": "place_order", "args": BUY, "status": "error",
                     "result": {"error": "trading disabled", "rejected": True}}} in events
    assert events[-2:] == [{"delta": "Rejected: trading disabled."}, {"done": True}]
    assert json.loads(llm.requests[1][-1].content)["error"] == "trading disabled"
    # The prompt tells the model the account state and the limits.
    prompt = llm.requests[0][0][1]
    assert "TESTNET" in prompt and "DISABLED" in prompt and "50.0 USDT per order" in prompt
    assert "200.0 USDT per UTC day" in prompt and "BTCUSDT, ETHUSDT" in prompt and "explicitly asks" in prompt


@pytest.mark.unit
def test_tool_error_is_an_error_event_not_a_crash(http, setup):
    service = FakeService(BinanceError("Binance /api/v3/order returned HTTP 503"))
    setup([[call("place_order", BUY)], ["The exchange failed."]], service)
    events = ask(http)

    assert {"trade": {"status": "error", "reason": "Binance /api/v3/order returned HTTP 503"}} in events
    assert events[1]["tool"]["status"] == "error" and events[-1] == {"done": True}


@pytest.mark.unit
def test_invalid_arguments_are_rejected_without_reaching_the_service(http, setup):
    service = FakeService()
    bad = {"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "quote_amount": 10}
    setup([[call("place_order", bad)], ["Could not."]], service)
    events = ask(http)

    assert service.calls == []
    [trade] = [e["trade"] for e in events if "trade" in e]
    assert trade["status"] == "rejected" and trade["reason"].startswith("invalid arguments")


@pytest.mark.unit
def test_tool_rounds_stop_at_five(http, setup):
    service = FakeService()
    llm = setup([[call("get_balances", {})]], service)   # asks for a tool every round
    events = ask(http, "balances?")

    assert service.calls == [("balances",)] * chat.MAX_TOOL_ROUNDS
    assert len(llm.requests) == chat.MAX_TOOL_ROUNDS + 1
    assert events[-1] == {"error": "Stopped: the model asked for more than 5 rounds of tools"}
    assert {"done": True} not in events


@pytest.mark.unit
def test_plain_chat_is_unchanged(http, setup):
    service = FakeService()
    setup([["Hello", " there"]], service)
    assert ask(http, "hi") == [{"delta": "Hello"}, {"delta": " there"}, {"done": True}]
    assert service.calls == []


@pytest.mark.unit
def test_cancel_order_goes_through_the_service_as_agent(http, setup):
    service = FakeService()
    setup([[call("cancel_order", {"symbol": "BTCUSDT", "order_id": 7})], ["Cancelled."]], service)
    events = ask(http, "cancel order 7")

    assert service.calls == [("cancel_order", "BTCUSDT", 7, "agent")]
    assert events[1]["tool"]["status"] == "ok" and not any("trade" in e for e in events)


@pytest.mark.unit
def test_account_tools_need_a_session_token(http, setup, monkeypatch):
    # The middleware refuses a tokenless POST already; the tool loop checks the token on its own too.
    monkeypatch.setattr(chat, "trading", SimpleNamespace(valid_token=lambda token: False))
    service = FakeService()
    llm = setup([[call("place_order", BUY)], ["Refused."]], service)
    events = ask(http)

    assert service.calls == []
    [trade] = [e["trade"] for e in events if "trade" in e]
    assert trade["status"] == "rejected" and "X-TA-Session" in trade["reason"]
    assert "X-TA-Session" in llm.requests[1][-1].content
