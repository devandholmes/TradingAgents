-- Risk exits on the live price and the bot's execution style.
-- The three percentages (5.0 = 5 %) are optional; the monitor sells the whole bot holding when
-- the price crosses entry*(1-SL), entry*(1+TP) or peak*(1-TR). peak_price is the highest price
-- seen since the position opened. After a risk exit risk_latched keeps the bot flat until the
-- strategy's own target returns to 0; risk_reason says which exit fired.
ALTER TABLE bot_config
    ADD COLUMN stop_loss_pct   double precision CHECK (stop_loss_pct > 0 AND stop_loss_pct <= 50),
    ADD COLUMN take_profit_pct double precision CHECK (take_profit_pct > 0 AND take_profit_pct <= 50),
    ADD COLUMN trailing_pct    double precision CHECK (trailing_pct > 0 AND trailing_pct <= 50),
    ADD COLUMN execution       text NOT NULL DEFAULT 'market' CHECK (execution IN ('market', 'maker_first')),
    ADD COLUMN peak_price      double precision,
    ADD COLUMN risk_latched    boolean NOT NULL DEFAULT false,
    ADD COLUMN risk_reason     text;
