-- Price-level alert rules: metric 'Price', threshold = price in rupees.
-- "Above or equal" fires when the price is >= threshold, "Below or equal" when <= threshold.
ALTER TABLE user_alerts DROP CONSTRAINT IF EXISTS user_alerts_metric_check;
ALTER TABLE user_alerts ADD CONSTRAINT user_alerts_metric_check
    CHECK (metric IN ('Daily', 'Weekly', 'Monthly', 'Price'));
ALTER TABLE user_alerts DROP CONSTRAINT IF EXISTS user_alerts_price_positive;
ALTER TABLE user_alerts ADD CONSTRAINT user_alerts_price_positive CHECK (metric <> 'Price' OR threshold > 0);

INSERT INTO schema_version (version) VALUES (2) ON CONFLICT DO NOTHING;
