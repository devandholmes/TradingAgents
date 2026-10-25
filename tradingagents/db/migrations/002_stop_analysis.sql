-- Each instance's backtest under every risk-exit preset (tradingagents.strategies.backtest.PRESETS),
-- over the periods its history covers; the engine rewrites an instance's rows once a day.
CREATE TABLE stop_analysis (
    instance_id      text NOT NULL REFERENCES strategy_instances (id) ON DELETE CASCADE,
    preset           text NOT NULL,
    period           text NOT NULL CHECK (period IN ('90d', '1y')),
    return_pct       double precision,
    sharpe           double precision,
    max_drawdown_pct double precision,
    trades           integer NOT NULL DEFAULT 0,
    win_rate         double precision,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instance_id, preset, period)   -- also serves an instance's rows and the FK
);
