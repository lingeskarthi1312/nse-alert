-- Market data moved from ClickHouse into PostgreSQL (free cloud hosting:
-- Neon Postgres + GitHub Actions + Streamlit Community Cloud, 2026-10-29).
-- Same schema/table names as the old ClickHouse database nse_alerts, so the
-- queries keep their table names. Shared reference data, no per-user rows:
-- no RLS; the dashboard role may only read it.
--
-- app_files holds the scheduler's state directory (scheduler.db + *.json
-- snapshots) between GitHub Actions runs; the dashboard reads the JSON
-- snapshots (recommendations, backtest, live quotes, health) from it.

CREATE SCHEMA IF NOT EXISTS nse_alerts;

CREATE TABLE IF NOT EXISTS nse_alerts.daily_stock_prices (
    symbol       text NOT NULL,
    trade_date   date NOT NULL,
    open_price   double precision,
    high_price   double precision,
    low_price    double precision,
    close_price  double precision,
    volume       bigint,
    recorded_at  timestamp NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
    PRIMARY KEY (symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS daily_stock_prices_date ON nse_alerts.daily_stock_prices (trade_date);

CREATE TABLE IF NOT EXISTS nse_alerts.daily_mtd_rankings (
    trade_date     date NOT NULL,
    symbol         text NOT NULL,
    category       text NOT NULL,
    close_price    double precision,
    mtd_change_pct double precision,
    ranking_type   text NOT NULL,
    recorded_at    timestamp NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
    PRIMARY KEY (trade_date, category, ranking_type, symbol)
);

CREATE TABLE IF NOT EXISTS nse_alerts.stock_categories (
    symbol     text NOT NULL,
    category   text NOT NULL,
    updated_at timestamp NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
    PRIMARY KEY (category, symbol)
);

CREATE TABLE IF NOT EXISTS nse_alerts.recommendations (
    run_date     date NOT NULL,
    generated_at timestamp NOT NULL,
    ranking      text NOT NULL,
    rank         smallint NOT NULL,
    symbol       text NOT NULL,
    name         text NOT NULL,
    asset_class  text NOT NULL,
    category     text NOT NULL,
    exchange     text NOT NULL,
    price        double precision NOT NULL,
    price_date   date,
    signal       text NOT NULL,
    score        double precision NOT NULL,
    components   text NOT NULL,
    entry_low    double precision,
    entry_high   double precision,
    stop         double precision,
    target1      double precision,
    target2      double precision,
    risk_reward  double precision,
    horizon      text NOT NULL,
    confidence   text NOT NULL,
    validation   text NOT NULL,
    reasons      text NOT NULL,
    risks        text NOT NULL,
    PRIMARY KEY (run_date, ranking, rank)
);

CREATE TABLE IF NOT EXISTS app_files (
    name       text PRIMARY KEY CHECK (name ~ '^[A-Za-z0-9_.-]+$'),
    data       bytea NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

GRANT USAGE ON SCHEMA nse_alerts TO nse_app, nse_service;
GRANT SELECT ON ALL TABLES IN SCHEMA nse_alerts TO nse_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nse_alerts TO nse_service;
GRANT SELECT ON app_files TO nse_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON app_files TO nse_service;

INSERT INTO schema_version (version) VALUES (6) ON CONFLICT DO NOTHING;
