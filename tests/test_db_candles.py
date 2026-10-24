"""Candles in Postgres: upsert and range reads, the resumable backfill, the vendor's read-through.

Against the tests' own database (<name>_test); skipped when Postgres is not
running. Binance is replaced by in-memory candle sources: nothing leaves the machine.
"""
import time
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from tradingagents import db
from tradingagents.dataflows.vendors.binance import ohlcv
from tradingagents.db import backfill, candles, testing

T0 = datetime(2001, 1, 1, tzinfo=UTC)


def _candles(start: datetime, count: int, step: int, base: float = 100.0) -> list[dict]:
    return [{"time": int(start.timestamp()) + i * step, "open": base + i, "high": base + i + 2,
             "low": base + i - 1, "close": base + i + 0.5, "volume": 10.0 * i} for i in range(count)]


@pytest.fixture(scope="module")
def url():
    return testing.require_test_database()


@pytest.fixture
def conn(url):
    with psycopg.connect(url, **db.SESSION) as conn:
        conn.execute("DELETE FROM candles")
        conn.commit()
        yield conn


@pytest.mark.unit
def test_upsert_then_read_a_range(conn):
    rows = _candles(T0, 6, 3600)
    assert candles.upsert_candles(conn, "TESTUSDT", "1h", rows) == 6
    assert candles.upsert_candles(conn, "TESTUSDT", "1h", rows) == 6   # again: nothing duplicated
    conn.commit()
    assert candles.read_candles(conn, "TESTUSDT", "1h", T0 + timedelta(hours=1), T0 + timedelta(hours=4)) == rows[1:4]
    assert candles.read_candles(conn, "TESTUSDT", "1d", T0, T0 + timedelta(days=1)) == []   # another series
    assert candles.latest_open_time(conn, "TESTUSDT", "1h") == T0 + timedelta(hours=5)
    assert candles.latest_open_time(conn, "OTHERUSDT", "1h") is None

    corrected = {**rows[2], "close": 1.0}
    candles.upsert_candles(conn, "TESTUSDT", "1h", [corrected])
    assert candles.read_candles(conn, "TESTUSDT", "1h", T0, T0 + timedelta(days=1))[2] == corrected
    assert conn.execute("SELECT count(*) FROM candles").fetchone() == (6,)


@pytest.fixture
def exchange(monkeypatch):
    """Binance's klines endpoint over a fixed 15m series: startTime, endTime and limit honoured."""
    series = _candles(T0, 200, 900)
    calls = []

    def fake_get_klines(symbol, interval, limit, start_ms, end_ms):
        calls.append((symbol, interval, start_ms, end_ms))
        return [c for c in series if start_ms <= c["time"] * 1000 <= end_ms][:limit]

    monkeypatch.setattr(backfill, "get_klines", fake_get_klines)
    monkeypatch.setattr(backfill, "KLINES_PAGE", 25)
    monkeypatch.setattr(backfill, "PAUSE", 0)
    return calls


@pytest.mark.unit
def test_backfill_pages_through_closed_candles_and_resumes(conn, exchange):
    # Ten 15m candles in, the tenth still open: nine are stored, in pages of 25 (one here).
    now = T0.timestamp() + 9 * 900 + 60
    assert backfill.backfill_series(conn, "BTCUSDT", "15m", days=1, now=now) == 9
    assert candles.latest_open_time(conn, "BTCUSDT", "15m") == T0 + timedelta(minutes=15 * 8)

    # Later: it carries on after the newest stored candle, across several pages.
    exchange.clear()
    now = T0.timestamp() + 100 * 900 + 60
    assert backfill.backfill_series(conn, "BTCUSDT", "15m", days=1, now=now) == 91
    assert exchange[0][2] == int((T0 + timedelta(minutes=15 * 9)).timestamp()) * 1000
    assert len(exchange) == 4   # 25 + 25 + 25 + 16
    stored = candles.read_candles(conn, "BTCUSDT", "15m", T0, T0 + timedelta(days=2))
    assert [c["time"] for c in stored] == [int(T0.timestamp()) + i * 900 for i in range(100)]

    exchange.clear()
    assert backfill.backfill_series(conn, "BTCUSDT", "15m", days=1, now=now) == 0   # nothing new


@pytest.fixture
def stored_days(conn, url, monkeypatch):
    """Ten daily BTCUSDT candles from 2001-01-01 in the database the vendor reads."""
    candles.upsert_candles(conn, "BTCUSDT", "1d", _candles(T0, 10, 86400))
    conn.commit()
    monkeypatch.setenv(db.URL_ENV, url)
    monkeypatch.setattr(db, "_offline_until", 0.0)


@pytest.fixture
def rest(monkeypatch):
    calls = []
    remote = _candles(T0, 40, 86400, base=500.0)   # different prices: the source shows in the output

    def fake_get_klines(symbol, interval="1d", limit=500, start_ms=None, end_ms=None):
        calls.append((start_ms, end_ms))
        return [c for c in remote if start_ms <= c["time"] * 1000 <= end_ms][:limit]

    monkeypatch.setattr(ohlcv, "get_klines", fake_get_klines)
    return calls


@pytest.mark.unit
def test_vendor_reads_a_fully_stored_range_from_postgres(stored_days, rest):
    out = ohlcv.get_stock_data("BTC-USD", "2001-01-02", "2001-01-06")
    assert rest == []
    assert "# Total records: 5" in out
    assert "2001-01-02,101.0,103.0,100.0,101.5,10.0" in out
    assert "2001-01-06,105.0" in out and "2001-01-07" not in out


@pytest.mark.unit
def test_vendor_asks_binance_when_a_day_is_missing(stored_days, rest):
    out = ohlcv.get_stock_data("BTC-USD", "2001-01-08", "2001-01-12")   # stored through 01-10 only
    assert len(rest) == 1
    assert "# Total records: 5" in out and "2001-01-08,507.0" in out


@pytest.mark.unit
def test_vendor_asks_binance_while_postgres_is_offline(stored_days, rest, monkeypatch):
    monkeypatch.setattr(db, "_offline_until", time.monotonic() + 60)   # unreachable a moment ago
    out = ohlcv.get_stock_data("BTC-USD", "2001-01-02", "2001-01-06")
    assert len(rest) == 1 and "2001-01-02,501.0" in out
