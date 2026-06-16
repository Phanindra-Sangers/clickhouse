# StarRocks vs ClickHouse

A detailed comparison for the Trino replacement decision, written against the deployment in this repo. Both are open-source, columnar, MPP analytical databases that can replace Trino as the query layer over a MinIO/Parquet lake. They differ most in architecture, join performance, data-lake integration, and operational model.

Bottom line up front: choose **ClickHouse** for raw single-table scan/aggregation speed, very high ingest rates, and the lowest-footprint deployment. Choose **StarRocks** for complex multi-table star-schema joins, strong data-lake/lakehouse querying (Iceberg, Hudi, Delta, Hive) without ingestion, and a MySQL-compatible interface that drops into existing BI tooling. For a Trino replacement that keeps querying the lake in place, StarRocks is the closer functional match; for a fast serving layer on materialized data, ClickHouse is leaner.

## At a glance

| Dimension | ClickHouse | StarRocks |
| --- | --- | --- |
| Model | Columnar MPP database | Columnar MPP database |
| License | Apache 2.0 | Apache 2.0 |
| Wire protocol | Native TCP (9000), HTTP (8123) | MySQL protocol (9030) |
| Cluster roles | Server nodes + Keeper (coordination) | FE (frontend) + BE (backend), optional CN |
| Storage engine | MergeTree family | Native columnar; shared-nothing or shared-data |
| Best at | Single-table scans, aggregations, very high ingest | Multi-table joins, star-schema, lakehouse queries |
| Joins | Works, weaker on large distributed joins historically | Cost-based optimizer, strong distributed joins |
| Data-lake querying | `s3()`, `Iceberg`, `DeltaLake`, `Hudi` table functions/engines | Catalogs for Iceberg, Hudi, Delta, Hive, JDBC |
| Updates/deletes | Eventually via mutations / ReplacingMergeTree | Primary-key model with real-time upserts/deletes |
| Operator | ClickHouse/clickhouse-operator (official) | starrocks-kubernetes-operator (official) |
| Coordination | ClickHouse Keeper (built in) | FE nodes (built-in Berkeley DB JE / BDBJE quorum) |
| Footprint | Lower (no JVM; C++) | Higher (FE is Java/JVM; BE is C++) |

## Architecture

ClickHouse is a single binary written in C++. Every server node both stores data and executes queries. Coordination for replicated tables is handled by ClickHouse Keeper, a built-in ZooKeeper-compatible service. There is no separate frontend or coordinator tier. This makes the cluster simple and light, but query planning is comparatively simple, which historically made large multi-table joins harder to optimize.

StarRocks splits responsibilities into two tiers. The Frontend (FE) is a Java service that holds metadata, parses SQL, and runs a cost-based optimizer to plan queries. The Backend (BE) is a C++ service that stores data and executes the plan fragments. FE nodes form their own metadata quorum (using embedded BDBJE), so there is no external ZooKeeper or Keeper. StarRocks also has a shared-data mode where BEs become stateless Compute Nodes (CN) and data lives in object storage like S3/MinIO, separating storage from compute much like Trino did, but with a fast local cache.

The practical consequence: StarRocks's cost-based optimizer and dedicated planning tier make it stronger on complex joins across a star schema, which is exactly the shape of many BI workloads. ClickHouse wins when the query is a big scan-and-aggregate over one wide table, where its storage engine and vectorized execution are extremely fast and there is less planning overhead.

## SQL and client compatibility

StarRocks speaks the MySQL wire protocol on port 9030. Any MySQL client, JDBC driver, or BI tool that connects to MySQL connects to StarRocks unchanged, and its SQL dialect is close to MySQL/standard SQL. This lowers migration friction for tools and teams already on MySQL-style connectivity.

ClickHouse uses its own native protocol (9000) and an HTTP interface (8123), with its own SQL dialect that is powerful but less standard (rich array/aggregate functions, `FINAL`, specific JOIN semantics). It also offers a MySQL-protocol port and a Postgres-protocol port for compatibility, but the native dialect is where its strengths are.

For Superset specifically, both work: ClickHouse via the `clickhouse-connect` driver (`clickhousedb://`), StarRocks via the MySQL dialect (`mysql://` / `starrocks://`).

## Data lake and the Trino replacement angle

This is the most relevant axis for replacing Trino over a Hadoop/MinIO/Parquet lake.

StarRocks was designed with external catalogs as a first-class feature. You register an Iceberg, Hudi, Delta Lake, Hive, or JDBC catalog, and StarRocks queries those tables in place with its optimizer and local caching, no ingestion required. This makes it a near drop-in for Trino's federated, query-the-lake role, often with better performance because of the cache and vectorized BE.

ClickHouse queries lake files too, through the `s3()` table function and the `Iceberg`, `DeltaLake`, and `Hudi` table engines, with no Hive Metastore needed. It is excellent at reading Parquet directly, but its lakehouse catalog integration and query-in-place optimizer are less mature than StarRocks's external-catalog model. ClickHouse shines once data is loaded into native MergeTree tables.

So: if the goal is to keep the lake as the source of truth and query it in place exactly like Trino, StarRocks is the closer fit. If the goal is to materialize hot datasets into a blazing-fast serving layer, ClickHouse is leaner and faster per node.

### Does StarRocks use Hive Metastore?

It depends on what you query, and this is a point in StarRocks's favor for a migration off Trino.

For its **own native tables**, no. The FE holds the catalog internally, the same as ClickHouse. No Hive Metastore is involved.

For **querying your existing lake**, it can, and that is useful. StarRocks has external catalogs: you register a Hive catalog pointing at your existing Hive Metastore URI and it queries those Hive tables in place, so it immediately sees every table the HMS already knows. For Iceberg, Delta, and Hudi it supports several catalog backends (Hive Metastore, AWS Glue, a REST catalog, or filesystem). It also has a `FILES()` table function, the analog of ClickHouse's `s3()`, to read Parquet directly by path with no metastore at all.

The nuance versus ClickHouse: StarRocks does not require Hive Metastore, but it can reuse one if you have it. That makes it a smoother drop-in for Trino during a migration where Hive tables and the HMS already exist. ClickHouse deliberately avoids the metastore by reading files directly and inferring schema. Both let you retire HMS eventually; StarRocks lets you keep it through the transition.

## Updates, deletes, and consistency

StarRocks has a Primary Key table model supporting real-time upserts and deletes with good read performance, plus other models (Duplicate, Aggregate, Unique) for different patterns. This suits mutable, frequently updated data and CDC ingestion.

ClickHouse is append-optimized. Updates and deletes exist as asynchronous mutations, and dedup/upsert patterns use `ReplacingMergeTree` or `CollapsingMergeTree` with eventual merge semantics, often querying with `FINAL`. It is outstanding for immutable event/log/time-series data, less natural for heavy in-place updates.

## Ingestion

ClickHouse sustains very high insert throughput and has broad native integrations (Kafka engine, materialized views, many table functions). It is a common choice for high-volume observability and event pipelines.

StarRocks offers Stream Load, Broker Load, Routine Load (Kafka), and pipe-style loading, plus the external-catalog path where you do not ingest at all. Strong, though ClickHouse generally leads on raw single-stream ingest rate.

## Operational model and footprint

ClickHouse is lighter. C++ only, one process type, Keeper is small, and on Kubernetes the official operator runs server pods plus a 3-node Keeper. Lower memory floor and fewer moving parts.

StarRocks is heavier. The FE is a JVM service that needs meaningful heap, and you run at least FE plus BE as separate tiers. More memory and more components, in exchange for the optimizer and lakehouse features. In shared-data mode you also operate object storage and a cache tier.

## When to pick which

Pick ClickHouse when the workload is single-table or denormalized scans and aggregations, very high ingest, time-series/observability/event data, and you want the smallest, simplest, lowest-cost cluster. It is the better fast serving layer on materialized data.

Pick StarRocks when the workload is complex multi-table joins over a star/snowflake schema, you want to query the lake in place across Iceberg/Hudi/Delta/Hive like Trino did, you need real-time upserts/deletes on a primary key, or you value MySQL-protocol compatibility for existing tooling.

For this project's Trino replacement: if you keep Hadoop writing Parquet to MinIO and want to query it in place with minimal change, StarRocks's external catalogs are the closer match to Trino. If you are willing to materialize the hot dashboard datasets into a native serving layer, ClickHouse gives the fastest per-node dashboards at the lowest footprint. A valid hybrid is StarRocks for federated lake queries and ClickHouse for the hot serving tier, but running both is more operational surface than most teams want; pick one unless the workloads are genuinely distinct.

## Demo: what to showcase in StarRocks

A good demo plays to where StarRocks differs from ClickHouse and from Trino, not just "it runs a query." All of these run on the cluster in this repo via the MySQL port.

- **MySQL-protocol access.** Connect with any `mysql` client or BI tool on port 9030, no special driver. The "it works with our existing tooling" moment.
- **A star-schema join.** Create a fact table plus a few dimension tables and run a multi-table join with aggregation. This is StarRocks's headline strength (cost-based optimizer) and the natural shape of BI queries, so it is the most honest thing to highlight against ClickHouse.
- **Real-time upserts.** Use a Primary Key table and show `INSERT`/`UPDATE`/`DELETE` reflecting immediately. ClickHouse is append-optimized and cannot do this cleanly, so it is a genuine differentiator for mutable data or CDC.
- **Materialized views with automatic query rewrite.** Define an MV, then query the base table and show StarRocks transparently using the MV to accelerate it.
- **Query the lake in place** via an external catalog (Hive/Iceberg) or the `FILES()` function, then join lake data with an internal table in one query. The direct Trino-replacement showcase.
- **Superset on StarRocks.** Point Superset at it over MySQL and rebuild the same dashboard you have on ClickHouse for a true side-by-side.

Example: a star-schema join and a primary-key upsert, both runnable now.

```sql
-- star schema: fact + dimension, joined and aggregated
CREATE DATABASE IF NOT EXISTS demo;
CREATE TABLE demo.dim_country (country VARCHAR(32), region VARCHAR(32))
  PRIMARY KEY(country) DISTRIBUTED BY HASH(country) PROPERTIES('replication_num'='1');
INSERT INTO demo.dim_country VALUES ('US','NA'),('India','APAC'),('UK','EMEA');
SELECT d.region, sum(s.amount) revenue
FROM demo.sales s JOIN demo.dim_country d ON s.country = d.country
GROUP BY d.region ORDER BY revenue DESC;

-- primary-key upsert: same key overwrites in place (ClickHouse cannot do this cleanly)
INSERT INTO demo.dim_country VALUES ('US','North America');   -- updates the US row
SELECT * FROM demo.dim_country WHERE country = 'US';
```

## Performance testing: ClickHouse vs StarRocks vs Trino

There is no universal winner; query shape decides it, and a fair benchmark on your own workload is the only thing that settles the choice. How to do it properly:

**Use a standard, query-shaped benchmark, not a vibe test.**
- ClickBench: one wide table, scan-and-aggregate. Tends to favor ClickHouse.
- TPC-H / TPC-DS: many-table joins. Tends to favor StarRocks and Trino.
- SSB (Star Schema Benchmark): a fact table with a few dimensions, the closest to real BI dashboards and the fairest middle ground for this use case. Lead with SSB.
- Best of all: replay your own real queries on your own data.

**Hold everything else equal.** Same dataset, same cluster size, same data format for lake tests (same Parquet/Iceberg), and measure cold and warm caches separately. Comparing a tuned engine against a default one is the most common way these benchmarks mislead.

**Measure what matches BI reality.**
- Latency percentiles (p50/p95/p99), not just an average.
- Throughput under concurrency: run 10, 50, 100 concurrent clients, since dashboards mean many users at once. This is often where engines separate.
- Ingest rate if the pipeline writes continuously.
- Resource use (CPU, memory) at a given latency, since cost matters.

**Likely outcome, as a hypothesis to verify.** ClickHouse usually leads on single-table aggregations and raw ingest; StarRocks usually leads on multi-table joins and concurrent BI; Trino, the engine being replaced, is typically the slowest for interactive latency but the most flexible for federated batch queries. For a Superset-over-a-lake workload, both ClickHouse and StarRocks should beat Trino on dashboard responsiveness, and the ClickHouse-vs-StarRocks choice reduces to whether your queries are wide single-table scans (ClickHouse) or star-schema joins (StarRocks).

**Caveat for the cluster in this repo.** The single-node kind cluster is fine for a functional demo but useless for performance numbers: StarRocks, ClickHouse, Superset, and others share one node's CPU, memory, and disk, so any latency comparison here is noise. Real benchmarking needs representative, isolated hardware with production-sized clusters.

## This repo

ClickHouse is deployed and documented in [README.md](README.md). StarRocks is deployed for side-by-side evaluation via [starrocks-values.yaml](starrocks-values.yaml) (sized down for a single-node kind cluster). The deployed version is StarRocks 4.1.1 (chart `kube-starrocks` 1.11.5), with 1 FE and 1 BE in the `starrocks` namespace.

### Install

```bash
helm repo add starrocks https://starrocks.github.io/starrocks-kubernetes-operator
helm repo update starrocks

helm install starrocks starrocks/kube-starrocks \
  -n starrocks --create-namespace \
  -f starrocks-values.yaml \
  --version 1.11.5 --timeout 300s
```

The operator brings up the FE first, then creates the BE once the FE is ready. Wait for both:

```bash
kubectl get pods -n starrocks      # kube-starrocks-fe-0 and kube-starrocks-be-0 both 1/1 Running
```

### Production deployment

For a production layout use [starrocks-prod-values.yaml](starrocks-prod-values.yaml): 3 FE (metadata quorum), 3 BE (so the default `replication_num=3` applies), one of each per node via hard anti-affinity, SSD storage with a `Retain` StorageClass, and a root password set on first install from a Secret. Before installing, create the password Secret (the key must be `password`) and set `storageClassName` to your SSD class:

```bash
kubectl create namespace starrocks
kubectl create secret generic starrocks-root-password \
  -n starrocks --from-literal=password='<choose-a-strong-password>'   # store in 1Password

helm install starrocks starrocks/kube-starrocks \
  -n starrocks -f starrocks-prod-values.yaml --version 1.11.5 --timeout 600s
```

Same fault-tolerance rule as the ClickHouse layout: the per-node anti-affinity is hard, so you need at least 3 nodes or the FE/BE pods stay `Pending`. Size FE memory above its JVM `-Xmx` and BE storage to your dataset.

### Accessing StarRocks

Forward the FE web UI (8030) and the MySQL query port (9030):

```bash
kubectl -n starrocks port-forward svc/kube-starrocks-fe-service 8030:8030 9030:9030
```

Web UI: open http://localhost:8030 and log in with user `root`, empty password (HTTP basic auth). It shows cluster status, sessions, and query profiles. It is a system/admin console, not a BI dashboard tool like Superset.

SQL over the MySQL protocol (any MySQL client or BI tool):

```bash
mysql -h127.0.0.1 -P9030 -uroot
```

Or run a query straight inside the FE pod:

```bash
kubectl exec -n starrocks kube-starrocks-fe-0 -- \
  mysql -h127.0.0.1 -P9030 -uroot -e "SHOW BACKENDS\G"
```

### Single-BE caveat

With one BE, set `replication_num=1` when creating tables, or DDL fails with "replication num should be less than or equal to the number of available backends" (the default is 3):

```sql
CREATE TABLE demo.sales ( ... )
DUPLICATE KEY(order_id) DISTRIBUTED BY HASH(order_id) BUCKETS 4
PROPERTIES('replication_num'='1');
```

A real cluster runs 3 FE and 3+ BE, where the default replication of 3 applies and you would not set this.

### Connecting Superset to StarRocks

StarRocks speaks MySQL, so add it in Superset with the MySQL dialect rather than the `clickhousedb` driver:

```
mysql://root:@kube-starrocks-fe-service.starrocks.svc.cluster.local:9030/<database>
```

(or the `starrocks://` dialect if the `starrocks` SQLAlchemy package is installed). This lets you build the same kind of dashboards against StarRocks for a true side-by-side with ClickHouse.
