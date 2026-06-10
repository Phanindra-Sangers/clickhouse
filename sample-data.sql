-- Sample data for the example "Sales Overview" dashboard.
-- Run this against ClickHouse when you do NOT yet have real MinIO Parquet to load,
-- so you have something to build and verify dashboards on.
--
-- The operator provisions `default` as a Replicated database engine, so:
--   * do NOT use ON CLUSTER (DDL auto-propagates to all replicas), and
--   * do NOT pass ZooKeeper path / replica args to ReplicatedMergeTree.
--
-- Apply with:
--   kubectl exec -i -n clickhouse sample-clickhouse-0-0-0 -- clickhouse-client --multiquery < sample-data.sql
-- (use --password "<pw>" once the default user has a password)

CREATE TABLE IF NOT EXISTS default.sales (
  order_id   UInt64,
  order_date Date,
  country    LowCardinality(String),
  category   LowCardinality(String),
  quantity   UInt32,
  amount     Decimal(12,2)
) ENGINE = ReplicatedMergeTree
ORDER BY (order_date, country);

-- 200,000 deterministic synthetic rows spread across one year, 6 countries,
-- 5 categories. cityHash64 gives a stable, well-distributed spread.
INSERT INTO default.sales
SELECT
  number AS order_id,
  toDate('2025-01-01') + toIntervalDay(number % 365) AS order_date,
  arrayElement(['US','UK','India','Germany','Brazil','Canada'], 1 + (cityHash64(number) % 6))   AS country,
  arrayElement(['Electronics','Clothing','Home','Sports','Books'], 1 + (cityHash64(number+11) % 5)) AS category,
  1 + (number % 5) AS quantity,
  round(20 + (cityHash64(number+23) % 48000) / 100, 2) AS amount
FROM numbers(200000);
