"""The chat assistant's data tools: strategies, the bot, candles and past analyses, from Postgres.

Each tool reads the web server's shared pool with a small, indexed query and
answers in a compact shape: numbers to 6 significant digits, times to the
minute, long texts cut short. ``run_sql`` is the escape hatch for anything the
others do not cover. It can only read, by four independent locks: a parser
that admits a single SELECT or WITH statement, a server-side cursor (DECLARE
refuses anything but a query, and data-modifying WITH clauses), a READ ONLY
transaction with ``statement_timeout`` 3000 ms, and a connection of its own as
``ta_reader``, a role granted SELECT and nothing else.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
from decimal import Decimal
from typing import Literal

import psycopg
from psycopg import conninfo, sql
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field

from tradingagents import db
from tradingagents.strategies.library import CATALOG
from tradingagents.trading.guard import SYMBOL_PATTERN

Window = Literal["7d", "30d", "90d", "1y", "live"]
Interval = Literal["15m", "1h", "4h", "1d"]
INTERVAL_SECONDS = {"15m": 900, "1h": 3600, "4h": 4 * 3600, "1d": 86400}

MAX_SQL_ROWS = 500
MAX_SQL_CHARS = 20_000   # a run_sql result stops adding rows past this much JSON
MAX_TEXT = 600           # characters of any one text value
MAX_CANDLES = 120        # get_market_history merges candles into at most this many buckets
STATEMENT_TIMEOUT_MS = 3000

# For run_sql's description: the tables and their columns (tradingagents/db/migrations).
SCHEMA = """candles(symbol, interval, open_time, open, high, low, close, volume);
strategy_instances(id, strategy_key, symbol, interval, params jsonb, created_at, backtested_through, live_since);
strategy_metrics(instance_id, period in 7d|30d|90d|1y|live, return_pct, sharpe, sortino, max_drawdown_pct,
win_rate 0..1, exposure_pct 0..100, trades, updated_at);
paper_fills(id, instance_id, ts, side, price, qty, quote, fee, exposure_after, source backtest|live);
equity_points(instance_id, ts, equity, exposure, source); instance_state(instance_id, exposure, cash, qty, equity,
pending_target, last_bar, updated_at); ai_signals(id, symbol, run_id, rating, ts);
bot_config(id=1, instance_id, size_usdt, activated_at, enabled); bot_trades(id, ts, instance_id, side, quote, qty,
price, real, status, order_id, reason); analysis_runs(id, symbol, ticker, date, status, rating, started_at,
finished_at, duration_s, error, analysts); analysis_reports(run_id, section, stage, title, content);
analysis_events(run_id, seq, ts, type, payload jsonb). Times are timestamptz in UTC."""


class DataUnavailable(Exception):
    """A tool that cannot answer: the database is offline, or the query was refused."""


# Tool schemas, as in chat.py: the title is the tool name, the docstring its description.
class GetStrategyLeaderboard(BaseModel):
    """Paper-traded strategy instances (strategy x symbol x interval, 1000 USDT reference capital each),
    ranked best first by one metric over a window of their backtest and live results."""

    model_config = ConfigDict(title="get_strategy_leaderboard")
    window: Window = "30d"
    symbol: str | None = Field(default=None, pattern=SYMBOL_PATTERN, description="e.g. BTCUSDT; omit for all")
    interval: Interval | None = Field(default=None, description="omit for all")
    sort: Literal["sharpe", "return_pct", "sortino", "max_drawdown_pct", "win_rate"] = "sharpe"
    limit: int = Field(default=10, ge=1, le=50)


class GetStrategyDetail(BaseModel):
    """One strategy instance: description, params, metrics per window, current state, recent fills."""

    model_config = ConfigDict(title="get_strategy_detail")
    instance_id: str = Field(max_length=80, description="<strategy_key>:<SYMBOL>:<interval>, e.g. ema_cross:BTCUSDT:1h")


class GetBotStatus(BaseModel):
    """The strategy bot: the instance it follows, the user's size, whether it is on, its recent trades."""

    model_config = ConfigDict(title="get_bot_status")


class GetMarketHistory(BaseModel):
    """Stored Binance candles of a symbol between two times (default: the last 7 days), with a summary;
    long ranges are merged into at most 120 buckets."""

    model_config = ConfigDict(title="get_market_history")
    symbol: str = Field(pattern=SYMBOL_PATTERN, description="e.g. BTCUSDT")
    interval: Interval = "1h"
    start: datetime.datetime | None = Field(default=None, description="ISO date or time, UTC")
    end: datetime.datetime | None = Field(default=None, description="ISO date or time, UTC; default now")


class GetPastAnalyses(BaseModel):
    """Earlier TradingAgents analysis runs, newest first: rating and the start of the final decision."""

    model_config = ConfigDict(title="get_past_analyses")
    symbol: str | None = Field(default=None, pattern=SYMBOL_PATTERN, description="e.g. BTCUSDT; omit for all")
    limit: int = Field(default=5, ge=1, le=20)


class RunSql(BaseModel):
    # The description is built, so it goes in the schema rather than a docstring.
    model_config = ConfigDict(title="run_sql", json_schema_extra={"description": (
        "One read-only SQL query (a single SELECT or WITH) on the dashboard's Postgres, for what the other "
        f"tools do not cover; at most {MAX_SQL_ROWS} rows, 3 s. Tables: {SCHEMA}")})
    query: str = Field(min_length=1, max_length=4000)

TOOLS = {schema.model_config["title"]: schema for schema in (
    GetStrategyLeaderboard, GetStrategyDetail, GetBotStatus, GetMarketHistory, GetPastAnalyses, RunSql,
)}


def _value(value):
    """A value as the model gets it: short numbers, minute times, cut texts."""
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float | Decimal):
        return float(f"{value:.6g}")
    if isinstance(value, datetime.datetime):
        return value.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%MZ")
    if isinstance(value, dict | list):
        text = json.dumps(value, default=str)
        return value if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…"
    text = str(value)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…"


def _row(row: dict) -> dict:
    return {k: _value(v) for k, v in row.items()}


async def _fetch(conn, query, params=None) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


def _pool():
    if (pool := db.pool()) is None:
        raise DataUnavailable("the database is offline")
    return pool


_LEADERBOARD = """
SELECT i.id AS instance_id, i.strategy_key, i.symbol, i.interval, m.return_pct, m.sharpe, m.sortino,
       m.max_drawdown_pct, m.win_rate, m.trades, m.exposure_pct, s.equity, m.updated_at
FROM strategy_metrics m
JOIN strategy_instances i ON i.id = m.instance_id
LEFT JOIN instance_state s ON s.instance_id = m.instance_id
WHERE m.period = %(window)s
  AND (%(symbol)s::text IS NULL OR i.symbol = %(symbol)s)
  AND (%(interval)s::text IS NULL OR i.interval = %(interval)s)
ORDER BY {sort} DESC NULLS LAST, i.id
LIMIT %(limit)s
"""


async def strategy_leaderboard(req: GetStrategyLeaderboard) -> dict:
    params = {"window": req.window, "symbol": req.symbol and req.symbol.upper(), "interval": req.interval,
              "limit": req.limit}
    query = sql.SQL(_LEADERBOARD).format(sort=sql.Identifier("m", req.sort))
    async with _pool().connection() as conn:
        rows = await _fetch(conn, query, params)
    for row in rows:
        strategy = CATALOG.get(row["strategy_key"])
        row["name"], row["family"] = (strategy.name, strategy.family) if strategy else (row["strategy_key"], None)
    result = {"window": req.window, "sorted_by": req.sort, "rows": [_row(r) for r in rows]}
    if not rows:
        result["note"] = "no metrics for this window and filter yet: the strategy engine has not scored any instance"
    return result


async def strategy_detail(req: GetStrategyDetail) -> dict:
    async with _pool().connection() as conn:
        found = await _fetch(conn, "SELECT id, strategy_key, symbol, interval, params, created_at,"
                                   " backtested_through, live_since FROM strategy_instances WHERE id = %s",
                             (req.instance_id,))
        if not found:
            raise DataUnavailable(f"no strategy instance {req.instance_id}")
        metrics = await _fetch(conn, "SELECT period, return_pct, sharpe, sortino, max_drawdown_pct, win_rate,"
                                     " exposure_pct, trades FROM strategy_metrics WHERE instance_id = %s",
                               (req.instance_id,))
        state = await _fetch(conn, "SELECT exposure, cash, qty, equity, pending_target, last_bar"
                                   " FROM instance_state WHERE instance_id = %s", (req.instance_id,))
        fills = await _fetch(conn, "SELECT ts, side, price, qty, quote, exposure_after, source FROM paper_fills"
                                   " WHERE instance_id = %s ORDER BY ts DESC LIMIT 10", (req.instance_id,))
    instance = _row(found[0])
    if strategy := CATALOG.get(instance["strategy_key"]):
        instance |= {"name": strategy.name, "family": strategy.family, "description": strategy.description}
    return {**instance, "metrics": {m.pop("period"): _row(m) for m in metrics},
            "state": _row(state[0]) if state else None, "recent_fills": [_row(f) for f in fills]}


async def bot_status(req: GetBotStatus) -> dict:
    async with _pool().connection() as conn:
        config = await _fetch(conn, "SELECT c.instance_id, c.size_usdt, c.activated_at, c.enabled, i.strategy_key,"
                                    " i.symbol, i.interval FROM bot_config c"
                                    " LEFT JOIN strategy_instances i ON i.id = c.instance_id WHERE c.id = 1")
        trades = await _fetch(conn, "SELECT id, ts, instance_id, side, quote, qty, price, real, status, reason"
                                    " FROM bot_trades ORDER BY id DESC LIMIT 10")
    active = config[0] if config and config[0]["enabled"] and config[0]["instance_id"] else None
    return {"active": _row(active) if active else None, "recent_trades": [_row(t) for t in trades]}


def _span(seconds: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _utc(t: datetime.datetime) -> datetime.datetime:
    return t if t.tzinfo else t.replace(tzinfo=datetime.UTC)


_HISTORY = """
SELECT date_bin(%(stride)s, open_time, 'epoch'::timestamptz) AS t,
       (array_agg(open ORDER BY open_time))[1] AS open, max(high) AS high, min(low) AS low,
       (array_agg(close ORDER BY open_time DESC))[1] AS close, sum(volume) AS volume
FROM candles
WHERE symbol = %(symbol)s AND interval = %(interval)s AND open_time >= %(start)s AND open_time < %(end)s
GROUP BY t ORDER BY t
"""


async def market_history(req: GetMarketHistory) -> dict:
    end = _utc(req.end) if req.end else datetime.datetime.now(datetime.UTC)
    start = _utc(req.start) if req.start else end - datetime.timedelta(days=7)
    if start >= end:
        raise DataUnavailable("start must be before end")
    step = INTERVAL_SECONDS[req.interval]
    stride = step * max(1, -(-int((end - start).total_seconds() // step) // MAX_CANDLES))   # ceiling division
    # Buckets on candle boundaries, as Binance's are (from the epoch): the first one starts at or before start.
    start = datetime.datetime.fromtimestamp(start.timestamp() // stride * stride, datetime.UTC)
    symbol = req.symbol.upper()
    params = {"stride": datetime.timedelta(seconds=stride), "symbol": symbol, "interval": req.interval,
              "start": start, "end": end}
    async with _pool().connection() as conn:
        buckets = await _fetch(conn, _HISTORY, params)
    result = {"symbol": symbol, "interval": req.interval, "start": _value(start), "end": _value(end),
              "bucket": _span(stride)}
    if not buckets:
        return result | {"note": "no stored candles in this range"}
    summary = {"open": buckets[0]["open"], "close": buckets[-1]["close"],
               "high": max(b["high"] for b in buckets), "low": min(b["low"] for b in buckets),
               "volume": sum(b["volume"] for b in buckets)}
    summary["change_pct"] = (summary["close"] / summary["open"] - 1) * 100
    columns = ["t", "open", "high", "low", "close", "volume"]
    return result | {"summary": _row(summary), "columns": columns,
                     "rows": [[_value(b[c]) for c in columns] for b in buckets]}


async def past_analyses(req: GetPastAnalyses) -> dict:
    async with _pool().connection() as conn:
        rows = await _fetch(conn, (
            "SELECT r.id, r.symbol, r.date, r.status, r.rating, r.started_at, r.duration_s, r.error,"
            " left(rep.content, %(text)s) AS final_decision FROM analysis_runs r"
            " LEFT JOIN analysis_reports rep ON rep.run_id = r.id AND rep.section = 'final_trade_decision'"
            " WHERE (%(symbol)s::text IS NULL OR r.symbol = %(symbol)s) ORDER BY r.started_at DESC LIMIT %(limit)s"
        ), {"symbol": req.symbol and req.symbol.upper(), "limit": req.limit, "text": MAX_TEXT})
    return {"runs": [_row({k: v for k, v in r.items() if v is not None}) for r in rows]}


# run_sql's parser reads code only: comments, string literals and quoted identifiers are blanked first.
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|create|alter|drop|truncate|grant|revoke|copy|call|do|set|reset|lock"
    r"|vacuum|analyze|refresh|cluster|reindex|comment|listen|unlisten|notify|prepare|execute|deallocate"
    r"|discard|begin|commit|abort|rollback|savepoint|release|into|import|load|security"
    r"|pg_notify|set_config|pg_sleep\w*|pg_terminate_backend|pg_cancel_backend|pg_\w*advisory\w*|lo_\w+"
    r"|dblink\w*|pg_read_\w+|pg_ls_\w+|pg_stat_file|pg_reload_conf|pg_rotate_logfile|pg_switch_wal)\b"
)


def _skip_quoted(query: str, i: int) -> int:
    """The index after the string literal or quoted identifier starting at ``i``."""
    quote, n = query[i], len(query)
    escapes = quote == "'" and i > 0 and query[i - 1] in "eE"   # E'...' takes backslash escapes
    i += 1
    while i < n:
        if escapes and query[i] == "\\":
            i += 2
        elif query[i] == quote:
            if not query.startswith(quote * 2, i):
                return i + 1
            i += 2
        else:
            i += 1
    raise DataUnavailable("query refused: unterminated quote")


def _skip_block_comment(query: str, i: int) -> int:
    depth, i = 1, i + 2   # Postgres block comments nest
    while depth and i < len(query):
        if query.startswith("/*", i):
            depth, i = depth + 1, i + 2
        elif query.startswith("*/", i):
            depth, i = depth - 1, i + 2
        else:
            i += 1
    if depth:
        raise DataUnavailable("query refused: unterminated comment")
    return i


def _code_only(query: str) -> str:
    """The query with every comment, string literal and quoted identifier replaced by a space."""
    out, i, n = [], 0, len(query)
    while i < n:
        char = query[i]
        tag = _DOLLAR_TAG.match(query, i) if char == "$" and not (i and (query[i - 1].isalnum()
                                                                        or query[i - 1] == "_")) else None
        if query.startswith("--", i):
            end = query.find("\n", i)
            i = n if end < 0 else end
        elif query.startswith("/*", i):
            i = _skip_block_comment(query, i)
        elif char in "'\"":
            i = _skip_quoted(query, i)
        elif tag:
            end = query.find(tag.group(0), tag.end())
            if end < 0:
                raise DataUnavailable("query refused: unterminated dollar quote")
            i = end + len(tag.group(0))
        else:
            out.append(char)
            i += 1
            continue
        out.append(" ")
    return "".join(out)


def check_query(query: str) -> str:
    """The query, trailing semicolons cut, if it is one SELECT or WITH statement; else DataUnavailable."""
    code = _code_only(query).lower()
    if ";" in code.rstrip().rstrip(";"):
        raise DataUnavailable("query refused: one statement only")
    first = re.match(r"[\s(]*([a-z]+)", code)
    if not first or first.group(1) not in ("select", "with"):
        raise DataUnavailable("query refused: only a single SELECT or WITH query may run")
    if word := _FORBIDDEN.search(code):
        raise DataUnavailable(f"query refused: {word.group(1).upper()} is not allowed in a read-only query")
    query = query.rstrip()
    while query.endswith(";"):   # only code semicolons can end the text: a string or comment would close it
        query = query[:-1].rstrip()
    return query


def reader_url() -> str:
    url, password = db.database_url(), os.environ.get(db.READONLY_PASSWORD_ENV)
    if not url:
        raise DataUnavailable("the database is not configured")
    if not password:
        raise DataUnavailable(f"run_sql is off: {db.READONLY_PASSWORD_ENV} is not set")
    return conninfo.make_conninfo(url, user=db.READER_ROLE, password=password)


def query_readonly(query: str, max_rows: int = MAX_SQL_ROWS) -> tuple[list[str], list[tuple], bool]:
    """``(columns, rows, truncated)`` of a query run as ta_reader in a READ ONLY transaction. Blocking.

    No parsing here: the role, the transaction and the cursor refuse writes on their own.
    """
    with psycopg.connect(reader_url(), **db.SESSION) as conn:
        conn.read_only = True   # every transaction: BEGIN READ ONLY
        try:
            conn.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
            with conn.cursor(name="run_sql") as cur:   # DECLARE run_sql CURSOR FOR <query>
                cur.execute(query)
                rows = cur.fetchmany(max_rows + 1)
                columns = [c.name for c in cur.description or ()]
        finally:
            conn.rollback()
    return columns, rows[:max_rows], len(rows) > max_rows


async def run_sql(req: RunSql) -> dict:
    query = check_query(req.query)
    try:
        # A connection of its own, opened and read in a thread: never the shared pool's owner role.
        columns, rows, truncated = await asyncio.to_thread(query_readonly, query)
    except psycopg.Error as exc:
        raise DataUnavailable(db.describe(exc)) from None
    out, size = [], 0
    for row in rows:
        values = [_value(v) for v in row]
        size += len(json.dumps(values, default=str))
        if size > MAX_SQL_CHARS:
            truncated = True
            break
        out.append(values)
    return {"columns": columns, "rows": out, "row_count": len(out), "truncated": truncated}


HANDLERS = {
    "get_strategy_leaderboard": strategy_leaderboard,
    "get_strategy_detail": strategy_detail,
    "get_bot_status": bot_status,
    "get_market_history": market_history,
    "get_past_analyses": past_analyses,
    "run_sql": run_sql,
}


async def call(name: str, args: dict):
    """Run one data tool; raises ValidationError or DataUnavailable (and psycopg errors of the pool)."""
    return await HANDLERS[name](TOOLS[name].model_validate(args))
