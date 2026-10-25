"""FastAPI app for the live dashboard: the market data API, plus the built web UI when present.

The server is local-only. ``LocalOnly`` refuses a Host other than 127.0.0.1 or
localhost (DNS rebinding) and an Origin other than the page's own or the Vite dev
server, and requires the per-process session token on every state-changing
request and on every trading route.
"""

import asyncio
import logging
import os
import subprocess
import sys
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

from tradingagents import db
from tradingagents.dataflows.errors import NoMarketDataError, VendorError
from tradingagents.trading import TradeRejected
from tradingagents.trading.service import get_service, running_service
from tradingagents.trading.streams import (
    STALE_SECONDS,
    PriceCache,
    get_account_stream,
    running_streams,
)
from tradingagents.web import ai_strategy, analyze, chat, market, strategies, trading

logger = logging.getLogger(__name__)

WEBUI_DIST = Path(__file__).resolve().parents[2] / "webui" / "dist"

DEV_ORIGIN = "http://localhost:5173"
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
TOKEN_PATHS = ("/api/trading/", "/api/account", "/api/orders", "/api/trades/")


class LocalOnly:
    """Pure ASGI middleware, so streamed responses and WebSockets pass through untouched."""

    def __init__(self, app: ASGIApp):
        self.app = app

    def _allowed(self, scope: Scope, headers: Headers) -> bool:
        host = headers.get("host", "")
        if urlsplit(f"//{host}").hostname not in ALLOWED_HOSTS:
            return False
        origin = headers.get("origin")
        if origin is not None and origin not in (f"http://{host}", DEV_ORIGIN):
            return False
        if scope["type"] != "http":   # /ws/account checks the token in its first message
            return True
        if scope["method"] in SAFE_METHODS and not scope["path"].startswith(TOKEN_PATHS):
            return True
        return trading.valid_token(headers.get("x-ta-session"))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or self._allowed(scope, Headers(scope=scope)):
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await WebSocket(scope, receive, send).close(code=1008)
        else:
            await JSONResponse({"detail": "forbidden"}, status_code=403)(scope, receive, send)


class EngineSupervisor:
    """The strategy engine child process, started again whenever it exits.

    Restarts back off exponentially (``BACKOFF_MIN``..``BACKOFF_MAX``; a run that lasted
    ``STABLE_SECONDS`` resets it). ``MAX_FAILURES`` exits within ``FAILURE_WINDOW`` seconds
    is a crash loop: it gives up, logs an error and ``/api/health`` reports ``gave_up``.
    """

    BACKOFF_MIN, BACKOFF_MAX = 1.0, 60.0
    STABLE_SECONDS = 60.0
    MAX_FAILURES, FAILURE_WINDOW = 10, 600.0
    POLL = 1.0   # seconds between exit checks; Popen, as Windows' SelectorEventLoop has no subprocesses

    def __init__(self, command: list[str] | None = None):
        self.command = command or [sys.executable, "-m", "tradingagents.strategies.engine"]
        self.process: subprocess.Popen | None = None
        self.restarts = 0
        self.gave_up = False
        self._failures: deque[float] = deque()
        self._task: asyncio.Task | None = None

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        self._task = asyncio.create_task(self._supervise(), name="engine-supervisor")

    async def _supervise(self) -> None:
        delay = self.BACKOFF_MIN
        while True:
            started = time.monotonic()
            self.process = subprocess.Popen(self.command)
            while (code := self.process.poll()) is None:
                await asyncio.sleep(self.POLL)
            now = time.monotonic()
            self._failures.append(now)
            while self._failures[0] < now - self.FAILURE_WINDOW:
                self._failures.popleft()
            if len(self._failures) >= self.MAX_FAILURES:
                self.gave_up = True
                logger.error("strategy engine exited %d times in %.0f s (last code %s); not restarting it",
                             len(self._failures), self.FAILURE_WINDOW, code)
                return
            if now - started >= self.STABLE_SECONDS:
                delay = self.BACKOFF_MIN
            logger.warning("strategy engine exited with code %s; restarting in %.0f s", code, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.BACKOFF_MAX)
            self.restarts += 1

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        if self.alive:
            self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait, 10)
            except subprocess.TimeoutExpired:   # never let a stuck engine skip the rest of the shutdown
                self.process.kill()


_engine: EngineSupervisor | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _engine
    # Migrated first; an offline database leaves its features offline and the rest running.
    async with db.running_database() as database, running_service() as service, running_streams(service):
        runs = await asyncio.to_thread(analyze.load_runs, db.database_url() if database else None)
        # The strategy engine is a child process, so its CPU work never touches this event loop.
        if database and os.environ.get("TRADINGAGENTS_ENGINE", "").strip().lower() != "off":
            _engine = EngineSupervisor()
            _engine.start()
        try:
            async with ai_strategy.running_scheduler(runs, database is not None):
                yield
        finally:
            if _engine:
                await _engine.close()
                _engine = None
            await asyncio.to_thread(runs.close)


app = FastAPI(title="TradingAgents Dashboard", lifespan=lifespan)
# The Vite dev server; the built UI is served from this origin and needs no CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[DEV_ORIGIN],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(LocalOnly)   # added last, so it runs first


@app.exception_handler(VendorError)
async def vendor_error(request: Request, exc: VendorError) -> JSONResponse:
    status = 404 if isinstance(exc, NoMarketDataError) else 502
    return JSONResponse({"detail": str(exc)}, status_code=status)


@app.exception_handler(TradeRejected)
async def trade_rejected(request: Request, exc: TradeRejected) -> JSONResponse:
    return JSONResponse({"detail": exc.reason}, status_code=422)


def _price_state() -> str:
    # While TRADING_PRICE_STREAM is on, the service's price lookup is the cache's own get.
    cache = getattr(get_service().price_lookup, "__self__", None)
    if not isinstance(cache, PriceCache):
        return "off"
    return "live" if time.monotonic() - cache.updated <= STALE_SECONDS else "connecting"


@app.get("/api/health")
async def health() -> dict:
    """``ok`` while the server answers (Docker's healthcheck), plus what is degraded.

    ``engine.last_bar_at`` is when the engine last wrote an instance's state (a closed bar
    traded, or its startup), ``lag_s`` how long ago that was by the database's clock.
    """
    database, last_bar_at, lag_s = False, None, None
    if (pool := db.pool()) is not None:
        try:
            async with pool.connection(timeout=2) as conn:
                cur = await conn.execute("SELECT max(updated_at), extract(epoch FROM now() - max(updated_at))::float8"
                                         " FROM instance_state")
                last_bar_at, lag_s = await cur.fetchone()
            database = True
        except psycopg.Error as exc:   # the pool's timeout is one too
            logger.warning("health: database check failed: %s", db.describe(exc))
    engine = _engine
    return {
        "ok": True,
        "engine": {"enabled": engine is not None, "alive": engine is not None and engine.alive,
                   "restarts": engine.restarts if engine else 0, "gave_up": engine is not None and engine.gave_up,
                   "last_bar_at": last_bar_at, "lag_s": None if lag_s is None else round(lag_s, 1)},
        "db": database,
        "streams": {"price": _price_state(), "account": get_account_stream().state},
    }


@app.get("/api/session")
async def session() -> dict[str, str]:
    return {"token": trading.SESSION_TOKEN}


app.include_router(market.router)
app.include_router(chat.router)
app.include_router(analyze.router)
app.include_router(trading.router)
app.include_router(trading.ws_router)
app.include_router(strategies.router)
app.include_router(strategies.ws_router)

# Frontend routes rank below API routes, so unknown paths fall back to index.html.
if WEBUI_DIST.is_dir():
    app.frontend("/", directory=WEBUI_DIST, fallback="index.html")
