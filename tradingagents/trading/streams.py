"""Live Binance streams held by the server: a last-price cache and the account's user data stream.

``PriceCache`` keeps one connection to the all-market mini-ticker stream and answers
``TradingService.price_lookup`` from memory, so an order's notional check needs no
round trip; a price older than ``STALE_SECONDS`` (or a symbol not seen since the last
reconnect) reads as None and the service falls back to the REST ticker.

``AccountStream`` subscribes to the user data stream through the WebSocket API's
``userDataStream.subscribe.signature`` (the REST listenKey flow is deprecated), seeds
balances from one REST account read, then applies ``outboundAccountPosition`` and
``executionReport`` events. Every browser on ``/ws/account`` gets its own bounded
queue; a full queue drops its oldest message, so a slow client never delays the
stream or the other clients.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator

from websockets.asyncio.client import connect as ws_connect

from tradingagents.trading.client import BinanceError
from tradingagents.trading.service import TradingService, order_view

logger = logging.getLogger(__name__)

# Market data for the mode's exchange: testnet orders are checked against testnet prices.
MARKET_URLS = {
    "live": "wss://data-stream.binance.vision/stream?streams=",
    "testnet": "wss://stream.testnet.binance.vision/stream?streams=",
}
ACCOUNT_URLS = {
    "live": "wss://ws-api.binance.com:443/ws-api/v3",
    "testnet": "wss://ws-api.testnet.binance.vision/ws-api/v3",
}

STALE_SECONDS = 5.0
QUEUE_SIZE = 64
BACKOFF_MIN, BACKOFF_MAX = 1.0, 60.0
STABLE_SECONDS = 30.0       # a connection that lived this long resets the backoff
REPLY_TIMEOUT = 10.0

Connect = Callable[..., contextlib.AbstractAsyncContextManager]


async def _reconnecting(name: str, url: str, connect: Connect,
                        consume: Callable[[object], Awaitable[None]], dropped: Callable[[], None]) -> None:
    """Run ``consume`` on a connection to ``url`` forever, reconnecting with exponential backoff."""
    delay = BACKOFF_MIN
    while True:
        opened = time.monotonic()
        try:
            async with connect(url) as ws:
                await consume(ws)
        except Exception as exc:
            # The URLs carry no secrets and BinanceError messages are redacted.
            logger.warning("%s stream dropped: %s: %s", name, type(exc).__name__, exc)
        dropped()
        if time.monotonic() - opened >= STABLE_SECONDS:
            delay = BACKOFF_MIN
        await asyncio.sleep(delay)
        delay = min(delay * 2, BACKOFF_MAX)


async def _stop(task: asyncio.Task | None) -> None:
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class PriceCache:
    """Last prices of the tradable symbols from their ``<symbol>@miniTicker`` streams.

    Only the symbols the guard allows are subscribed, so each message is one small
    ticker rather than the whole market. Freshness is the connection's (time of its
    last message), not each symbol's; prices are dropped on disconnect so a reconnect
    never serves a price from before the gap.
    """

    @staticmethod
    def url_for(mode: str, symbols: Iterable[str]) -> str:
        return MARKET_URLS[mode] + "/".join(f"{s.lower()}@miniTicker" for s in symbols)

    def __init__(self, url: str, connect: Connect = ws_connect, clock: Callable[[], float] = time.monotonic):
        self.url = url
        self.prices: dict[str, float] = {}
        self.updated = -math.inf
        self._connect, self._clock = connect, clock
        self._task: asyncio.Task | None = None

    def get(self, symbol: str) -> float | None:
        if self._clock() - self.updated > STALE_SECONDS:
            return None
        return self.prices.get(symbol)

    def start(self) -> None:
        self._task = asyncio.create_task(
            _reconnecting("Price", self.url, self._connect, self._consume, self.prices.clear), name="price-cache"
        )

    async def close(self) -> None:
        await _stop(self._task)

    async def _consume(self, ws) -> None:
        async for message in ws:
            ticker = json.loads(message)["data"]
            self.prices[ticker["s"]] = float(ticker["c"])
            self.updated = self._clock()


def _offer(queue: asyncio.Queue, message: dict | None) -> None:
    """Enqueue without waiting; a full queue loses its oldest message."""
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(message)


def _balance(raw: dict) -> dict:
    return {"asset": raw["a"], "free": float(raw["f"]), "locked": float(raw["l"])}


def _report_order(event: dict) -> dict:
    """An executionReport in the REST order shape ``order_view`` reads."""
    return order_view({
        "symbol": event["s"], "orderId": event["i"], "side": event["S"], "type": event["o"],
        "price": event["p"], "origQty": event["q"], "executedQty": event["z"],
        "cummulativeQuoteQty": event["Z"], "status": event["X"], "time": event["T"],
    })


class AccountStream:
    """Balances and order updates of the configured account, fanned out to subscribers.

    ``state`` is "unconfigured" (no keys; nothing connects), "connecting", "live" or
    "offline" (dropped, reconnecting).
    """

    def __init__(self, service: TradingService, url: str | None = None, connect: Connect = ws_connect):
        self.service = service
        self.url = url or ACCOUNT_URLS[service.mode]
        self.state = "connecting" if service.client.configured else "unconfigured"
        self.balances: dict[str, dict] = {}
        self._connect = connect
        self._queues: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self.service.on_status = self._broadcast_status
        if self.service.client.configured:
            self._task = asyncio.create_task(
                _reconnecting("Account", self.url, self._connect, self._consume, self._dropped),
                name="account-stream",
            )

    async def close(self) -> None:
        await _stop(self._task)
        self.service.on_status = lambda: None
        self._publish(None)   # tells every /ws/account handler to close

    def status_event(self) -> dict:
        return {"type": "status", "state": self.state, "mode": self.service.mode,
                "enabled": self.service.guard.enabled}

    def snapshot_event(self) -> dict:
        return {"type": "snapshot", "balances": list(self.balances.values())}

    @contextlib.contextmanager
    def subscribe(self) -> Iterator[asyncio.Queue]:
        """A queue that starts with the status (and the snapshot when live), then gets every update."""
        queue: asyncio.Queue = asyncio.Queue(QUEUE_SIZE)
        queue.put_nowait(self.status_event())
        if self.state == "live":
            queue.put_nowait(self.snapshot_event())
        self._queues.add(queue)
        try:
            yield queue
        finally:
            self._queues.discard(queue)

    def _publish(self, message: dict | None) -> None:
        for queue in self._queues:
            _offer(queue, message)

    def _broadcast_status(self) -> None:
        self._publish(self.status_event())

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self._broadcast_status()

    def _dropped(self) -> None:
        self._set_state("offline")

    async def _consume(self, ws) -> None:
        await ws.send(json.dumps({
            "id": "account", "method": "userDataStream.subscribe.signature",
            "params": self.service.client.ws_signed(),
        }))
        async with asyncio.timeout(REPLY_TIMEOUT):
            reply = json.loads(await ws.recv())
        if reply.get("status") != 200:
            error = reply.get("error") or {}
            raise BinanceError(f"user data stream subscribe failed: {error.get('msg', reply.get('status'))}",
                               error.get("code"), reply.get("status"))
        # Subscribed first, then read: events after the snapshot queue up on the socket meanwhile.
        account = await self.service.account()
        seen = account.get("update_time") or 0
        self.balances = {b["asset"]: b for b in account["balances"]}
        self._set_state("live")
        self._publish(self.snapshot_event())
        async for message in ws:
            event = json.loads(message).get("event")
            kind = event.get("e") if isinstance(event, dict) else None
            if kind == "outboundAccountPosition" and event["u"] > seen:
                changed = [_balance(b) for b in event["B"]]
                for b in changed:
                    if b["free"] or b["locked"]:
                        self.balances[b["asset"]] = b
                    else:
                        self.balances.pop(b["asset"], None)
                self._publish({"type": "balances", "balances": changed})
            elif kind == "executionReport":
                self._publish({"type": "order", "order": _report_order(event)})
            elif kind in ("eventStreamTerminated", "serverShutdown"):
                return


_account: AccountStream | None = None


def get_account_stream() -> AccountStream:
    if _account is None:
        raise RuntimeError("AccountStream is not running; it starts with the app's lifespan")
    return _account


@contextlib.asynccontextmanager
async def running_streams(service: TradingService) -> AsyncIterator[AccountStream]:
    """Run the price cache (unless ``TRADING_PRICE_STREAM=off``) and the account stream for ``service``."""
    global _account
    prices = None
    if os.getenv("TRADING_PRICE_STREAM", "").strip().lower() != "off":
        prices = PriceCache(PriceCache.url_for(service.mode, service.guard.limits.allowed_symbols))
        prices.start()
        service.price_lookup = prices.get
    _account = AccountStream(service)
    _account.start()
    try:
        yield _account
    finally:
        await _account.close()
        _account = None
        if prices:
            await prices.close()
            service.price_lookup = lambda symbol: None
