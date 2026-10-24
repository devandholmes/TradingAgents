"""Helpers for tests against the real Postgres (``docker compose up -d postgres``).

The suite blanks every ``TRADINGAGENTS_*`` variable (tests/conftest.py), so the
database URL is read from ``.env`` itself. A test that needs the database calls
``require_database()`` and is skipped when it is not set up or not reachable.
libpq opens its own sockets, so the suite's network block does not stop it.
"""

from __future__ import annotations

import os

from tradingagents.db import LOOP_FACTORY, READONLY_PASSWORD_ENV, URL_ENV


def _dotenv(name: str) -> str | None:
    from dotenv import dotenv_values, find_dotenv

    return os.environ.get(name) or dotenv_values(find_dotenv(usecwd=True)).get(name) or None


def database_url_for_tests() -> str | None:
    return _dotenv(URL_ENV)


def readonly_password_for_tests() -> str | None:
    return _dotenv(READONLY_PASSWORD_ENV)


def require_database() -> str:
    """The database URL, after skipping the calling test when it cannot be reached."""
    import psycopg
    import pytest

    url = database_url_for_tests()
    if not url:
        pytest.skip(f"{URL_ENV} is not set in .env")
    try:
        psycopg.connect(url, connect_timeout=3).close()
    except psycopg.Error as exc:
        pytest.skip(f"Postgres is not reachable ({type(exc).__name__}): docker compose up -d postgres")
    return url


def require_test_database() -> str:
    """The URL of a database of the tests' own on the same server (``<name>_test``), migrated.

    Tests write there, never into the database the app and its history use. The
    reader role is cluster-wide: it is updated only with the password from .env.
    """
    import psycopg
    from psycopg import conninfo, sql

    from tradingagents.db.migrate import apply_migrations, ensure_reader

    url = require_database()
    name = f"{conninfo.conninfo_to_dict(url).get('dbname') or 'postgres'}_test"
    with psycopg.connect(url, autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    test_url = conninfo.make_conninfo(url, dbname=name)
    with psycopg.connect(test_url) as conn:
        apply_migrations(conn)
        if password := readonly_password_for_tests():
            ensure_reader(conn, password)
    return test_url


def selector_client(app, **kwargs):
    """A TestClient whose event loop async psycopg accepts (a SelectorEventLoop on Windows).

    Enter it in a module-scoped fixture, like the suite's other clients: on Windows
    a new loop opens a socket pair to itself, which the per-test network block refuses.
    """
    from fastapi.testclient import TestClient

    if LOOP_FACTORY is not None:
        kwargs.setdefault("backend_options", {"loop_factory": LOOP_FACTORY})
    return TestClient(app, **kwargs)
