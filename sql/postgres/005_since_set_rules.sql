-- "Since set" alert rules: metric 'SinceSet', threshold = % move (magnitude)
-- from base_price, the live price stored when the rule was saved.
-- "Above or equal 5" fires at >= +5% from base_price, "Below or equal 5" at <= -5%.
-- One-shot: the alert jobs disable the rule (enabled=false) when it fires.
ALTER TABLE user_alerts ADD COLUMN IF NOT EXISTS base_price double precision;
ALTER TABLE user_alerts DROP CONSTRAINT IF EXISTS user_alerts_metric_check;
ALTER TABLE user_alerts ADD CONSTRAINT user_alerts_metric_check
    CHECK (metric IN ('Daily', 'Weekly', 'Monthly', 'Price', 'SinceSet'));
ALTER TABLE user_alerts DROP CONSTRAINT IF EXISTS user_alerts_since_set_base;
ALTER TABLE user_alerts ADD CONSTRAINT user_alerts_since_set_base
    CHECK (metric <> 'SinceSet' OR base_price > 0);

INSERT INTO schema_version (version) VALUES (5) ON CONFLICT DO NOTHING;
