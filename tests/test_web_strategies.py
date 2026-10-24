"""Strategy Lab routes and /ws/strategies against the tests' own Postgres, with the app's real lifespan.

Leaderboard filters and sorting, normalised and downsampled equity, instance detail,
the bot routes' validation and token, the WebSocket push on NOTIFY (leaderboard and
bot), and 503 while the database is offline. The TradingService is on a fake Binance;
the engine and the AI scheduler are off. Skipped when Postgres is not running.
"""
import json
import time

import psycopg
import pytest
from starlette.websockets import WebSocketDisconnect

from tests.test_bot import bot_config_lock
from tests.test_trading_guard import make_service
from tradingagents import db
from tradingagents.db import testing
from tradingagents.trading import service as service_module
from tradingagents.web import strategies
from tradingagents.web.app import app
from tradingagents.web.trading import SESSION_TOKEN

TOKEN = {"X-TA-Session": SESSION_TOKEN}
WS_URL = "ws://127.0.0.1:8000/ws/strategies"
EMA, SMA, RSI, GRID = "ema_cross:BTCUSDT:1h", "sma_cross:BTCUSDT:1h", "rsi_reversion:ETHUSDT:4h", "grid:XRPUSDT:1d"
OWN = [EMA, SMA, RSI, GRID]
ROW_KEYS = {"instance_id", "strategy_key", "name", "family", "symbol", "interval", "return_pct", "sharpe",
            "sortino", "max_drawdown_pct", "win_rate", "trades", "exposure_pct", "exposure", "equity",
            "live_since", "updated_at"}


@pytest.fixture(scope="module")
def url():
    return testing.require_test_database()


@pytest.fixture(scope="module")
def http(url):
    with pytest.MonkeyPatch.context() as env:
        env.setenv(db.URL_ENV, url)
        # The migration updates the cluster-wide reader role: with the real password, or it loses its login.
        env.setenv(db.READONLY_PASSWORD_ENV, testing.readonly_password_for_tests() or "")
        env.setenv("TRADINGAGENTS_ENGINE", "off")
        env.setenv("TRADINGAGENTS_AI_STRATEGY", "off")
        with testing.selector_client(app, base_url="http://127.0.0.1:8000") as client:
            yield client


@pytest.fixture
def sql(url):
    """Four instances with metrics, one equity curve and a BTCUSDT price; the bot off. Only these rows are
    removed, and bot_config is held under the bot tests' lock, so concurrent runs on the database don't collide."""
    with psycopg.connect(url, autocommit=True, **db.SESSION) as conn, bot_config_lock(conn):
        conn.execute("DELETE FROM strategy_instances WHERE id = ANY(%s)", (OWN,))
        conn.execute("DELETE FROM bot_trades WHERE instance_id = ANY(%s)", (OWN,))
        for instance in OWN:
            key, symbol, interval = instance.split(":")
            conn.execute("INSERT INTO strategy_instances (id, strategy_key, symbol, interval, params) "
                         "VALUES (%s, %s, %s, %s, '{\"fast\": 12}')", (instance, key, symbol, interval))
        for instance, period, ret, sharpe in [(EMA, "30d", 10, 2.0), (SMA, "30d", 30, 1.0), (RSI, "30d", 5, None),
                                              (GRID, "30d", -2, 0.5), (EMA, "7d", 3, 3.0)]:
            conn.execute("INSERT INTO strategy_metrics (instance_id, period, return_pct, sharpe, sortino, "
                         "max_drawdown_pct, win_rate, exposure_pct, trades) VALUES (%s, %s, %s, %s, 1, -4, 0.5, 60, 7)",
                         (instance, period, ret, sharpe))
        conn.execute("INSERT INTO instance_state (instance_id, exposure, cash, qty, equity) VALUES (%s, 1, 0, 0.01, 1100)",
                     (EMA,))
        # 400 hourly points up to now (the last 10 live), and one 60 days back outside the 30-day window.
        conn.execute("""
            INSERT INTO equity_points (instance_id, ts, equity, exposure, source)
            SELECT %s, date_trunc('hour', now()) - (399 - i) * interval '1 hour', 1000 + i, 1,
                   CASE WHEN i >= 390 THEN 'live' ELSE 'backtest' END
            FROM generate_series(0, 399) AS i""", (EMA,))
        conn.execute("INSERT INTO equity_points VALUES (%s, now() - interval '60 days', 500, 0, 'backtest')", (EMA,))
        conn.execute("INSERT INTO paper_fills (instance_id, ts, side, price, qty, quote, fee, exposure_after, source) "
                     "VALUES (%s, now(), 'BUY', 100000, 0.01, 1000, 1, 1, 'live')", (EMA,))
        conn.execute("INSERT INTO candles VALUES ('BTCUSDT', '1h', date_trunc('hour', now()), 1e5, 1e5, 1e5, 1e5, 1) "
                     "ON CONFLICT (symbol, interval, open_time) DO UPDATE SET close = EXCLUDED.close")
        yield lambda query, *params: conn.execute(query, params).fetchall()


@pytest.fixture
def fake(tmp_path, monkeypatch):
    service, fake = make_service(tmp_path, enabled=False)
    monkeypatch.setattr(service_module, "_service", service)
    return fake


def board(http, **params) -> list[str]:
    """This module's instances, in the order the leaderboard ranks them among any others in the database."""
    response = http.get("/api/strategies/leaderboard", params={"limit": 1000, **params})
    assert response.status_code == 200, response.text
    return [r["instance_id"] for r in response.json() if r["instance_id"] in OWN]


@pytest.mark.unit
def test_catalog_lists_every_strategy(http, sql):
    catalog = {s["key"]: s for s in http.get("/api/strategies").json()}
    assert catalog["tradingagents_ai"]["intervals"] == ["1d"]
    assert set(catalog["ema_cross"]) == {"key", "name", "family", "description", "params", "intervals"}


@pytest.mark.unit
def test_leaderboard_filters_and_sorts(http, sql):
    assert board(http) == [EMA, SMA, GRID, RSI]   # sharpe, nulls last
    assert board(http, sort="return_pct") == [SMA, EMA, RSI, GRID]
    assert board(http, symbol="btcusdt") == [EMA, SMA]
    assert board(http, interval="4h") == [RSI]
    assert board(http, window="7d") == [EMA]
    assert len(http.get("/api/strategies/leaderboard", params={"limit": 1}).json()) == 1
    assert http.get("/api/strategies/leaderboard", params={"sort": "id; DROP TABLE candles"}).status_code == 422

    (row,) = [r for r in http.get("/api/strategies/leaderboard", params={"window": "7d"}).json()
              if r["instance_id"] == EMA]
    assert set(row) == ROW_KEYS
    assert (row["name"], row["family"], row["equity"], row["exposure"]) == ("EMA cross 12/26", "trend", 1100, 1)


@pytest.mark.unit
def test_equity_is_normalised_to_100_and_downsampled(http, sql):
    curves = http.get("/api/strategies/equity", params={"ids": f"{EMA},{SMA}", "window": "30d", "points": 50}).json()
    ema = curves[EMA]
    assert curves[SMA] == [] and 2 < len(ema) <= 50
    assert ema[0]["v"] == pytest.approx(100)                  # the 60-day-old point is outside the window
    assert ema[-1]["v"] == pytest.approx(1399 / 1000 * 100)   # the last point is always kept
    assert all(isinstance(p["t"], int) for p in ema) and [p["t"] for p in ema] == sorted(p["t"] for p in ema)
    assert ema[-1]["t"] - ema[0]["t"] == 399 * 3_600_000

    live = http.get("/api/strategies/equity", params={"ids": EMA, "window": "live"}).json()[EMA]
    assert len(live) == 10 and live[0]["v"] == pytest.approx(100)
    assert live[-1]["v"] == pytest.approx(1399 / 1390 * 100)


@pytest.mark.unit
def test_instance_detail(http, sql):
    detail = http.get(f"/api/strategies/instances/{EMA}").json()
    assert detail["params"] == {"fast": 12} and detail["description"].startswith("Long while the 12-bar EMA")
    assert set(detail["metrics"]) == {"7d", "30d"} and detail["metrics"]["30d"]["sharpe"] == 2.0
    assert detail["recent_fills"][0]["source"] == "live" and detail["equity"] == 1100
    assert http.get("/api/strategies/instances/nope:BTCUSDT:1h").status_code == 404


@pytest.mark.unit
def test_bot_routes_validate_and_need_the_token(http, sql, fake):
    body = {"instance_id": EMA, "size_usdt": 0.10}
    assert http.post("/api/bot/activate", json=body).status_code == 403
    assert http.post("/api/bot/deactivate", json={}).status_code == 403

    def activate(**changes):
        return http.post("/api/bot/activate", json={**body, **changes}, headers=TOKEN)

    assert activate(size_usdt=60).status_code == 422                  # above the 50 USDT per-order limit
    assert activate(size_usdt=0).status_code == 422
    assert activate(instance_id="nope:BTCUSDT:1h").status_code == 404
    assert "not an allowed symbol" in activate(instance_id=GRID).json()["detail"]
    assert activate(size_usdt=4.99).json()["active"]["mode"] == "paper"   # below the 5 USDT minNotional
    assert activate(size_usdt=5).json()["active"]["mode"] == "live"

    status = http.get("/api/bot").json()
    assert status["active"]["instance_id"] == EMA and status["active"]["size_usdt"] == 5
    assert set(status) == {"active", "position", "pnl", "trades", "in_sync", "drift"}
    assert http.post("/api/bot/sync").status_code == 403
    assert http.post("/api/bot/sync", headers=TOKEN).json()["in_sync"]
    assert http.get("/api/bot/preview", params={"instance_id": EMA}).json()["target"] == 1
    assert http.get("/api/bot/preview", params={"instance_id": "nope:BTCUSDT:1h"}).status_code == 404
    assert http.post("/api/bot/deactivate", json={"close_position": True}, headers=TOKEN).json()["active"] is None
    assert "/api/v3/order" not in fake.paths()


@pytest.mark.unit
def test_routes_answer_503_while_the_database_is_offline(http, sql, fake, monkeypatch):
    monkeypatch.setattr(db, "_pool", None)
    for path in ("/api/strategies", "/api/strategies/leaderboard", f"/api/strategies/instances/{EMA}",
                 f"/api/strategies/equity?ids={EMA}", "/api/bot"):
        response = http.get(path)
        assert (response.status_code, response.json()) == (503, {"detail": "database offline"}), path
    for path, body in (("/api/bot/activate", {"instance_id": EMA, "size_usdt": 1}), ("/api/bot/deactivate", {})):
        assert http.post(path, json=body, headers=TOKEN).status_code == 503


def _wait_listening():
    deadline = time.monotonic() + 10
    while not strategies.get_hub().connected:
        assert time.monotonic() < deadline, "the LISTEN connection never came up"
        time.sleep(0.05)


@pytest.mark.unit
def test_ws_needs_the_token_first(http):
    with http.websocket_connect(WS_URL) as ws:
        ws.send_json({"token": "wrong"})
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 1008


@pytest.mark.unit
def test_ws_pushes_leaderboard_rows_on_notify(http, sql, fake):
    with http.websocket_connect(WS_URL) as ws:
        ws.send_json({"token": SESSION_TOKEN})
        assert ws.receive_json()["type"] == "bot"
        _wait_listening()
        sql("SELECT pg_notify('ta_strategy_update', %s)", json.dumps({"instance_ids": [EMA], "bar": "2026-10-24T17:00Z"}))
        frames = {f["window"]: f for f in (ws.receive_json(), ws.receive_json())}
    assert set(frames) == {"7d", "30d"} and {f["type"] for f in frames.values()} == {"leaderboard"}
    (row,) = frames["30d"]["rows"]
    assert set(row) == ROW_KEYS and (row["instance_id"], row["sharpe"], row["equity"]) == (EMA, 2.0, 1100)


@pytest.mark.unit
def test_ws_pushes_bot_status_on_activation_and_signal(http, sql, fake):
    with http.websocket_connect(WS_URL) as ws:
        ws.send_json({"token": SESSION_TOKEN})
        assert ws.receive_json()["active"] is None
        _wait_listening()
        http.post("/api/bot/activate", json={"instance_id": EMA, "size_usdt": 1}, headers=TOKEN)
        entered = ws.receive_json()   # activation enters the instance's current 100% at once
        assert entered["active"]["mode"] == "paper" and entered["in_sync"]
        (trade,) = entered["trades"]
        assert isinstance(trade["id"], int) and (trade["side"], trade["real"], trade["status"]) == ("BUY", False, "filled")
        sql("SELECT pg_notify('ta_bot_signal', %s)", json.dumps(
            {"instance_id": EMA, "target": 0.0, "previous": 1.0, "price": 100000.0, "bar": "2026-10-24T17:00Z"}))
        frame = ws.receive_json()
    assert frame["type"] == "bot" and frame["position"]["exposure"] == pytest.approx(0)
    assert [t["side"] for t in frame["trades"]] == ["SELL", "BUY"]
