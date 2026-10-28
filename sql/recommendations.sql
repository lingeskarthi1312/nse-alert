-- New table for recommendation snapshots (additive migration, 2026-10-19).
-- MTD retention: listed in config/alert_system.json mtd_cleanup.targets.
-- The recommendations job also runs this CREATE ... IF NOT EXISTS itself.
CREATE TABLE IF NOT EXISTS nse_alerts.recommendations
(
    run_date Date,
    generated_at DateTime,
    ranking LowCardinality(String),
    rank UInt8,
    symbol String,
    name String,
    asset_class LowCardinality(String),
    category LowCardinality(String),
    exchange LowCardinality(String),
    price Float64,
    price_date Date,
    signal LowCardinality(String),
    score Float64,
    components String,
    entry_low Nullable(Float64),
    entry_high Nullable(Float64),
    stop Nullable(Float64),
    target1 Nullable(Float64),
    target2 Nullable(Float64),
    risk_reward Nullable(Float64),
    horizon String,
    confidence LowCardinality(String),
    validation LowCardinality(String),
    reasons String,
    risks String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(run_date)
ORDER BY (run_date, ranking, rank);
