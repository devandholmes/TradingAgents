"""Strategy Lab endpoints: catalog, leaderboard, instance detail, equity curves, the bot, and /ws/strategies.

Reads run on the shared async pool; while the database is offline every route here
answers 503 ``{"detail": "database offline"}``. One LISTEN connection (``Hub``) takes
the engine's NOTIFYs, nothing polls: ``ta_strategy_update`` becomes one leaderboard
frame per window, ``ta_bot_signal`` goes to the bot, and the bot's status after every
action is pushed too (the bot's risk monitor runs in this process as well). Each time
it (re)connects, startup included, the bot reconciles to its instance's target, so a signal sent while nobody listened is not lost. Every /ws/strategies client has its own bounded queue that drops
its oldest frame when full, so a slow browser never delays the others or the bot.
POST routes need the session token (the app's middleware checks it).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Annotated, Literal

import anyio
import psycopg
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    HTTPException,
    Path,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from psycopg import sql
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from tradingagents import db
from tradingagents.strategies import CATALOG
from tradingagents.strategies.backtest import PRESETS
from tradingagents.trading.bot import Bot, jsonable, rows
from tradingagents.trading.streams import QUEUE_SIZE, _offer, _reconnecting, _stop
from tradingagents.web.trading import TOKEN_TIMEOUT, _forward, valid_token

logger = logging.getLogger(__name__)

UPDATE_CHANNEL = "ta_strategy_update"
BOT_CHANNEL = "ta_bot_signal"

Window = Literal["7d", "30d", "90d", "1y", "live"]
SPANS = {"7d": "7 days", "30d": "30 days", "90d": "90 days", "1y": "365 days", "live": None}
Sort = Literal["return_pct", "sharpe", "sortino", "max_drawdown_pct", "win_rate", "trades", "exposure_pct", "equity"]
InstanceId = Annotated[str, Path(max_length=64)]
MAX_EQUITY_IDS = 50

LEADERBOARD = """
SELECT m.period, i.id AS instance_id, i.strategy_key, i.symbol, i.interval,
       m.return_pct, m.sharpe, m.sortino, m.max_drawdown_pct, m.win_rate, m.trades, m.exposure_pct,
       s.exposure, s.equity, i.live_since, m.updated_at
FROM strategy_metrics m
JOIN strategy_instances i ON i.id = m.instance_id
LEFT JOIN instance_state s ON s.instance_id = m.instance_id
WHERE (%(window)s::text IS NULL OR m.period = %(window)s)
  AND (%(symbol)s::text IS NULL OR i.symbol = %(symbol)s)
  AND (%(interval)s::text IS NULL OR i.interval = %(interval)s)
  AND (%(ids)s::text[] IS NULL OR i.id = ANY(%(ids)s))
ORDER BY {sort} DESC NULLS LAST, i.id
LIMIT %(limit)s
"""

# Normalised to 100 at each curve's first point in the window and thinned in Postgres
# to at most ``points`` per curve: every k-th point, plus the last.
EQUITY = """
WITH w AS (
    SELECT instance_id, ts, equity,
           row_number() OVER c AS rn, count(*) OVER (PARTITION BY instance_id) AS n,
           first_value(equity) OVER c AS base
    FROM equity_points
    WHERE instance_id = ANY(%(ids)s)
      AND (%(span)s::interval IS NULL AND source = 'live' OR ts >= now() - %(span)s::interval)
    WINDOW c AS (PARTITION BY instance_id ORDER BY ts)
)
SELECT instance_id, (extract(epoch FROM ts) * 1000)::bigint AS t, equity / NULLIF(base, 0) * 100 AS v
FROM w
WHERE n <= %(points)s OR rn = n OR (rn - 1) %% ceil(n::numeric / (%(points)s - 1))::int = 0
ORDER BY instance_id, ts
"""


def _named(row: dict) -> dict:
    strategy = CATALOG.get(row["strategy_key"])
    return {**row, "name": strategy.name if strategy else row["strategy_key"],
            "family": strategy.family if strategy else None}


async def leaderboard_rows(conn, *, window: str | None = None, symbol: str | None = None,
                           interval: str | None = None, ids: list[str] | None = None,
                           sort: str = "sharpe", limit: int = 500) -> list[dict]:
    """Leaderboard rows, each with its ``period``: of one window, or of all of them."""
    query = sql.SQL(LEADERBOARD).format(sort=sql.Identifier(sort))
    found = await rows(conn, query, {"window": window, "symbol": symbol, "interval": interval,
                                     "ids": ids, "limit": limit})
    return [jsonable(_named(r)) for r in found]


@contextlib.asynccontextmanager
async def _listen_connection(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    async with await psycopg.AsyncConnection.connect(url, autocommit=True, **db.SESSION) as conn:
        yield conn


class Hub:
    """The LISTEN connection, and the /ws/strategies subscribers it fans out to."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self.connected = False
        self._queues: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if url := db.database_url():
            self._task = asyncio.create_task(
                _reconnecting("Strategies LISTEN", url, _listen_connection, self._consume, self._dropped),
                name="strategies-listen",
            )

    async def close(self) -> None:
        await _stop(self._task)
        self.publish(None)   # tells every /ws/strategies handler to close

    @contextlib.contextmanager
    def subscribe(self) -> Iterator[asyncio.Queue]:
        queue: asyncio.Queue = asyncio.Queue(QUEUE_SIZE)
        self._queues.add(queue)
        try:
            yield queue
        finally:
            self._queues.discard(queue)

    def publish(self, message: dict | None) -> None:
        for queue in self._queues:
            _offer(queue, message)

    def _dropped(self) -> None:
        self.connected = False

    async def _consume(self, conn: psycopg.AsyncConnection) -> None:
        for channel in (UPDATE_CHANNEL, BOT_CHANNEL):
            await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
        self.connected = True
        self.bot.submit()   # listening now: catch up on what changed while not
        async for note in conn.notifies():
            try:
                payload = json.loads(note.payload)
                if note.channel == BOT_CHANNEL:
                    self.bot.submit(payload)
                elif self._queues and (pool := db.pool()) is not None:
                    await self._push_leaderboard(pool, [str(i) for i in payload["instance_ids"]])
            except Exception:
                logger.exception("NOTIFY %s not handled", note.channel)

    async def _push_leaderboard(self, pool: AsyncConnectionPool, ids: list[str]) -> None:
        async with pool.connection() as conn:
            found = await leaderboard_rows(conn, ids=ids, limit=len(ids) * len(SPANS))
        frames: dict[str, list[dict]] = {}
        for row in found:
            frames.setdefault(row.pop("period"), []).append(row)
        for window, changed in frames.items():
            self.publish({"type": "leaderboard", "window": window, "rows": changed})


_hub: Hub | None = None


def get_hub() -> Hub:
    if _hub is None:
        raise RuntimeError("the strategies hub is not running; it starts with the app's lifespan")
    return _hub


def get_bot() -> Bot:
    return get_hub().bot


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Nested inside the app's lifespan: the pool and the TradingService are already up.
    global _hub
    bot = Bot()
    _hub = Hub(bot)
    bot.publish = _hub.publish
    _hub.start()
    bot.start()   # the risk monitor: idle until a reconcile or status read gives it a holding to watch
    try:
        yield
    finally:
        await _hub.close()
        await bot.close()
        _hub = None


async def _pool() -> AsyncConnectionPool:
    if (pool := db.pool()) is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database offline")
    return pool


Pool = Annotated[AsyncConnectionPool, Depends(_pool)]

router = APIRouter(prefix="/api", tags=["strategies"], dependencies=[Depends(_pool)], lifespan=lifespan)


@router.get("/strategies")
async def catalog() -> list[dict]:
    return [{"key": s.key, "name": s.name, "family": s.family, "description": s.description,
             "params": s.params, "intervals": list(s.intervals)} for s in CATALOG.values()]


@router.get("/strategies/leaderboard")
async def leaderboard(
    pool: Pool,
    window: Window = "30d",
    symbol: Annotated[str | None, Query(max_length=20)] = None,
    interval: Annotated[str | None, Query(max_length=4)] = None,
    sort: Sort = "sharpe",
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
) -> list[dict]:
    async with pool.connection() as conn:
        found = await leaderboard_rows(conn, window=window, symbol=symbol.upper() if symbol else None,
                                       interval=interval or None, sort=sort, limit=limit)
    for row in found:
        del row["period"]
    return found


@router.get("/strategies/instances/{instance_id}")
async def instance_detail(instance_id: InstanceId, pool: Pool) -> dict:
    async with pool.connection() as conn:
        found = await rows(conn, """
            SELECT i.id AS instance_id, i.strategy_key, i.symbol, i.interval, i.params, i.created_at,
                   i.backtested_through, i.live_since, s.exposure, s.equity, s.cash, s.qty, s.last_bar
            FROM strategy_instances i LEFT JOIN instance_state s ON s.instance_id = i.id
            WHERE i.id = %s""", (instance_id,))
        if not found:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown strategy instance")
        metrics = await rows(conn, """
            SELECT period, return_pct, sharpe, sortino, max_drawdown_pct, win_rate, exposure_pct, trades, updated_at
            FROM strategy_metrics WHERE instance_id = %s""", (instance_id,))
        fills = await rows(conn, """
            SELECT id, ts, side, price, qty, quote, fee, exposure_after, source
            FROM paper_fills WHERE instance_id = %s ORDER BY ts DESC LIMIT 50""", (instance_id,))
    detail = jsonable(_named(found[0]))
    strategy = CATALOG.get(detail["strategy_key"])
    detail["description"] = strategy.description if strategy else ""
    detail["metrics"] = {m.pop("period"): jsonable(m) for m in metrics}
    detail["recent_fills"] = [jsonable(f) for f in fills]
    return detail


@router.get("/strategies/equity")
async def equity(
    pool: Pool,
    ids: Annotated[str, Query(max_length=4000)],
    window: Window = "30d",
    points: Annotated[int, Query(ge=2, le=5000)] = 300,
) -> dict[str, list[dict]]:
    wanted = list(dict.fromkeys(i for i in ids.split(",") if i))[:MAX_EQUITY_IDS]
    curves: dict[str, list[dict]] = {i: [] for i in wanted}
    async with pool.connection() as conn:
        for r in await rows(conn, EQUITY, {"ids": wanted, "span": SPANS[window], "points": points}):
            curves[r["instance_id"]].append({"t": r["t"], "v": r["v"]})
    return curves


# --- risk-exit stop analysis ---


@router.get("/strategies/instances/{instance_id}/stops")
async def instance_stops(instance_id: InstanceId, pool: Pool) -> dict:
    """The instance's backtest under each risk-exit preset; ``best`` beats ``none`` on Sharpe over 1y (else 90d)."""
    async with pool.connection() as conn:
        if not await rows(conn, "SELECT 1 FROM strategy_instances WHERE id = %s", (instance_id,)):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown strategy instance")
        found = await rows(conn, """
            SELECT preset, period, return_pct, sharpe, max_drawdown_pct, trades, win_rate
            FROM stop_analysis WHERE instance_id = %s""", (instance_id,))
    order = list(PRESETS)
    found = sorted((r for r in found if r["preset"] in PRESETS),
                   key=lambda r: (r["period"] != "1y", order.index(r["preset"])))
    best = None
    for period in ("1y", "90d"):
        sharpe = {r["preset"]: r["sharpe"] for r in found if r["period"] == period}
        if sharpe:
            floor = sharpe.pop("none", None)
            beating = {p: s for p, s in sharpe.items() if s is not None and (floor is None or s > floor)}
            best = max(beating, key=beating.get) if beating else None
            break
    return {"rows": [{**jsonable(r), "label": PRESETS[r["preset"]][0]} for r in found],
            "best": best}


# --- the bot ---

RiskPct = Annotated[float | None, Field(gt=0, le=50, allow_inf_nan=False)]
Execution = Literal["market", "maker_first"]


class Risk(BaseModel):
    """Risk exits in percent of the entry (5.0 = 5 %); null turns one off."""
    stop_loss_pct: RiskPct = None
    take_profit_pct: RiskPct = None
    trailing_pct: RiskPct = None


class Activation(Risk):
    instance_id: str = Field(max_length=64)
    size_usdt: float = Field(gt=0, allow_inf_nan=False)
    execution: Execution = "market"


class RiskUpdate(Risk):
    execution: Execution | None = None   # null keeps the bot's execution


class Deactivation(BaseModel):
    close_position: bool = False


@router.get("/bot")
async def bot_status() -> dict:
    return await get_bot().status()


@router.post("/bot/activate")
async def activate_bot(body: Activation) -> dict:
    try:
        return await get_bot().activate(body.instance_id, body.size_usdt, stop_loss_pct=body.stop_loss_pct,
                                        take_profit_pct=body.take_profit_pct, trailing_pct=body.trailing_pct,
                                        execution=body.execution)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown strategy instance") from None


@router.post("/bot/risk")
async def set_bot_risk(body: RiskUpdate) -> dict:
    """Replace the active bot's risk exits (all three; null turns one off), and its execution if given."""
    try:
        return await get_bot().set_risk(body.stop_loss_pct, body.take_profit_pct, body.trailing_pct, body.execution)
    except LookupError:
        raise HTTPException(status.HTTP_409_CONFLICT, "no active bot") from None


@router.post("/bot/deactivate")
async def deactivate_bot(body: Deactivation) -> dict:
    return await get_bot().deactivate(body.close_position)


@router.post("/bot/sync")
async def sync_bot() -> dict:
    """Retry bringing the bot to its instance's target, e.g. after a rejected order."""
    return await get_bot().reconcile()


@router.get("/bot/preview")
async def preview_bot(instance_id: Annotated[str, Query(max_length=64)]) -> dict:
    try:
        return await get_bot().preview(instance_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown strategy instance") from None


# --- /ws/strategies ---

ws_router = APIRouter(tags=["strategies"])


@ws_router.websocket("/ws/strategies")
async def strategies_stream(websocket: WebSocket) -> None:
    """First message ``{"token"}``; then the bot's status, and every leaderboard and bot frame."""
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
    hub = get_hub()
    with hub.subscribe() as queue:
        if db.pool() is not None:
            try:
                _offer(queue, {"type": "bot", **await hub.bot.status()})
            except Exception as exc:
                logger.warning("Bot status not sent to a new /ws/strategies client: %s", db.describe(exc))
        async with anyio.create_task_group() as group:
            group.start_soon(_forward, queue, websocket, group)
            while (await websocket.receive())["type"] != "websocket.disconnect":
                pass
            group.cancel_scope.cancel()
