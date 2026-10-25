"""Trading endpoints: status and kill switch, account, open orders, placing and cancelling orders, the audit log.

Every route here needs the ``X-TA-Session`` header (checked by the app's
middleware against ``SESSION_TOKEN``). Guard rejections answer 422 and Binance
failures 502, both as ``{"detail": reason}``.
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
from typing import Annotated

import anyio
import anyio.abc
from fastapi import APIRouter, Depends, Path, Query, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel

from tradingagents.trading import OrderRequest, TradingService, get_service
from tradingagents.trading.guard import SYMBOL_PATTERN
from tradingagents.trading.streams import get_account_stream

# Generated per process; GET /api/session hands it to same-origin pages only.
SESSION_TOKEN = secrets.token_urlsafe(32)

async def _service() -> TradingService:
    # An async dependency runs on the event loop; a plain def one would cost a threadpool hop per request.
    return get_service()


Service = Annotated[TradingService, Depends(_service)]
Symbol = Annotated[str, Path(pattern=SYMBOL_PATTERN)]


def valid_token(token: str | None) -> bool:
    return token is not None and hmac.compare_digest(token, SESSION_TOKEN)


class Enabled(BaseModel):
    enabled: bool


router = APIRouter(prefix="/api", tags=["trading"])


@router.get("/trading/status")
async def trading_status(service: Service) -> dict:
    # fees: {maker, taker, bnb_discount, source}, cached an hour by the service.
    return {**await service.status(), "fees": await service.fees()}


@router.post("/trading/enabled")
async def set_trading_enabled(body: Enabled, service: Service) -> dict:
    return await service.set_enabled(body.enabled)


@router.get("/account")
async def account(service: Service) -> dict:
    return await service.account()


@router.get("/orders/open")
async def open_orders(
    service: Service,
    symbol: Annotated[str | None, Query(pattern=SYMBOL_PATTERN)] = None,
) -> list[dict]:
    return await service.open_orders(symbol)


@router.post("/orders")
async def place_order(order: OrderRequest, service: Service) -> dict:
    return await service.place_order(order, "user")


@router.delete("/orders/{symbol}/{order_id}")
async def cancel_order(symbol: Symbol, order_id: Annotated[int, Path(ge=0)], service: Service) -> dict:
    return await service.cancel_order(symbol, order_id, "user")


@router.get("/trades/recent")
async def recent_trades(service: Service, limit: Annotated[int, Query(ge=1, le=500)] = 50) -> list[dict]:
    return service.recent_trades(limit)


# --- /ws/account: live balances and order updates (Task E) ---

TOKEN_TIMEOUT = 10.0

ws_router = APIRouter(tags=["trading"])


async def _forward(queue: asyncio.Queue, websocket: WebSocket, group: anyio.abc.TaskGroup) -> None:
    try:
        while (message := await queue.get()) is not None:
            await websocket.send_json(message)
        await websocket.close(code=status.WS_1001_GOING_AWAY)   # the server is shutting down
    except WebSocketDisconnect:
        pass
    group.cancel_scope.cancel()


@ws_router.websocket("/ws/account")
async def account_stream(websocket: WebSocket) -> None:
    """First message ``{"token"}``; then a status, a balance snapshot when live, and every update."""
    await websocket.accept()
    try:
        with anyio.fail_after(TOKEN_TIMEOUT):
            first = await websocket.receive_json()
    except WebSocketDisconnect:
        return
    except (TimeoutError, ValueError, KeyError):
        first = None
    if not isinstance(first, dict) or not valid_token(first.get("token")):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    # anyio, as Starlette's own disconnect listeners: the server's cancel scopes then unwind it cleanly.
    with get_account_stream().subscribe() as queue:
        async with anyio.create_task_group() as group:
            group.start_soon(_forward, queue, websocket, group)
            while (await websocket.receive())["type"] != "websocket.disconnect":
                pass
            group.cancel_scope.cancel()
