"""The AI strategy's daily scheduler, on a fake clock: nothing waits for real and no model is called.

Runs go through the real run registry (``analyze.Runs``) on the tests' own
database with a scripted ``run_analysis``, so their signals land in
``ai_signals`` and NOTIFY ``ta_ai_signal`` as they would at 00:10 UTC.
"""
import asyncio
import datetime
import json
import threading

import psycopg
import pytest

from tradingagents import db
from tradingagents.db import testing
from tradingagents.web import ai_strategy, analyze

UTC = datetime.UTC
MIDNIGHT = datetime.datetime(2026, 10, 24, 0, 10, tzinfo=UTC)


class Clock:
    """A clock that moves only when the code under test sleeps; every sleep is recorded."""

    def __init__(self, start=MIDNIGHT):
        self.time, self.sleeps = start, []

    def now(self):
        return self.time

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.time += datetime.timedelta(seconds=seconds)
        await asyncio.sleep(0.01)   # real time, for the runs' threads


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
def rows(url):
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DELETE FROM analysis_runs")
        conn.execute("DELETE FROM ai_signals")
        yield lambda query: conn.execute(query).fetchall()


@pytest.mark.unit
@pytest.mark.parametrize(("now", "expected"), [
    ("2026-10-24T00:05:00+00:00", "2026-10-24T00:10:00+00:00"),
    ("2026-10-24T00:10:00+00:00", "2026-10-25T00:10:00+00:00"),
    ("2026-10-24T18:00:00+00:00", "2026-10-25T00:10:00+00:00"),
    ("2026-10-24T01:00:00+02:00", "2026-10-24T00:10:00+00:00"),   # 23:00 UTC on the 23rd
])
def test_next_run_is_the_next_0010_utc(now, expected):
    assert ai_strategy.next_run_at(datetime.datetime.fromisoformat(now)) == datetime.datetime.fromisoformat(expected)


@pytest.mark.unit
@pytest.mark.parametrize(("value", "on"), [("", True), ("on", True), ("off", False), ("OFF", False), ("0", False)])
def test_the_switch(monkeypatch, value, on):
    monkeypatch.setenv(ai_strategy.ENV, value)
    assert ai_strategy.enabled() is on


@pytest.mark.unit
def test_daily_runs_are_sequential_and_record_signals(url, runner, rows, tmp_path, monkeypatch):
    ratings = {"BTC-USD": "Buy", "ETH-USD": "Underweight", "SOL-USD": None}   # SOL's run fails
    running, overlap, seen = [], [], []
    lock = threading.Lock()

    def scripted(ticker, trade_date, analysts, emit, stop):
        with lock:
            running.append(ticker)
            overlap.append(len(running))
        seen.append((ticker, trade_date, analysts))
        try:
            if ratings[ticker] is None:
                raise RuntimeError("model unavailable")
            emit({"type": "decision", "rating": ratings[ticker], "summary": f"**Rating: {ratings[ticker]}**"})
            return ratings[ticker]
        finally:
            with lock:
                running.remove(ticker)

    monkeypatch.setattr(analyze, "run_analysis", scripted)
    runs = analyze.Runs(tmp_path, url)
    clock = Clock()
    with psycopg.connect(url, autocommit=True) as listener:
        listener.execute(f"LISTEN {ai_strategy.CHANNEL}")
        try:
            signals = runner.run(ai_strategy.run_daily(runs, ["BTCUSDT", "ETHUSDT", "SOLUSDT"], "2026-10-24",
                                                       clock.now, clock.sleep))
            notified = [json.loads(n.payload) for n in listener.notifies(timeout=2, stop_after=2)]
        finally:
            runs.close()

    assert signals == {"BTCUSDT": "Buy", "ETHUSDT": "Underweight"}
    assert [t for t, _, _ in seen] == ["BTC-USD", "ETH-USD", "SOL-USD"] and max(overlap) == 1
    assert all(d == "2026-10-24" and a == analyze.ANALYSTS for _, d, a in seen)
    assert notified == [{"symbol": "BTCUSDT", "rating": "Buy"}, {"symbol": "ETHUSDT", "rating": "Underweight"}]
    recorded = rows("SELECT s.symbol, s.rating, r.symbol, r.status FROM ai_signals s"
                    " JOIN analysis_runs r ON r.id = s.run_id ORDER BY s.id")
    assert recorded == [("BTCUSDT", "Buy", "BTCUSDT", "done"), ("ETHUSDT", "Underweight", "ETHUSDT", "done")]
    assert rows("SELECT status, error FROM analysis_runs WHERE symbol = 'SOLUSDT'") == [("error", "model unavailable")]


class FakeRuns:
    """A registry with no free slot for its first ``busy`` starts, whose runs never finish."""

    def __init__(self, busy=0):
        self.busy, self.starts, self.cancelled = busy, 0, []

    def start(self, symbol, ticker, trade_date, analysts):
        self.starts += 1
        if self.starts <= self.busy:
            return None
        return analyze.Run(id=f"fake-{symbol}", symbol=symbol, ticker=ticker, date=trade_date,
                           analysts=analysts, started_at=MIDNIGHT.isoformat())

    def cancel(self, run):
        run.status = "cancelled"
        self.cancelled.append(run.id)


@pytest.mark.unit
def test_a_busy_registry_is_waited_for_and_a_hung_run_cancelled(runner, rows, monkeypatch):
    monkeypatch.setattr(ai_strategy, "RUN_TIMEOUT", 10 * ai_strategy.POLL_SECONDS)
    runs, clock = FakeRuns(busy=3), Clock()
    signals = runner.run(ai_strategy.run_daily(runs, ["BTCUSDT"], "2026-10-24", clock.now, clock.sleep))

    assert runs.starts == 4   # three refusals (MAX_RUNNING already running), then a start
    assert runs.cancelled == ["fake-BTCUSDT"] and signals == {}
    assert clock.sleeps == [ai_strategy.POLL_SECONDS] * 10   # waiting for the slot counts towards the timeout
    assert rows("SELECT * FROM ai_signals") == []


@pytest.mark.unit
def test_no_free_slot_all_day_starts_nothing(runner, monkeypatch):
    monkeypatch.setattr(ai_strategy, "RUN_TIMEOUT", 4 * ai_strategy.POLL_SECONDS)
    runs, clock = FakeRuns(busy=10**6), Clock()
    assert runner.run(ai_strategy.run_daily(runs, ["BTCUSDT", "ETHUSDT"], "2026-10-24", clock.now, clock.sleep)) == {}
    assert runs.starts == 10 and runs.cancelled == []   # five tries for each symbol


@pytest.mark.unit
def test_the_scheduler_sleeps_until_0010_utc_then_runs_each_day(runner, monkeypatch):
    days = []

    async def fake_daily(runs, symbols, trade_date, now, sleep):
        days.append((symbols, trade_date, now()))
        if len(days) == 1:
            raise RuntimeError("a bad day")   # logged; the scheduler goes on to the next
        raise asyncio.CancelledError   # ends the test's loop

    monkeypatch.setattr(ai_strategy, "run_daily", fake_daily)
    clock = Clock(datetime.datetime(2026, 10, 23, 18, 0, tzinfo=UTC))
    with pytest.raises(asyncio.CancelledError):
        runner.run(ai_strategy.scheduler(None, ["BTCUSDT"], clock.now, clock.sleep))

    assert clock.sleeps == [6 * 3600 + 600, 86400]
    assert days == [(["BTCUSDT"], "2026-10-24", MIDNIGHT),
                    (["BTCUSDT"], "2026-10-25", MIDNIGHT + datetime.timedelta(days=1))]


@pytest.mark.unit
@pytest.mark.parametrize(("switch", "database_up", "starts"), [("", True, True), ("off", True, False),
                                                                ("", False, False)])
def test_the_lifespan_task_starts_only_when_on_with_a_database(runner, monkeypatch, switch, database_up, starts):
    monkeypatch.setenv(ai_strategy.ENV, switch)
    started = []

    async def fake_scheduler(runs, symbols):
        started.append(symbols)
        await asyncio.Event().wait()

    monkeypatch.setattr(ai_strategy, "scheduler", fake_scheduler)

    async def lifetime():
        async with ai_strategy.running_scheduler(None, database_up) as task:
            await asyncio.sleep(0.01)
        return task

    task = runner.run(lifetime())
    assert (task is not None) is starts
    if starts:
        assert task.cancelled() and started == [["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]]
