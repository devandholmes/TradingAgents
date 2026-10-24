"""Analysis runs saved in Postgres: round trip, a restart mid-run, the one-time import of JSON runs.

Against the tests' own database (<name>_test); skipped when Postgres is not
running. The graph is replaced by a scripted run: no model is called.
"""
import json
import threading

import psycopg
import pytest

from tradingagents.db import testing
from tradingagents.web import analyze

SCRIPT = [
    {"type": "stage", "stage": "market", "status": "running", "agent": "Market Analyst"},
    {"type": "activity", "agent": "Market Analyst", "tool": "get_stock_data"},
    {"type": "report", "stage": "market", "section": "market_report", "title": "Market Analysis",
     "content": "Market is up"},
    {"type": "stage", "stage": "market", "status": "done", "agent": "Market Analyst"},
    {"type": "decision", "rating": "Buy", "summary": "**Rating: Buy**"},
]


@pytest.fixture(scope="module")
def url():
    return testing.require_test_database()


@pytest.fixture
def db_rows(url):
    """A query on the tests' database, which starts each test with no runs."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DELETE FROM analysis_runs")
        yield lambda query, *params: conn.execute(query, params).fetchall()


@pytest.fixture
def gate(monkeypatch):
    """Scripted runs; while the gate is closed, a run holds after its first two events."""
    gate = threading.Event()
    gate.set()

    def scripted(ticker, trade_date, analysts, emit, stop):
        for i, event in enumerate(SCRIPT):
            if i == 2:
                gate.wait(5)
            if stop.is_set():
                return None
            emit(event)
        return "Buy"

    monkeypatch.setattr(analyze, "run_analysis", scripted)
    return gate


def _run(runs):
    return runs.start("BTCUSDT", "BTC-USD", "2026-10-20", ["market"])


def _wait(run):
    for worker in [t for t in threading.enumerate() if t.name == f"analysis-{run.id}"]:
        worker.join(5)
        assert not worker.is_alive()


def _bare(events):
    return [{k: v for k, v in e.items() if k not in ("id", "ts")} for e in events]


@pytest.mark.unit
def test_a_run_round_trips_through_postgres(url, db_rows, gate, tmp_path):
    runs = analyze.Runs(tmp_path, url)
    run = _run(runs)
    _wait(run)
    restarted = analyze.Runs(tmp_path, url)
    try:
        assert restarted.runs[run.id].events == run.events   # ids and timestamps too
        assert restarted.detail(restarted.runs[run.id]) == runs.detail(run)
        assert restarted.list(10) == runs.list(10)
        assert restarted.detail(restarted.runs[run.id])["rating"] == "Buy"
    finally:
        runs.close()
        restarted.close()

    assert db_rows("SELECT status, rating, date::text, analysts FROM analysis_runs") == [
        ("done", "Buy", "2026-10-20", ["market"])]
    assert db_rows("SELECT count(*) FROM analysis_events") == [(len(run.events),)]
    assert db_rows("SELECT section, stage, title, content FROM analysis_reports") == [
        ("market_report", "market", "Market Analysis", "Market is up")]
    assert db_rows("SELECT payload FROM analysis_events WHERE type = 'decision'") == [
        ({"rating": "Buy", "summary": "**Rating: Buy**"},)]
    assert list(tmp_path.iterdir()) == []   # no JSON files beside a database


@pytest.mark.unit
def test_a_run_cut_short_by_a_restart_is_an_error(url, db_rows, gate, tmp_path):
    gate.clear()
    runs = analyze.Runs(tmp_path, url)
    held = _run(runs)
    try:
        for _ in range(100):
            if len(held.events) >= 2:
                break
            threading.Event().wait(0.05)
        restarted = analyze.Runs(tmp_path, url)   # a new server, while the run is still going
        reloaded = restarted.runs[held.id]
        assert reloaded.status == "error" and reloaded.error == "server restarted"
        # Tool calls are saved with the next stage or report: this one was not yet.
        assert _bare(reloaded.events) == [SCRIPT[0], {"type": "error", "message": "server restarted"},
                                          {"type": "status", "status": "error"}]
        restarted.close()
        assert db_rows("SELECT status, error FROM analysis_runs") == [("error", "server restarted")]
    finally:
        runs.cancel(held)
        gate.set()
        _wait(held)
        runs.close()


@pytest.mark.unit
def test_json_runs_are_imported_once(url, db_rows, gate, tmp_path):
    offline = analyze.Runs(tmp_path)   # a server without the database: one JSON file per run
    done = _run(offline)
    _wait(done)
    held = json.loads((tmp_path / f"{done.id}.json").read_text())
    (tmp_path / "abandoned.json").write_text(json.dumps(
        {**held, "id": "abandoned", "status": "running", "rating": None, "finished_at": None,
         "events": held["events"][:1]}))

    imported = analyze.Runs(tmp_path, url)
    imported.close()
    again = analyze.Runs(tmp_path, url)   # the files are still there: nothing is imported twice
    try:
        assert again.detail(again.runs[done.id]) == offline.detail(done)
        assert again.runs["abandoned"].error == "server restarted"
    finally:
        again.close()
    assert set(db_rows("SELECT id, status FROM analysis_runs")) == {("abandoned", "error"), (done.id, "done")}
    assert db_rows("SELECT count(*) FROM analysis_events WHERE run_id = %s", done.id) == [(len(done.events),)]
    assert db_rows("SELECT count(*) FROM analysis_events WHERE run_id = 'abandoned'") == [(3,)]
