-- MTD retention: NO schema migration is required. Existing tables, columns,
-- engines and keys are unchanged. Retention is applied by the alert_system
-- mtd_cleanup job (alert_system/cleanup.py). Inspected on 2026-10-19:
--
--   nse_alerts.daily_stock_prices  MergeTree  PARTITION BY toYYYYMM(trade_date)  ORDER BY (symbol, trade_date)
--   nse_alerts.daily_mtd_rankings  MergeTree  (no partition key)                ORDER BY (trade_date, category, ranking_type, symbol)
--   nse_alerts.stock_categories    ReplacingMergeTree(updated_at)               reference data - NOT an MTD dataset, never cleaned

-- 1. Read-only verification (safe to run any time) ---------------------------
SELECT table, partition, sum(rows) AS rows, min(min_date) AS min_date, max(max_date) AS max_date
FROM system.parts
WHERE active AND database = 'nse_alerts'
GROUP BY table, partition ORDER BY table, partition;

SELECT 'daily_stock_prices' AS t, min(trade_date), max(trade_date), count() FROM nse_alerts.daily_stock_prices
UNION ALL
SELECT 'daily_mtd_rankings', min(trade_date), max(trade_date), count() FROM nse_alerts.daily_mtd_rankings;

-- 2. What the cleanup job executes (shown for review; do not run by hand) -----
-- cutoff(prices)   = first day of current IST month - 10 days (baseline closes for MTD %),
--                    moved earlier if any symbol's last pre-month close is older (max 45 days)
-- cutoff(rankings) = first day of current IST month
--
-- ALTER TABLE nse_alerts.daily_stock_prices DROP PARTITION ID '<YYYYMM>';   -- months entirely < cutoff
-- DELETE FROM nse_alerts.daily_stock_prices WHERE trade_date < toDate('<cutoff>');  -- boundary month leftovers
-- DELETE FROM nse_alerts.daily_mtd_rankings WHERE trade_date < toDate('<month_start>');

-- 3. OPTIONAL (not applied): native TTL safety net for daily_mtd_rankings ----
-- Expires rows once their calendar month has ended (calendar-month rule, not
-- 30 days). TTL runs on merges in the *server* timezone (WSL is Asia/Kolkata)
-- and is not instantaneous, so the scheduled job stays the primary mechanism.
-- Do NOT add a TTL to daily_stock_prices: the MTD baseline needs pre-month rows.
--
-- ALTER TABLE nse_alerts.daily_mtd_rankings
--     MODIFY TTL toStartOfMonth(trade_date) + INTERVAL 1 MONTH;
