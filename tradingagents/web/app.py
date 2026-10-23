"""FastAPI app for the live dashboard: the market data API, plus the built web UI when present.

The server is local-only. ``LocalOnly`` refuses a Host other than 127.0.0.1 or
localhost (DNS rebinding) and an Origin other than the page's own or the Vite dev
server, and requires the per-process session token on every state-changing
request and on every trading route.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

from tradingagents.dataflows.errors import NoMarketDataError, VendorError
from tradingagents.trading import TradeRejected
from tradingagents.trading.service import running_service
from tradingagents.trading.streams import running_streams
from tradingagents.web import analyze, chat, market, trading

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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with running_service() as service, running_streams(service):
        yield


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


@app.get("/api/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/session")
async def session() -> dict[str, str]:
    return {"token": trading.SESSION_TOKEN}


app.include_router(market.router)
app.include_router(chat.router)
app.include_router(analyze.router)
app.include_router(trading.router)
app.include_router(trading.ws_router)

# Frontend routes rank below API routes, so unknown paths fall back to index.html.
if WEBUI_DIST.is_dir():
    app.frontend("/", directory=WEBUI_DIST, fallback="index.html")
