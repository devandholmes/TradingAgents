"""Price cache and account stream on fake websockets; /ws/account token check and first message.

``FakeConnect`` hands out scripted ``FakeSocket``s (or raises), so reconnects are
driven message by message; Binance REST stays the fake from test_trading_guard.
"""
import asyncio
import contextlib
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests import test_trading_guard
from tests.test_trading_guard import make_service
from tradingagents.trading import streams
from tradingagents.trading.client import sign
from tradingagents.trading.streams import QUEUE_SIZE, AccountStream, PriceCache
from tradingagents.web.app import app
from tradingagents.web.trading import SESSION_TOKEN

run = test_trading_guard.run   # the shared module-scoped event loop fixture

SUBSCRIBED = {"id": "account", "status": 200, "result": {"subscriptionId": 0}}
SNAPSHOT_TIME = 1_700_000_000_000   # FakeBinance's account updateTime
WS_URL = "ws://127.0.0.1:8000/ws/account"   # the middleware refuses TestClient's default "testserver" Host


class FakeSocket:
    """Yields what the test pushes; None closes it, an exception instance is raised."""

    def __init__(self, *messages):
        self.sent: list[dict] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        for message in messages:
            self.push(message)

    def push(self, message) -> None:
        self.inbox.put_nowait(message if message is None or isinstance(message, Exception) else json.dumps(message))

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        await asyncio.sleep(0)   # one frame per loop turn, as off a real network
        item = await self.inbox.get()
        if item is None:
            raise StopAsyncIteration
        if isinstance(item, Exception):
            raise item
        return item

    recv = __anext__


class FakeConnect:
    """Each connect takes the next outcome: a FakeSocket, or an exception to raise."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    @contextlib.asynccontextmanager
    async def __call__(self, url, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else FakeSocket()
        if isinstance(outcome, Exception):
            raise outcome
        yield outcome


async def until(condition, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.001)


def drain(queue: asyncio.Queue) -> list:
    return [queue.get_nowait() for _ in range(queue.qsize())]


def position(u: int, *balances: tuple[str, str, str]) -> dict:
    return {"subscriptionId": 0, "event": {
        "e": "outboundAccountPosition", "E": u, "u": u,
        "B": [{"a": a, "f": f, "l": locked} for a, f, locked in balances],
    }}


EXECUTION = {"subscriptionId": 0, "event": {
    "e": "executionReport", "E": SNAPSHOT_TIME + 5, "s": "BTCUSDT", "i": 42, "S": "BUY", "o": "MARKET",
    "p": "0.00000000", "q": "0.00020000", "z": "0.00020000", "Z": "20.00000000", "X": "FILLED",
    "T": SNAPSHOT_TIME + 5,
}}


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(streams, "BACKOFF_MIN", 0.0)


# --- PriceCache ---

def tick(symbol: str, close: str) -> dict:
    """A combined-stream miniTicker frame, as Binance sends it."""
    return {"stream": f"{symbol.lower()}@miniTicker", "data": {"s": symbol, "c": close}}


@pytest.mark.unit
def test_price_cache_subscribes_to_the_tradable_symbols_only():
    assert PriceCache.url_for("live", ("BTCUSDT", "ETHUSDT")) == (
        "wss://data-stream.binance.vision/stream?streams=btcusdt@miniTicker/ethusdt@miniTicker"
    )
    assert PriceCache.url_for("testnet", ("BTCUSDT",)).startswith("wss://stream.testnet.binance.vision/")


@pytest.mark.unit
def test_price_cache_serves_prices_until_the_stream_goes_quiet(run):
    clock = [100.0]
    socket = FakeSocket(tick("BTCUSDT", "65000.50"), tick("ETHUSDT", "3000.00"))
    cache = PriceCache("wss://fake", FakeConnect(socket), clock=lambda: clock[0])

    async def scenario():
        cache.start()
        await until(lambda: cache.get("ETHUSDT"))
        assert cache.get("BTCUSDT") == 65000.5
        assert cache.get("ETHUSDT") == 3000.0
        assert cache.get("SOLUSDT") is None
        clock[0] += 5.1
        assert cache.get("BTCUSDT") is None
        socket.push(tick("SOLUSDT", "150"))
        await until(lambda: cache.get("SOLUSDT"))
        # Any message refreshes the stream, and an unchanged ticker is still current.
        assert cache.get("BTCUSDT") == 65000.5
        await cache.close()

    run(scenario())
    assert cache._task.cancelled()


@pytest.mark.unit
def test_service_reads_the_cache_and_falls_back_to_rest_when_stale(run, tmp_path):
    service, fake = make_service(tmp_path)
    clock = [0.0]
    socket = FakeSocket(tick("BTCUSDT", "50000"))
    cache = PriceCache("wss://fake", FakeConnect(socket), clock=lambda: clock[0])
    service.price_lookup = cache.get

    async def scenario():
        cache.start()
        await until(lambda: cache.get("BTCUSDT"))
        assert await service._last_price("BTCUSDT") == 50000.0
        assert "/api/v3/ticker/price" not in fake.paths()
        clock[0] += 6
        assert await service._last_price("BTCUSDT") == fake.price
        assert "/api/v3/ticker/price" in fake.paths()
        await cache.close()

    run(scenario())


@pytest.mark.unit
def test_price_cache_reconnects_and_forgets_prices_from_before_the_gap(run, no_backoff):
    first, second = FakeSocket(tick("BTCUSDT", "1")), FakeSocket()
    connect = FakeConnect(OSError("refused"), first, second)
    cache = PriceCache("wss://fake", connect)

    async def scenario():
        cache.start()
        await until(lambda: cache.get("BTCUSDT") == 1.0)
        first.push(None)   # server closes
        second.push(tick("ETHUSDT", "2"))
        await until(lambda: cache.get("ETHUSDT") == 2.0)
        assert cache.get("BTCUSDT") is None
        await cache.close()

    run(scenario())
    assert connect.calls == 3


# --- AccountStream ---

@pytest.mark.unit
def test_account_stream_subscribes_signed_then_snapshots_and_applies_events(run, tmp_path):
    service, _ = make_service(tmp_path)
    socket = FakeSocket(SUBSCRIBED)
    stream = AccountStream(service, "wss://fake", FakeConnect(socket))

    async def scenario():
        with stream.subscribe() as one, stream.subscribe() as two:
            stream.start()
            await until(lambda: stream.state == "live")
            socket.push(position(SNAPSHOT_TIME - 1, ("BTC", "9", "9")))   # older than the snapshot
            socket.push(position(SNAPSHOT_TIME + 1, ("BTC", "0", "0"), ("ETH", "2.5", "0")))
            socket.push(EXECUTION)
            await until(lambda: one.qsize() >= 5)
            await stream.close()
            return drain(one), drain(two)

    one, two = run(scenario())
    assert one == two
    assert [m and m["type"] for m in one] == ["status", "status", "snapshot", "balances", "order", None]
    assert one[0]["state"] == "connecting" and one[1]["state"] == "live"
    assert {b["asset"] for b in one[2]["balances"]} == {"USDT", "BTC"}
    assert one[3]["balances"] == [
        {"asset": "BTC", "free": 0.0, "locked": 0.0}, {"asset": "ETH", "free": 2.5, "locked": 0.0},
    ]
    assert one[4]["order"]["order_id"] == 42 and one[4]["order"]["price"] == pytest.approx(100_000)
    assert set(stream.balances) == {"USDT", "ETH"}

    request = socket.sent[0]
    assert request["method"] == "userDataStream.subscribe.signature"
    params = dict(request["params"])
    signature = params.pop("signature")
    assert params["apiKey"] == "key"
    assert signature == sign("&".join(f"{k}={v}" for k, v in sorted(params.items())), "secret")


@pytest.mark.unit
def test_slow_subscriber_drops_its_oldest_and_never_holds_up_others(run, tmp_path):
    service, _ = make_service(tmp_path)
    socket = FakeSocket(SUBSCRIBED)
    stream = AccountStream(service, "wss://fake", FakeConnect(socket))
    events = QUEUE_SIZE * 3
    received = []

    async def reader(queue):
        while len(received) < events:
            message = await queue.get()
            if message["type"] == "balances":
                received.append(message)

    async def scenario():
        with stream.subscribe() as slow, stream.subscribe() as fast:
            task = asyncio.create_task(reader(fast))
            stream.start()
            await until(lambda: stream.state == "live")
            for n in range(events):
                socket.push(position(SNAPSHOT_TIME + 1 + n, ("ETH", str(n + 1), "0")))
            await asyncio.wait_for(task, 2)
            await stream.close()
            return drain(slow)

    slow = run(scenario())
    assert len(received) == events
    assert len(slow) == QUEUE_SIZE
    assert slow[-1] is None
    assert slow[-2]["balances"][0]["free"] == events   # newest kept, oldest (the status) dropped
    assert slow[0]["type"] == "balances"


@pytest.mark.unit
def test_account_stream_reconnects_after_a_failed_subscribe_without_logging_secrets(
        run, tmp_path, no_backoff, caplog):
    service, _ = make_service(tmp_path)
    refused = FakeSocket({"id": "account", "status": 401, "error": {"code": -2015, "msg": "Invalid API-key"}})
    accepted = FakeSocket(SUBSCRIBED)
    connect = FakeConnect(refused, OSError("reset"), accepted)
    stream = AccountStream(service, "wss://fake", connect)

    async def scenario():
        with stream.subscribe() as queue:
            stream.start()
            await until(lambda: stream.state == "live")
            await stream.close()
            return [m["state"] for m in drain(queue) if m and m["type"] == "status"]

    with caplog.at_level("WARNING"):
        states = run(scenario())
    assert states == ["connecting", "offline", "live"]
    assert connect.calls == 3
    assert "Invalid API-key" in caplog.text
    assert refused.sent[0]["params"]["signature"] not in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.unit
def test_kill_switch_changes_reach_subscribers(run, tmp_path):
    service, _ = make_service(tmp_path, keys=False)
    stream = AccountStream(service, "wss://fake", FakeConnect())

    async def scenario():
        with stream.subscribe() as queue:
            stream.start()   # no keys: nothing connects
            await service.set_enabled(False)
            await stream.close()
            return drain(queue)

    messages = run(scenario())
    assert messages[0] == {"type": "status", "state": "unconfigured", "mode": "testnet", "enabled": True}
    assert messages[1] == {"type": "status", "state": "unconfigured", "mode": "testnet", "enabled": False}
    assert stream._task is None


# --- /ws/account ---

@pytest.fixture(scope="module")
def http():
    # Started once, before the per-test network block (see test_web_market). No keys: the stream is unconfigured.
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        yield client


@pytest.mark.unit
@pytest.mark.parametrize("first", [{"token": "wrong"}, {}, ["token"], "not json"])
def test_ws_account_needs_the_session_token_first(http, first):
    with http.websocket_connect(WS_URL) as ws:
        ws.send_text(first if isinstance(first, str) else json.dumps(first))
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 1008


@pytest.mark.unit
def test_ws_account_rejects_a_foreign_origin(http):
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        http.websocket_connect(WS_URL, headers={"origin": "https://evil.example"}),
    ):
        pass
    assert closed.value.code == 1008


@pytest.mark.unit
def test_ws_account_reports_unconfigured_with_the_token(http):
    with http.websocket_connect(WS_URL) as ws:
        ws.send_json({"token": SESSION_TOKEN})
        message = ws.receive_json()
    assert message["type"] == "status"
    assert message["state"] == "unconfigured"
    assert message["mode"] == "testnet"
