"""The engine supervisor (a fake engine that dies) and the shape of /api/health."""

import asyncio
import sys

import pytest
from fastapi.testclient import TestClient

from tradingagents.web import app as app_module
from tradingagents.web.app import EngineSupervisor, app

DIES = [sys.executable, "-c", "raise SystemExit(3)"]


@pytest.fixture(scope="module")
def run():
    # One event loop for the module, made before the per-test network block (see test_web_market).
    with asyncio.Runner() as runner:
        runner.get_loop()
        yield runner.run


def fast(supervisor: EngineSupervisor, failures: int = 10) -> EngineSupervisor:
    supervisor.BACKOFF_MIN, supervisor.BACKOFF_MAX, supervisor.POLL = 0.01, 0.02, 0.01
    supervisor.MAX_FAILURES = failures
    return supervisor


async def until(condition, timeout: float = 20.0) -> None:
    async def poll():
        while not condition():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout)


@pytest.mark.unit
def test_respawns_an_engine_that_dies(run, tmp_path):
    marker = tmp_path / "crashed-once"
    # Dies on its first run, then stays up like a healthy engine.
    command = [sys.executable, "-c",
               "import pathlib, sys, time; p = pathlib.Path(sys.argv[1])\n"
               "if not p.exists(): p.touch(); sys.exit(1)\n"
               "time.sleep(60)", str(marker)]
    supervisor = fast(EngineSupervisor(command))

    async def scenario():
        supervisor.start()
        try:
            await until(lambda: supervisor.restarts == 1 and supervisor.alive)
            process = supervisor.process
        finally:
            await supervisor.close()
        return process

    process = run(scenario())
    assert marker.exists() and not supervisor.gave_up
    assert process.poll() is not None   # stopped on shutdown, not left running


@pytest.mark.unit
def test_gives_up_on_a_crash_loop(run):
    supervisor = fast(EngineSupervisor(DIES), failures=3)

    async def scenario():
        supervisor.start()
        try:
            await until(lambda: supervisor.gave_up)
        finally:
            await supervisor.close()

    run(scenario())
    assert supervisor.restarts == 2 and not supervisor.alive
    assert supervisor.process.returncode == 3


@pytest.mark.unit
def test_backoff_doubles_up_to_the_cap(run, monkeypatch):
    supervisor = EngineSupervisor(DIES)
    supervisor.MAX_FAILURES, supervisor.BACKOFF_MAX, supervisor.POLL = 7, 8.0, 0.001
    slept = []
    real_sleep = asyncio.sleep

    async def sleep(seconds):
        if seconds >= 1:   # the backoff, not the exit polls
            slept.append(seconds)
        await real_sleep(0.001)

    monkeypatch.setattr(app_module.asyncio, "sleep", sleep)

    async def scenario():
        supervisor.start()
        try:
            await until(lambda: supervisor.gave_up)
        finally:
            await supervisor.close()

    run(scenario())
    assert slept == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]


@pytest.fixture(scope="module")
def http():
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client


@pytest.mark.unit
def test_health_shape_while_engine_and_database_are_off(http):
    # The suite runs with no database and TRADINGAGENTS_ENGINE / TRADING_PRICE_STREAM off.
    assert http.get("/api/health").json() == {
        "ok": True,
        "engine": {"enabled": False, "alive": False, "restarts": 0, "gave_up": False,
                   "last_bar_at": None, "lag_s": None},
        "db": False,
        "streams": {"price": "off", "account": "unconfigured"},
    }


@pytest.mark.unit
def test_health_reports_a_supervised_engine(http, monkeypatch):
    supervisor = EngineSupervisor(DIES)
    supervisor.restarts, supervisor.gave_up = 10, True
    monkeypatch.setattr(app_module, "_engine", supervisor)
    engine = http.get("/api/health").json()["engine"]
    assert engine["enabled"] and engine["gave_up"] and not engine["alive"] and engine["restarts"] == 10
