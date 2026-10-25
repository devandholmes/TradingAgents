-- Real commissions per bot trade: summed from the order's fills per asset (live), or the modelled
-- fee in USDT (paper). Null on rows recorded before fees were tracked, or when one order's fills
-- mixed assets (fee_asset then lists them).
ALTER TABLE bot_trades
    ADD COLUMN fee       double precision,
    ADD COLUMN fee_asset text;
