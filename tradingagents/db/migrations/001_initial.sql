-- Schema v1: candles, strategy instances and their results, the bot, AI signals and analysis runs.
-- Applied once by tradingagents/db/migrate.py, inside a transaction, and recorded in schema_migrations.

-- Binance klines, one row per candle; open_time is the candle's open, in UTC.
CREATE TABLE candles (
    symbol    text NOT NULL,
    interval  text NOT NULL,
    open_time timestamptz NOT NULL,
    open      double precision NOT NULL,
    high      double precision NOT NULL,
    low       double precision NOT NULL,
    close     double precision NOT NULL,
    volume    double precision NOT NULL,
    PRIMARY KEY (symbol, interval, open_time)   -- also serves range reads and the latest candle per series
);

-- One strategy on one symbol and interval: id = <strategy_key>:<SYMBOL>:<interval>.
CREATE TABLE strategy_instances (
    id                 text PRIMARY KEY,
    strategy_key       text NOT NULL,
    symbol             text NOT NULL,
    interval           text NOT NULL,
    params             jsonb NOT NULL DEFAULT '{}',
    created_at         timestamptz NOT NULL DEFAULT now(),
    backtested_through timestamptz,
    live_since         timestamptz
);

CREATE TABLE paper_fills (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    instance_id    text NOT NULL REFERENCES strategy_instances (id) ON DELETE CASCADE,
    ts             timestamptz NOT NULL,
    side           text NOT NULL CHECK (side IN ('BUY', 'SELL')),
    price          double precision NOT NULL,
    qty            double precision NOT NULL,
    quote          double precision NOT NULL,
    fee            double precision NOT NULL,
    exposure_after double precision NOT NULL,
    source         text NOT NULL CHECK (source IN ('backtest', 'live'))
);
-- An instance's recent fills (detail view); also the FK's index.
CREATE INDEX paper_fills_instance_ts ON paper_fills (instance_id, ts DESC);

-- Backtest: one point per day; live: one per closed bar.
CREATE TABLE equity_points (
    instance_id text NOT NULL REFERENCES strategy_instances (id) ON DELETE CASCADE,
    ts          timestamptz NOT NULL,
    equity      double precision NOT NULL,
    exposure    double precision NOT NULL,
    source      text NOT NULL CHECK (source IN ('backtest', 'live')),
    PRIMARY KEY (instance_id, ts)   -- serves the equity-curve range reads
);

CREATE TABLE instance_state (
    instance_id    text PRIMARY KEY REFERENCES strategy_instances (id) ON DELETE CASCADE,
    exposure       double precision NOT NULL,
    cash           double precision NOT NULL,
    qty            double precision NOT NULL,
    equity         double precision NOT NULL,
    pending_target double precision,
    last_bar       timestamptz,
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- `period`, not `window`: WINDOW is a reserved word. A metric is null when undefined (no trades, flat equity).
CREATE TABLE strategy_metrics (
    instance_id      text NOT NULL REFERENCES strategy_instances (id) ON DELETE CASCADE,
    period           text NOT NULL CHECK (period IN ('7d', '30d', '90d', '1y', 'live')),
    return_pct       double precision,
    sharpe           double precision,
    sortino          double precision,
    max_drawdown_pct double precision,
    win_rate         double precision,
    exposure_pct     double precision,
    trades           integer NOT NULL DEFAULT 0,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instance_id, period)
);
-- The leaderboard reads one period across every instance.
CREATE INDEX strategy_metrics_period ON strategy_metrics (period);

CREATE TABLE analysis_runs (
    id          text PRIMARY KEY,
    symbol      text NOT NULL,
    ticker      text NOT NULL,
    date        date NOT NULL,
    status      text NOT NULL CHECK (status IN ('running', 'done', 'error', 'cancelled')),
    rating      text,
    started_at  timestamptz NOT NULL,
    finished_at timestamptz,
    duration_s  double precision,
    error       text,
    analysts    jsonb NOT NULL DEFAULT '[]'
);
-- History newest first, overall and per symbol (dashboard list, get_past_analyses).
CREATE INDEX analysis_runs_started ON analysis_runs (started_at DESC);
CREATE INDEX analysis_runs_symbol_started ON analysis_runs (symbol, started_at DESC);

-- A run's events in order; seq is the SSE event id.
CREATE TABLE analysis_events (
    run_id  text NOT NULL REFERENCES analysis_runs (id) ON DELETE CASCADE,
    seq     integer NOT NULL,
    ts      timestamptz NOT NULL,
    type    text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, seq)
);

-- The latest content of each report section of a run.
CREATE TABLE analysis_reports (
    run_id  text NOT NULL REFERENCES analysis_runs (id) ON DELETE CASCADE,
    section text NOT NULL,
    stage   text NOT NULL,
    title   text NOT NULL,
    content text NOT NULL,
    PRIMARY KEY (run_id, section)
);

-- The AI strategy's daily rating per symbol, from an analysis run.
CREATE TABLE ai_signals (
    id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol text NOT NULL,
    run_id text REFERENCES analysis_runs (id) ON DELETE SET NULL,
    rating text NOT NULL,
    ts     timestamptz NOT NULL DEFAULT now()
);
-- The latest signal per symbol.
CREATE INDEX ai_signals_symbol_ts ON ai_signals (symbol, ts DESC);
CREATE INDEX ai_signals_run ON ai_signals (run_id);   -- the FK's index

-- The single bot: which instance it follows, and the user's size.
CREATE TABLE bot_config (
    id           integer PRIMARY KEY CHECK (id = 1),
    instance_id  text REFERENCES strategy_instances (id) ON DELETE SET NULL,
    size_usdt    double precision CHECK (size_usdt > 0),
    activated_at timestamptz,
    enabled      boolean NOT NULL DEFAULT false
);
INSERT INTO bot_config (id) VALUES (1);

-- Every bot action, real or paper-mirrored, kept after its instance is gone.
CREATE TABLE bot_trades (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,   -- newest first: ORDER BY id DESC
    ts          timestamptz NOT NULL DEFAULT now(),
    instance_id text NOT NULL,
    side        text NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quote       double precision,
    qty         double precision,
    price       double precision,
    real        boolean NOT NULL,
    status      text NOT NULL,
    order_id    bigint,
    reason      text
);
-- The bot's position and PnL per instance.
CREATE INDEX bot_trades_instance ON bot_trades (instance_id, id);
