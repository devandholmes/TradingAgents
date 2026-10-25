"""/api/health against the tests' own Postgres: the engine's last write and its lag.

Its own module: the app's lifespan holds globals, so its TestClient must not overlap
test_reliability's (no database). Skipped when Postgres is not running.
"""

import psycopg
import pytest

from tradingagents import db
from tradingagents.db import testing
from tradingagents.web.app import app


@pytest.fixture(scope="module")
def db_http():
    url = testing.require_test_database()
    with pytest.MonkeyPatch.context() as env:
        env.setenv(db.URL_ENV, url)
        # The migration updates the cluster-wide reader role: with the real password, or it loses its login.
        env.setenv(db.READONLY_PASSWORD_ENV, testing.readonly_password_for_tests() or "")
        env.setenv("TRADINGAGENTS_AI_STRATEGY", "off")
        with testing.selector_client(app, base_url="http://127.0.0.1:8000") as client:
            yield client, url


@pytest.mark.unit
def test_health_reads_the_engines_last_write(db_http):
    client, url = db_http
    own = "health_probe:BTCUSDT:1h"
    with psycopg.connect(url, autocommit=True, **db.SESSION) as conn:
        conn.execute("DELETE FROM strategy_instances WHERE id = %s", (own,))
        conn.execute("INSERT INTO strategy_instances (id, strategy_key, symbol, interval, params)"
                     " VALUES (%s, 'health_probe', 'BTCUSDT', '1h', '{}')", (own,))
        conn.execute("INSERT INTO instance_state (instance_id, exposure, cash, qty, equity, last_bar, updated_at)"
                     " VALUES (%s, 0, 1000, 0, 1000, now(), now())", (own,))
        try:
            health = client.get("/api/health").json()
        finally:
            conn.execute("DELETE FROM strategy_instances WHERE id = %s", (own,))
    assert health["db"] is True
    assert health["engine"]["last_bar_at"] is not None and 0 <= health["engine"]["lag_s"] < 60
