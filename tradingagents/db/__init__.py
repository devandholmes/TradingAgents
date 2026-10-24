"""Postgres: where candles, strategy results and analysis runs are kept.

``TRADINGAGENTS_DATABASE_URL`` names the database (``docker compose up -d
postgres`` runs it). Nothing here is required: with no URL, or with the database
down, ``open_pool`` returns None, ``db_available()`` is False and callers fall
back to what they did without one.

The web server shares one async pool for small queries on its event loop.
Worker threads and other processes (backfill, the strategy engine) use blocking
``connect()`` connections of their own. Every session runs in UTC.

Async psycopg refuses Windows' ProactorEventLoop, uvicorn's default there:
``uvicorn_loop_factory`` gives uvicorn a SelectorEventLoop (``tradingagents-web``
uses it; a direct run needs ``uvicorn ... --loop tradingagents.db:uvicorn_loop_factory``),
and ``LOOP_FACTORY`` does the same for a process of its own:
``with asyncio.Runner(loop_factory=LOOP_FACTORY) as runner: runner.run(main())``
(``asyncio.run`` takes a loop factory only from Python 3.12).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import psycopg
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

URL_ENV = "TRADINGAGENTS_DATABASE_URL"
READONLY_PASSWORD_ENV = "TRADINGAGENTS_DATABASE_READONLY_PASSWORD"
READER_ROLE = "ta_reader"
CONNECT_TIMEOUT = 3   # seconds
# Every connection, pooled or not: timestamps come back in UTC whatever the server's zone.
SESSION = {"options": "-c TimeZone=UTC", "connect_timeout": CONNECT_TIMEOUT}

# For asyncio.Runner(loop_factory=LOOP_FACTORY): None is asyncio's default.
LOOP_FACTORY: Callable[[], asyncio.AbstractEventLoop] | None = (
    asyncio.SelectorEventLoop if sys.platform == "win32" else None
)

OFFLINE_RETRY = 60   # seconds

_pool: AsyncConnectionPool | None = None
_offline_until = 0.0   # time.monotonic() before which connect() fails without trying


def database_url() -> str | None:
    return os.environ.get(URL_ENV) or None


def uvicorn_loop_factory() -> asyncio.AbstractEventLoop:
    """uvicorn's ``loop`` setting, which names a loop factory: a SelectorEventLoop on Windows,
    uvicorn's own choice elsewhere."""
    if LOOP_FACTORY is not None:
        return LOOP_FACTORY()
    from uvicorn.loops.auto import auto_loop_factory

    return auto_loop_factory(use_subprocess=False)()


def describe(exc: BaseException) -> str:
    """An error's first line, for logs: libpq names the host and user, never the password."""
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text.splitlines()[0] if text else ''}"


def connect(url: str | None = None, **kwargs) -> psycopg.Connection:
    """A blocking connection, for worker threads and other processes, never the event loop.

    A database that could not be reached is not tried again for ``OFFLINE_RETRY``
    seconds: on Windows even a refused connection takes the whole timeout.
    """
    global _offline_until
    url = url or database_url()
    if not url:
        raise psycopg.OperationalError(f"{URL_ENV} is not set")
    if time.monotonic() < _offline_until:
        raise psycopg.OperationalError("Postgres was unreachable moments ago")
    try:
        return psycopg.connect(url, **{**SESSION, **kwargs})
    except psycopg.OperationalError:
        _offline_until = time.monotonic() + OFFLINE_RETRY
        raise


def pool() -> AsyncConnectionPool | None:
    """The web server's pool, or None while the database is offline."""
    return _pool


def db_available() -> bool:
    return _pool is not None


async def open_pool(url: str | None = None, min_size: int = 1, max_size: int = 4) -> AsyncConnectionPool | None:
    """Open the shared pool; None, logged, when there is no URL or the database cannot be reached."""
    global _pool
    url = url or database_url()
    if not url:
        return None
    candidate = AsyncConnectionPool(url, min_size=min_size, max_size=max_size, kwargs=SESSION, open=False)
    try:
        await candidate.open(wait=True, timeout=CONNECT_TIMEOUT + 1)
    except Exception as exc:
        logger.warning("Postgres is offline (%s): run `docker compose up -d postgres`", describe(exc))
        await candidate.close()
        return None
    _pool = candidate
    return candidate


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        current, _pool = _pool, None
        await current.close()


@asynccontextmanager
async def running_database() -> AsyncIterator[AsyncConnectionPool | None]:
    """Migrate the database, then hold the shared pool open; None throughout when it is offline."""
    from tradingagents.db.migrate import migrate

    # Blocking DDL in a thread, off the event loop; an offline database is not fatal.
    if not database_url() or not await asyncio.to_thread(migrate):
        yield None
        return
    try:
        yield await open_pool()
    finally:
        await close_pool()
