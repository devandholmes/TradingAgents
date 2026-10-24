"""Trading routes and the local-only middleware: session token, Host and Origin checks, response shapes.

Each test swaps in a TradingService on a fake Binance (see test_trading_guard).
"""
import pytest
from fastapi.testclient import TestClient

from tests.test_trading_guard import FakeBinance, make_service
from tradingagents.trading import service as service_module
from tradingagents.web.app import app
from tradingagents.web.trading import SESSION_TOKEN

TOKEN = {"X-TA-Session": SESSION_TOKEN}
ORDER = {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quote_amount": 20}


@pytest.fixture(scope="module")
def http():
    # Started once, before the per-test network block (see test_web_market).
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        yield client


@pytest.fixture
def fake(tmp_path, monkeypatch):
    service, fake = make_service(tmp_path, enabled=False)
    monkeypatch.setattr(service_module, "_service", service)
    return fake


@pytest.mark.unit
def test_session_returns_a_token(http):
    assert http.get("/api/session").json() == {"token": SESSION_TOKEN}


@pytest.mark.unit
@pytest.mark.parametrize("method, path", [
    ("GET", "/api/trading/status"),
    ("POST", "/api/trading/enabled"),
    ("GET", "/api/account"),
    ("GET", "/api/orders/open"),
    ("POST", "/api/orders"),
    ("DELETE", "/api/orders/BTCUSDT/1"),
    ("GET", "/api/trades/recent"),
    ("POST", "/api/chat"),
    ("POST", "/api/analyses"),
    ("DELETE", "/api/analyses/r_missing"),
])
def test_token_is_required(http, fake, method, path):
    assert http.request(method, path).status_code == 403
    assert http.request(method, path, headers={"X-TA-Session": "wrong"}).status_code == 403


@pytest.mark.unit
def test_dashboard_reads_stay_token_free(http):
    assert http.get("/api/health").status_code == 200


@pytest.mark.unit
@pytest.mark.parametrize("headers", [
    {"host": "evil.example"},
    {"host": "evil.example:8000"},
    {"origin": "http://evil.example"},
    {"origin": "http://127.0.0.1:9999"},
])
def test_foreign_host_or_origin_is_refused(http, fake, headers):
    assert http.get("/api/health", headers=headers).status_code == 403
    assert http.get("/api/trading/status", headers={**TOKEN, **headers}).status_code == 403


@pytest.mark.unit
@pytest.mark.parametrize("headers", [
    {"origin": "http://127.0.0.1:8000"},
    {"origin": "http://localhost:5173"},
    {"host": "localhost:5173", "origin": "http://localhost:5173"},   # through the Vite proxy
])
def test_own_and_dev_origins_are_allowed(http, fake, headers):
    assert http.get("/api/trading/status", headers={**TOKEN, **headers}).status_code == 200


@pytest.mark.unit
def test_status_shape(http, fake):
    assert http.get("/api/trading/status", headers=TOKEN).json() == {
        "mode": "testnet", "enabled": False, "configured": True, "can_trade": False,
        "limits": {"max_order_usdt": 50.0, "daily_cap_usdt": 200.0,
                   "allowed_symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]},
        "used_today_usdt": 0.0,
    }


@pytest.mark.unit
def test_disabled_order_is_422_with_reason(http, fake):
    response = http.post("/api/orders", json=ORDER, headers=TOKEN)
    assert response.status_code == 422
    assert response.json() == {"detail": "trading disabled"}
    assert fake.requests == []


@pytest.mark.unit
def test_enable_then_order_then_recent(http, fake):
    status = http.post("/api/trading/enabled", json={"enabled": True}, headers=TOKEN).json()
    assert status["enabled"] is True and status["can_trade"] is True

    order = http.post("/api/orders", json=ORDER, headers=TOKEN).json()
    assert order["status"] == "FILLED" and order["order_id"] == 42
    assert set(order) >= {"symbol", "order_id", "side", "type", "price", "orig_qty", "executed_qty", "status", "time"}

    [entry] = http.get("/api/trades/recent", headers=TOKEN).json()
    assert entry["status"] == "filled" and entry["source"] == "user"
    assert http.get("/api/trading/status", headers=TOKEN).json()["used_today_usdt"] == 20


@pytest.mark.unit
def test_binance_error_is_502(http, tmp_path, monkeypatch):
    fake = FakeBinance()
    fake.order_error = {"code": -2010, "msg": "Account has insufficient balance."}
    service, _ = make_service(tmp_path, fake)
    monkeypatch.setattr(service_module, "_service", service)
    response = http.post("/api/orders", json=ORDER, headers=TOKEN)
    assert response.status_code == 502
    assert "insufficient balance" in response.json()["detail"]


@pytest.mark.unit
def test_invalid_order_body_is_422(http, fake):
    body = {**ORDER, "type": "LIMIT"}   # quote_amount on a LIMIT, no price
    assert http.post("/api/orders", json=body, headers=TOKEN).status_code == 422


@pytest.mark.unit
def test_account_open_orders_and_cancel(http, fake):
    account = http.get("/api/account", headers=TOKEN).json()
    assert account["mode"] == "testnet" and account["update_time"] == 1_700_000_000_000
    assert [b["asset"] for b in account["balances"]] == ["USDT", "BTC"]

    [order] = http.get("/api/orders/open", params={"symbol": "BTCUSDT"}, headers=TOKEN).json()
    assert order["order_id"] == 7

    cancelled = http.delete("/api/orders/BTCUSDT/7", headers=TOKEN).json()
    assert cancelled["status"] == "CANCELED" and cancelled["order_id"] == 7


@pytest.mark.unit
def test_unconfigured_account_is_422(http, tmp_path, monkeypatch):
    service, _ = make_service(tmp_path, keys=False)
    monkeypatch.setattr(service_module, "_service", service)
    response = http.get("/api/account", headers=TOKEN)
    assert response.status_code == 422 and "not configured" in response.json()["detail"]
