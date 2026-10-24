"""Versioned SQL migrations: ``migrations/NNN_name.sql``, applied once each, in order.

``schema_migrations`` records the versions applied. All pending migrations run in
one transaction under an advisory lock, so the web server and the engine starting
together neither race nor half-apply a version. The read-only role
``ta_reader`` is brought up to date on every run: SELECT on every table, its
password from ``TRADINGAGENTS_DATABASE_READONLY_PASSWORD`` (no password, no login).

``python -m tradingagents.db.migrate`` applies them by hand.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import psycopg
from psycopg import sql

from tradingagents.db import READER_ROLE, READONLY_PASSWORD_ENV, connect, describe

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
LOCK_KEY = 0x7A_DB_0001   # pg_advisory_xact_lock key shared by every migrating process


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    """Apply the pending migrations; the versions applied, oldest first (none: already current)."""
    applied = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.stem in done:
                continue
            conn.execute(path.read_text(encoding="utf-8"))   # no parameters: may hold many statements
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
            applied.append(path.stem)
    return applied


def ensure_reader(conn: psycopg.Connection, password: str | None = None) -> None:
    """Create or update ``ta_reader``: read-only by grants and by session defaults."""
    password = password if password is not None else os.environ.get(READONLY_PASSWORD_ENV) or None
    role = sql.Identifier(READER_ROLE)
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (READER_ROLE,)).fetchone() is None:
            conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(role))
        if password:
            # DDL takes no bind parameters: the password goes in as a quoted literal, never logged.
            conn.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(role, sql.Literal(password)))
        else:
            conn.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(role))
        for statement in (
            "ALTER ROLE {role} SET default_transaction_read_only = on",
            "ALTER ROLE {role} SET statement_timeout = '3s'",
            "GRANT CONNECT ON DATABASE {db} TO {role}",
            "GRANT USAGE ON SCHEMA public TO {role}",
            "GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role}",
            # Tables later migrations create, as this (owning) user.
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {role}",
        ):
            conn.execute(sql.SQL(statement).format(role=role, db=sql.Identifier(conn.info.dbname)))


def migrate(url: str | None = None) -> bool:
    """Migrate the database and its reader role; False, logged, when it cannot be reached."""
    try:
        with connect(url) as conn:
            if applied := apply_migrations(conn):
                logger.info("Applied database migrations %s", ", ".join(applied))
            ensure_reader(conn)
    except psycopg.Error as exc:
        logger.warning("Database not migrated (%s): run `docker compose up -d postgres`", describe(exc))
        return False
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(0 if migrate() else 1)


if __name__ == "__main__":
    main()
