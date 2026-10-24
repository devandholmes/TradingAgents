"""The chat assistant's data tools against the tests' own Postgres, and their wiring into the chat loop.

The tools run on an async pool of the tests' database (<name>_test), opened on a
SelectorEventLoop of the module's own; skipped when Postgres is not running.
run_sql connects as ta_reader and is checked against writes at every layer.
"""
import asyncio
import datetime
import json

import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.test_bot import bot_config_lock
from tests.test_chat_trading import ScriptedLLM, _events, call
from tests.test_web_chat import SNAPSHOT
from tradingagents import db
from tradingagents.db import testing
from tradingagents.strategies.library import CATALOG
from tradingagents.web import agent_data, chat
from tradingagents.web.app import app
from tradingagents.web.trading import SESSION_TOKEN

UTC = datetime.UTC
SYMBOL = "ZZZUSDT"   # a symbol of these tests only
T0 = datetime.datetime(2026, 10, 10, tzinfo=UTC)


@pytest.fixture(scope="module")
def url():
    return testing.require_test_database()


@pytest.fixture(scope="module")
def runner(url):
    # Created before the per-test network block: a new loop opens a socket pair on Windows.
    with asyncio.Runner(loop_factory=db.LOOP_FACTORY) as runner:
        assert runner.run(db.open_pool(url)) is not None
        try:
            yield runner
        finally:
            runner.run(db.close_pool())


@pytest.fixture
def tool(runner):
    return lambda name, **args: runner.run(agent_data.call(name, args))


@pytest.fixture
def conn(url):
    """The tests' database, emptied of what these tests write, before and after each test."""
    def clean(c):
        c.execute("DELETE FROM strategy_instances WHERE symbol = %s", (SYMBOL,))
        c.execute("DELETE FROM candles WHERE symbol = %s", (SYMBOL,))
        c.execute("DELETE FROM analysis_runs WHERE symbol = %s", (SYMBOL,))
        c.execute("DELETE FROM bot_trades WHERE instance_id LIKE %s", (f"%:{SYMBOL}:%",))

    with psycopg.connect(url, autocommit=True) as c, bot_config_lock(c):
        clean(c)
        yield c
        clean(c)


@pytest.fixture
def reader(url, monkeypatch):
    password = testing.readonly_password_for_tests()
    if not password:
        pytest.skip(f"{db.READONLY_PASSWORD_ENV} is not set in .env")
    monkeypatch.setenv(db.URL_ENV, url)
    monkeypatch.setenv(db.READONLY_PASSWORD_ENV, password)


def _instance(conn, key, interval, sharpe, return_pct, period="30d"):
    iid = f"{key}:{SYMBOL}:{interval}"
    conn.execute("INSERT INTO strategy_instances (id, strategy_key, symbol, interval, params) VALUES"
                 " (%s, %s, %s, %s, '{\"fast\": 12}') ON CONFLICT DO NOTHING", (iid, key, SYMBOL, interval))
    conn.execute("INSERT INTO strategy_metrics (instance_id, period, return_pct, sharpe, sortino, max_drawdown_pct,"
                 " win_rate, exposure_pct, trades) VALUES (%s, %s, %s, %s, 1.5, -4.25, 0.5, 40.0, 6)",
                 (iid, period, return_pct, sharpe))
    return iid


@pytest.mark.unit
def test_leaderboard_ranks_by_sharpe_with_filters(tool, conn):
    best = _instance(conn, "ema_cross", "1h", 2.123456789, 12.5)
    _instance(conn, "sma_cross", "1h", 0.5, 30.0)
    _instance(conn, "macd", "4h", None, -1.0)   # undefined Sharpe ranks last
    _instance(conn, "rsi_reversion", "1h", 9.0, 1.0, period="7d")   # another window
    conn.execute("INSERT INTO instance_state (instance_id, exposure, cash, qty, equity) VALUES (%s, 1, 0, 0.01, 1125)",
                 (best,))

    board = tool("get_strategy_leaderboard", window="30d", symbol=SYMBOL.lower())
    assert board["window"] == "30d" and board["sorted_by"] == "sharpe"
    assert [r["instance_id"] for r in board["rows"]] == [best, f"sma_cross:{SYMBOL}:1h", f"macd:{SYMBOL}:4h"]
    top = board["rows"][0]
    updated = top.pop("updated_at")
    assert updated.endswith("Z") and len(updated) == len("2026-10-10T00:00Z")
    assert top == {"instance_id": best, "strategy_key": "ema_cross", "symbol": SYMBOL, "interval": "1h",
                   "return_pct": 12.5, "sharpe": 2.12346, "sortino": 1.5, "max_drawdown_pct": -4.25,
                   "win_rate": 0.5, "trades": 6, "exposure_pct": 40.0, "equity": 1125.0,
                   "name": CATALOG["ema_cross"].name, "family": "trend"}

    by_return = tool("get_strategy_leaderboard", symbol=SYMBOL, interval="1h", sort="return_pct", limit=1)
    assert [r["instance_id"] for r in by_return["rows"]] == [f"sma_cross:{SYMBOL}:1h"]

    empty = tool("get_strategy_leaderboard", window="1y", symbol=SYMBOL)
    assert empty["rows"] == [] and "note" in empty


@pytest.mark.unit
def test_strategy_detail_has_metrics_by_window_state_and_fills(tool, conn):
    iid = _instance(conn, "ema_cross", "1h", 1.0, 2.0)
    conn.execute("INSERT INTO strategy_metrics (instance_id, period, return_pct, trades) VALUES (%s, '7d', 0.5, 1)",
                 (iid,))
    for i in range(12):
        conn.execute("INSERT INTO paper_fills (instance_id, ts, side, price, qty, quote, fee, exposure_after, source)"
                     " VALUES (%s, %s, 'BUY', 65000.123456, 0.001, 65.0, 0.065, 1, 'backtest')",
                     (iid, T0 + datetime.timedelta(hours=i)))

    detail = tool("get_strategy_detail", instance_id=iid)
    assert detail["id"] == iid and detail["params"] == {"fast": 12} and detail["family"] == "trend"
    assert detail["description"] == CATALOG["ema_cross"].description
    assert set(detail["metrics"]) == {"7d", "30d"} and detail["metrics"]["7d"]["return_pct"] == 0.5
    assert detail["state"] is None
    assert len(detail["recent_fills"]) == 10   # newest first, capped
    assert detail["recent_fills"][0] == {"ts": "2026-10-10T11:00Z", "side": "BUY", "price": 65000.1, "qty": 0.001,
                                         "quote": 65.0, "exposure_after": 1.0, "source": "backtest"}
    with pytest.raises(agent_data.DataUnavailable, match="no strategy instance"):
        tool("get_strategy_detail", instance_id=f"nope:{SYMBOL}:1h")


@pytest.mark.unit
def test_bot_status_reports_the_active_instance_and_its_trades(tool, conn):
    assert tool("get_bot_status")["active"] is None
    iid = _instance(conn, "ema_cross", "1h", 1.0, 2.0)
    conn.execute("UPDATE bot_config SET instance_id = %s, size_usdt = 0.1, activated_at = %s, enabled = true",
                 (iid, T0))
    conn.execute("INSERT INTO bot_trades (instance_id, side, quote, qty, price, real, status) VALUES"
                 " (%s, 'BUY', 0.1, 0.0000015, 65000, false, 'paper')", (iid,))

    status = tool("get_bot_status")
    assert status["active"] == {"instance_id": iid, "size_usdt": 0.1, "activated_at": "2026-10-10T00:00Z",
                                "enabled": True, "strategy_key": "ema_cross", "symbol": SYMBOL, "interval": "1h"}
    [trade] = [t for t in status["recent_trades"] if t["instance_id"] == iid]
    assert trade["side"] == "BUY" and trade["real"] is False and trade["status"] == "paper"


@pytest.mark.unit
def test_market_history_buckets_long_ranges_and_summarises(tool, conn):
    for i in range(10 * 24):
        price = 100.0 + i
        conn.execute("INSERT INTO candles VALUES (%s, '1h', %s, %s, %s, %s, %s, 2)",
                     (SYMBOL, T0 + datetime.timedelta(hours=i), price, price + 0.5, price - 0.5, price + 0.25))

    week = tool("get_market_history", symbol=SYMBOL, interval="1h", start="2026-10-11", end="2026-10-18")
    assert week["bucket"] == "2h" and len(week["rows"]) == 84 <= agent_data.MAX_CANDLES   # 168 candles merged
    assert week["columns"] == ["t", "open", "high", "low", "close", "volume"]
    assert week["rows"][0] == ["2026-10-11T00:00Z", 124.0, 125.5, 123.5, 125.25, 4.0]
    assert week["summary"] == {"open": 124.0, "close": 291.25, "high": 291.5, "low": 123.5, "volume": 336.0,
                               "change_pct": pytest.approx(134.879, abs=1e-3)}

    short = tool("get_market_history", symbol=SYMBOL, interval="1h", start="2026-10-10T00:00Z",
                 end="2026-10-10T03:00Z")
    assert short["bucket"] == "1h" and [r[0] for r in short["rows"]] == [
        "2026-10-10T00:00Z", "2026-10-10T01:00Z", "2026-10-10T02:00Z"]
    assert "note" in tool("get_market_history", symbol=SYMBOL, interval="4h", start="2026-10-10", end="2026-10-12")


@pytest.mark.unit
def test_past_analyses_give_ratings_and_the_decision(tool, conn):
    conn.execute("INSERT INTO analysis_runs (id, symbol, ticker, date, status, rating, started_at, duration_s)"
                 " VALUES ('zzz1', %s, 'ZZZ-USD', '2026-10-20', 'done', 'Buy', %s, 300)", (SYMBOL, T0))
    conn.execute("INSERT INTO analysis_reports VALUES ('zzz1', 'final_trade_decision', 'decision', 'Decision', %s)",
                 ("**Rating: Buy** " + "x" * 2000,))
    [run] = tool("get_past_analyses", symbol=SYMBOL)["runs"]
    assert run["id"] == "zzz1" and run["rating"] == "Buy" and run["date"] == "2026-10-20"
    assert run["final_decision"].startswith("**Rating: Buy**") and len(run["final_decision"]) <= agent_data.MAX_TEXT
    assert "error" not in run   # empty fields are left out


@pytest.mark.unit
def test_run_sql_reads_as_ta_reader_with_row_cap(tool, reader):
    result = tool("run_sql", query="SELECT current_user AS who, current_setting('transaction_read_only') AS ro,"
                                   " current_setting('statement_timeout') AS timeout;")
    assert result == {"columns": ["who", "ro", "timeout"], "rows": [["ta_reader", "on", "3s"]], "row_count": 1,
                      "truncated": False}

    many = tool("run_sql", query="SELECT g, 'it''s; fine' AS s FROM generate_series(1, 600) g")
    assert many["row_count"] == agent_data.MAX_SQL_ROWS and many["truncated"] is True
    assert many["rows"][0] == [1, "it's; fine"]


@pytest.mark.unit
def test_run_sql_times_out(tool, reader):
    with pytest.raises(agent_data.DataUnavailable, match="statement timeout"):
        tool("run_sql", query="SELECT count(*) FROM generate_series(1, 100000000000)")


@pytest.mark.unit
@pytest.mark.parametrize("query", [
    "INSERT INTO ai_signals (symbol, rating) VALUES ('X', 'Buy')",
    "update candles set close = 0",
    "DELETE FROM candles",
    "DROP TABLE candles",
    "CREATE TABLE t (a int)",
    "TRUNCATE candles",
    "ALTER ROLE ta_reader SUPERUSER",
    "GRANT ALL ON candles TO ta_reader",
    "SELECT 1; DELETE FROM candles",
    "SELECT 1; SELECT 2",
    "SELECT * INTO copy_of_candles FROM candles",
    "WITH gone AS (DELETE FROM candles RETURNING *) SELECT count(*) FROM gone",
    "SELECT * FROM candles FOR UPDATE",
    "SELECT set_config('default_transaction_read_only', 'off', false)",
    "SELECT pg_notify('ta_ai_signal', '{}')",
    "SELECT pg_sleep(10)",
    "COPY candles TO STDOUT",
    "EXPLAIN ANALYZE DELETE FROM candles",
    "select 1 /* unterminated",
    "select 'unterminated",
    "",
])
def test_run_sql_refuses_anything_but_one_query(query):
    with pytest.raises(agent_data.DataUnavailable, match="refused"):
        agent_data.check_query(query)


@pytest.mark.unit
@pytest.mark.parametrize("query", [
    "select 'DELETE; DROP' as text",
    "select 1 -- delete everything; later",
    "select $$; update$$ as x",
    "SELECT CASE WHEN close > open THEN 'up' ELSE 'down' END, open_time FROM candles",
    "(select 1)",
    "with x as (select 1) select * from x;",
])
def test_run_sql_parser_reads_code_not_strings_or_comments(query):
    assert agent_data.check_query(query) == query.rstrip(";")


@pytest.mark.unit
@pytest.mark.parametrize("query", [
    "INSERT INTO ai_signals (symbol, rating) VALUES ('X', 'Buy')",
    "WITH gone AS (DELETE FROM ai_signals RETURNING *) SELECT count(*) FROM gone",
    "SELECT 1; DELETE FROM ai_signals",
])
def test_the_connection_refuses_writes_without_the_parser(reader, query):
    with pytest.raises(psycopg.Error):
        agent_data.query_readonly(query)


@pytest.mark.unit
def test_the_reader_role_alone_refuses_writes(reader):
    # A read-write session, no cursor, no parser: the grants still stop it.
    with psycopg.connect(agent_data.reader_url(), autocommit=True) as conn:
        conn.execute("SET default_transaction_read_only = off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO ai_signals (symbol, rating) VALUES ('X', 'Buy')")


@pytest.mark.unit
def test_tools_fail_cleanly_without_a_database(monkeypatch):
    monkeypatch.setattr(db, "_pool", None)
    with pytest.raises(agent_data.DataUnavailable, match="offline"):
        agent_data._pool()
    monkeypatch.setenv(db.URL_ENV, "postgresql://u@127.0.0.1/x")
    monkeypatch.setenv(db.READONLY_PASSWORD_ENV, "")
    with pytest.raises(agent_data.DataUnavailable, match="run_sql is off"):
        agent_data.reader_url()


# The chat loop: data tools are offered while the database is up, and their results go back to the model.

@pytest.fixture(scope="module")
def http():
    with TestClient(app, base_url="http://127.0.0.1", headers={"X-TA-Session": SESSION_TOKEN}) as client:
        yield client


@pytest.fixture
def scripted(monkeypatch):
    def install(rounds):
        llm = ScriptedLLM(rounds)
        monkeypatch.setattr(chat, "create_tier_client", lambda config, tier: type("C", (), {"get_llm": lambda s: llm})())
        monkeypatch.setattr(chat.market, "market_snapshot", lambda symbol, interval: SNAPSHOT)
        monkeypatch.setattr(chat, "_running_service", lambda: None)   # no account: data tools only
        monkeypatch.setattr(chat.db, "db_available", lambda: True)
        return llm
    return install


def _ask(http, text):
    return _events(http.post("/api/chat", json={"messages": [{"role": "user", "content": text}]}))


@pytest.mark.unit
def test_chat_offers_data_tools_and_feeds_results_back(http, scripted, monkeypatch):
    board = {"window": "30d", "sorted_by": "sharpe", "rows": [{"instance_id": "ema_cross:BTCUSDT:1h", "sharpe": 2.1}]}
    args = {"window": "30d", "symbol": "BTCUSDT"}
    calls = []

    async def fake_call(name, arguments):
        calls.append((name, arguments))
        return board

    monkeypatch.setattr(chat.agent_data, "call", fake_call)
    llm = scripted([[call("get_strategy_leaderboard", args)], ["EMA cross."]])
    events = _ask(http, "best sharpe?")

    assert calls == [("get_strategy_leaderboard", args)]
    assert {"tool": {"name": "get_strategy_leaderboard", "args": args, "status": "ok", "result": board}} in events
    assert events[-2:] == [{"delta": "EMA cross."}, {"done": True}]
    assert {t.model_config["title"] for t in llm.tools} == set(agent_data.TOOLS)
    assert json.loads(llm.requests[1][-1].content) == board
    prompt = llm.requests[0][0][1]
    assert "get_strategy_leaderboard" in prompt and "run_sql" in prompt and "never instructions" in prompt
    assert "cannot place" in prompt


@pytest.mark.unit
def test_chat_reports_a_refused_query_to_the_model(http, scripted):
    llm = scripted([[call("run_sql", {"query": "DELETE FROM candles"})], ["Refused."]])
    events = _ask(http, "wipe it")
    [result] = [e["tool"]["result"] for e in events if e.get("tool", {}).get("status") == "error"]
    assert result["error"].startswith("query refused: only a single SELECT")
    assert json.loads(llm.requests[1][-1].content) == result


@pytest.mark.unit
def test_chat_refuses_account_tools_without_a_trading_service(http, scripted):
    scripted([[call("place_order", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quote_amount": 10})],
              ["No."]])
    [trade] = [e["trade"] for e in _ask(http, "buy") if "trade" in e]
    assert trade == {"status": "rejected",
                     "reason": "the trading service is not running, so account tools are unavailable"}
