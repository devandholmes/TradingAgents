"""Migrations, the read-only role and the shared pool, against the real Postgres.

Skipped when .env names no database or it is not running (docker compose up -d
postgres). Everything is written to a database of the tests' own (<name>_test).
"""
import asyncio
import sys

import psycopg
import pytest
from psycopg import conninfo, errors

from tradingagents import db
from tradingagents.db import migrate, testing

TABLES = {
    "candles", "strategy_instances", "paper_fills", "equity_points", "instance_state", "strategy_metrics",
    "ai_signals", "bot_config", "bot_trades", "analysis_runs", "analysis_events", "analysis_reports",
}


@pytest.fixture(scope="module")
def url():
    return testing.require_test_database()


@pytest.fixture(scope="module")
def runner():
    # The loop tradingagents-web serves on. Made before the per-test network block:
    # on Windows a loop opens a socket pair to itself.
    with asyncio.Runner(loop_factory=db.uvicorn_loop_factory) as runner:
        runner.get_loop()
        yield runner


@pytest.mark.unit
def test_migrations_apply_once_and_again_change_nothing(url):
    with psycopg.connect(url) as conn:
        assert migrate.apply_migrations(conn) == []   # the fixture applied them
        assert migrate.apply_migrations(conn) == []
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert versions == sorted(p.stem for p in migrate.MIGRATIONS_DIR.glob("*.sql"))
        tables = {row[0] for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
        assert tables >= TABLES | {"schema_migrations"}
        assert conn.execute("SELECT id, enabled FROM bot_config").fetchall() == [(1, False)]


@pytest.mark.unit
def test_migrate_reports_an_unreachable_database_without_raising(monkeypatch):
    monkeypatch.setattr(db, "_offline_until", 0.0)
    assert migrate.migrate("postgresql://nobody@127.0.0.1:1/none") is False
    # ...and the next connection fails at once instead of waiting out the timeout again.
    with pytest.raises(psycopg.OperationalError, match="moments ago"):
        db.connect("postgresql://nobody@127.0.0.1:1/none")


@pytest.mark.unit
def test_reader_role_reads_but_cannot_write(url):
    password = testing.readonly_password_for_tests()
    if not password:
        pytest.skip(f"{db.READONLY_PASSWORD_ENV} is not set in .env")
    reader = conninfo.make_conninfo(url, user=db.READER_ROLE, password=password)
    with psycopg.connect(reader, autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM bot_config").fetchone() == (1,)
        for statement in (
            "UPDATE bot_config SET enabled = true",
            "INSERT INTO candles VALUES ('X', '1d', now(), 1, 1, 1, 1, 1)",
            "DELETE FROM analysis_runs",
            "CREATE TABLE intruder (id int)",
        ):
            with pytest.raises((errors.InsufficientPrivilege, errors.ReadOnlySqlTransaction)):
                conn.execute(statement)
        # Even out of the read-only default, the grants refuse a write.
        conn.execute("SET default_transaction_read_only = off")
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("UPDATE bot_config SET enabled = true")


@pytest.mark.unit
def test_pool_runs_queries_on_the_servers_event_loop(url, runner, monkeypatch):
    monkeypatch.setenv(db.URL_ENV, url)
    # The migration updates the cluster-wide reader role: with the real password, or it loses its login.
    monkeypatch.setenv(db.READONLY_PASSWORD_ENV, testing.readonly_password_for_tests() or "")

    async def query():
        async with db.running_database() as pool:
            assert pool is db.pool() and db.db_available()
            async with pool.connection() as conn:
                return await (await conn.execute("SELECT count(*) FROM bot_config")).fetchone()

    if sys.platform == "win32":   # uvicorn's default there, ProactorEventLoop, is refused
        assert isinstance(runner.get_loop(), asyncio.SelectorEventLoop)
    assert runner.run(query()) == (1,)
    assert not db.db_available()


@pytest.mark.unit
def test_no_database_url_means_offline_at_once(runner, monkeypatch):
    monkeypatch.setenv(db.URL_ENV, "")

    async def offline():
        async with db.running_database() as pool:
            return pool, db.db_available()

    assert runner.run(offline()) == (None, False)
