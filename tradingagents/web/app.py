"""FastAPI app for the live dashboard: the market data API, plus the built web UI when present."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from tradingagents.dataflows.errors import NoMarketDataError, VendorError
from tradingagents.web import analyze, chat, market

WEBUI_DIST = Path(__file__).resolve().parents[2] / "webui" / "dist"

app = FastAPI(title="TradingAgents Dashboard")
# The Vite dev server; the built UI is served from this origin and needs no CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(VendorError)
async def vendor_error(request: Request, exc: VendorError) -> JSONResponse:
    status = 404 if isinstance(exc, NoMarketDataError) else 502
    return JSONResponse({"detail": str(exc)}, status_code=status)


@app.get("/api/health")
def health() -> dict[str, bool]:
    return {"ok": True}


app.include_router(market.router)
app.include_router(chat.router)
app.include_router(analyze.router)

# Frontend routes rank below API routes, so unknown paths fall back to index.html.
if WEBUI_DIST.is_dir():
    app.frontend("/", directory=WEBUI_DIST, fallback="index.html")
