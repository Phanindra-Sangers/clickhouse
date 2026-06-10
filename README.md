# ClickHouse on Kubernetes (Official Operator)

This repository deploys ClickHouse and ClickHouse Keeper on Kubernetes using the **official ClickHouse operator** from [github.com/ClickHouse/clickhouse-operator](https://github.com/ClickHouse/clickhouse-operator), released by ClickHouse Inc. This is not the third-party Altinity operator. The operator uses CRDs in the `clickhouse.com/v1alpha1` API group and relies on built-in ClickHouse Keeper for coordination, so no separate ZooKeeper is required.

## Contents

```
clickhouse-cluster.yaml        Minimal dev cluster (1 shard x 2 replicas, 3 Keepers)
clickhouse-prod-3nodes.yaml    Production layout for 3 nodes (1 shard x 3 replicas)
clickhouse-prod-4nodes.yaml    Production layout for 4 nodes (2 shards x 2 replicas)
superset-values.yaml           Helm values for Superset wired to ClickHouse
sample-data.sql                Sample sales table + 200k rows for the example dashboard
build_dashboard.py             Idempotent script that builds the example dashboard
README.md                      This guide
```

## Components and versions

The deployment in this repo was validated with the following versions. Pin these in production rather than tracking `latest`.

| Component | Version | Source |
| --- | --- | --- |
| ClickHouse operator (Helm chart) | `0.0.5` | `oci://ghcr.io/clickhouse/clickhouse-operator-helm` |
| cert-manager | `v1.16.3` | `github.com/cert-manager/cert-manager` |
| ClickHouse server / keeper | `25.3` (LTS, recommended pin) | `docker.io/clickhouse/clickhouse-server`, `docker.io/clickhouse/clickhouse-keeper` |
| CRD API group | `clickhouse.com/v1alpha1` | Kinds: `ClickHouseCluster`, `KeeperCluster` |

## Prerequisites

- A Kubernetes cluster v1.28.0 or newer with 3 or 4 worker nodes.
- `kubectl` v1.28.0+ and `helm` v3.8+ (OCI support).
- cert-manager installed in the cluster. The operator uses it to issue its admission webhook certificates and, optionally, ClickHouse server TLS certificates.
- A real, SSD-backed StorageClass with reclaim policy `Retain`. Do not use the local-path / hostPath classes that ship with kind or minikube for production data.

---

## Topology recommendation

Lead with the layout, then the reasoning.

**3 nodes:** 3 Keepers and ClickHouse as **1 shard x 3 replicas**. Every node holds a complete copy of the data, so any single node failure costs zero data and requires no rebalancing. Use [clickhouse-prod-3nodes.yaml](clickhouse-prod-3nodes.yaml).

**4 nodes:** 3 Keepers and ClickHouse as **2 shards x 2 replicas**. This doubles usable storage and write throughput versus the 3-node layout, and each shard keeps a replica on a separate node for HA. Prefer this over 4 replicas x 1 shard unless your dataset fits comfortably on one node and the workload is read-heavy. Use [clickhouse-prod-4nodes.yaml](clickhouse-prod-4nodes.yaml).

Keeper stays at **3 in both cases.** Quorum requires an odd count, and 3 already tolerates one failure. A 4th Keeper does not improve fault tolerance and adds Raft commit latency.

---

## Installation guide

### 1. Install cert-manager

```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.16.3/cert-manager.yaml

kubectl wait --for=condition=Available --timeout=120s \
  deployment/cert-manager deployment/cert-manager-webhook deployment/cert-manager-cainjector \
  -n cert-manager
```

### 2. Install the operator

```bash
helm install clickhouse-operator oci://ghcr.io/clickhouse/clickhouse-operator-helm \
  --version 0.0.5 \
  --create-namespace \
  -n clickhouse-operator-system \
  --wait --timeout 180s
```

Verify the controller is running and the CRDs are registered:

```bash
kubectl get pods -n clickhouse-operator-system
kubectl get crd | grep clickhouse.com
```

### 3. Create the namespace and default-user password Secret

The ClickHouse `default` user must have a password in production. Source it from a Secret as a SHA-256 hash; never inline a plaintext password in the manifest.

```bash
kubectl create namespace clickhouse

PW='REPLACE-with-a-strong-password'
HASH=$(printf '%s' "$PW" | sha256sum | awk '{print $1}')
kubectl create secret generic clickhouse-default-user \
  -n clickhouse \
  --from-literal=password-sha256="$HASH"
```

Store the plaintext password in 1Password, not in this repo or your shell history. The manifests reference this Secret via `spec.settings.defaultUserPassword.secret`.

### 4. Edit the manifest, then deploy

Open the manifest for your node count and change at minimum:

- `storageClassName` on both `dataVolumeClaimSpec` blocks to your real SSD class.
- `containerTemplate.image.tag` on both kinds to a verified, pinned tag.
- `dataVolumeClaimSpec.resources.requests.storage` to your sizing.

Validate against the live CRDs before applying:

```bash
kubectl apply --dry-run=server -f clickhouse-prod-3nodes.yaml    # or -4nodes
```

Then apply:

```bash
kubectl apply -f clickhouse-prod-3nodes.yaml
```

### 5. Verify

```bash
kubectl get keepercluster,clickhousecluster -n clickhouse
kubectl get pods -n clickhouse -o wide          # confirm one pod per node
kubectl exec -n clickhouse <a-clickhouse-pod> -- clickhouse-client \
  --password "$PW" -q "SELECT host_name, host_address FROM system.clusters FORMAT PrettyCompact"
```

Both custom resources should report `READY True`. The `-o wide` output confirms the anti-affinity worked: no two ClickHouse pods share a node.

---

## Fault tolerance: how anti-affinity works

You do not write affinity blocks by hand. Setting `spec.podTemplate.nodeHostnameKey: kubernetes.io/hostname` makes the operator generate a **hard** pod anti-affinity rule on every pod:

```yaml
podAntiAffinity:
  requiredDuringSchedulingIgnoredDuringExecution:
  - labelSelector:
      matchLabels:
        app: <cluster>-clickhouse
        clickhouse.com/role: clickhouse-server
    topologyKey: kubernetes.io/hostname
```

`required...` is enforced, not best-effort: a replica cannot be scheduled onto a node that already runs a replica of the same cluster. The result is one replica per node, so losing a node costs at most one replica.

The hard consequence: **you must have at least as many nodes as replicas.** With fewer nodes, the extra pods stay `Pending` indefinitely with the scheduler message `node(s) didn't match pod anti-affinity rules`. This is intentional; the operator refuses to co-locate replicas rather than silently defeating your fault tolerance. The 3-node and 4-node layouts in this repo are sized so every pod gets its own node.

`spec.podTemplate.topologyZoneKey: topology.kubernetes.io/zone` adds a **soft** spread across availability zones on top of the hard per-node rule. The operator tries to balance replicas across AZs but will not block scheduling when a zone is full. Per-node stays hard, per-zone stays best-effort. This is the correct combination for cloud multi-AZ clusters. Keeper uses the same two fields independently.

---

## Production best practices

These are already reflected in the prod manifests. The reasoning matters for when you tune them.

**Do not set a CPU limit on ClickHouse.** ClickHouse parallelizes a single query across cores; a CPU limit triggers CFS throttling that sharply degrades query latency. Set a CPU request for scheduling and a memory limit to bound the OOM blast radius, but leave CPU unlimited. The manifests follow this. Keeper gets a small memory cap because it is lightweight.

**Use Retain storage and size for merges.** ClickHouse rewrites data during background merges, which temporarily needs extra disk. Provision roughly 30 percent headroom over your steady-state data size. A `Retain` reclaim policy ensures deleting a PVC does not destroy data.

**Keep one pod of disruption budget.** `podDisruptionBudget.maxUnavailable: 1` lets node drains and rolling upgrades proceed one pod at a time while a majority keeps serving. With 3 replicas this maintains quorum; with 2 replicas per shard it keeps one replica per shard up.

**Pin image tags and constrain upgrade proposals.** Set explicit `image.tag` values and keep server and Keeper on compatible versions. `spec.upgradeChannel: lts` limits the operator to proposing LTS major upgrades; set `stable` for the latest stable line or a specific `major.minor` to freeze.

**Enable TLS for any non-trivial environment.** cert-manager is already a prerequisite. Set `spec.settings.tls.enabled: true` and reference a server certificate Secret via `tls.serverCertSecret.name`. Set `tls.required: true` to refuse plaintext connections entirely. Wire up a cert-manager Issuer and Certificate that writes to that Secret.

**Keep Keeper on fast, low-latency disk.** Keeper fsyncs its Raft log on every commit. Even though its volume is small, slow disk directly raises write latency for the whole ClickHouse cluster. Use the same SSD class, not bulk HDD.

**Plan backups separately.** The operator manages cluster lifecycle, not backups. Use `clickhouse-backup` or `BACKUP ... TO Disk/S3` to an object store on a schedule. Replication protects against node loss, not against accidental `DROP` or data corruption.

**Log in JSON at `information` level.** Both manifests set `logger.jsonLogs: true` and `logger.level: information` so logs are parseable by your aggregation stack and not flooded with `trace` output (the operator default is `trace`).

---

## Day-2 operations

**Scale replicas or shards** by editing `spec.replicas` / `spec.shards` and re-applying. Remember the anti-affinity constraint: raising `replicas` requires that many nodes, or new pods stay `Pending`. Add nodes first.

**Connect from inside the cluster** via the headless service:

```bash
kubectl -n clickhouse port-forward svc/<cluster>-clickhouse-headless 8123:8123 9000:9000
# then: clickhouse-client --host 127.0.0.1 --password <pw>   (native, 9000)
#       curl http://127.0.0.1:8123/                          (HTTP)
```

**Check cluster topology and replication health:**

```bash
kubectl exec -n clickhouse <pod> -- clickhouse-client --password <pw> -q \
  "SELECT * FROM system.clusters FORMAT Vertical"
kubectl exec -n clickhouse <pod> -- clickhouse-client --password <pw> -q \
  "SELECT database, table, is_readonly, absolute_delay FROM system.replicas"
```

**Rotate the default-user password** by updating the Secret and re-applying (or letting the operator reconcile). Generate a new SHA-256 hash and `kubectl create secret ... --dry-run=client -o yaml | kubectl apply -f -`.

---

## Uninstall

```bash
kubectl delete -f clickhouse-prod-3nodes.yaml          # removes the cluster CRs
# PVCs are retained by design; delete them explicitly to free storage:
kubectl delete pvc -n clickhouse -l app.kubernetes.io/part-of=clickhouse-prod
helm uninstall clickhouse-operator -n clickhouse-operator-system
```

Deleting the cluster CRs does not delete PVCs, so data survives a recreate. Delete PVCs only when you intend to destroy the data.

---

## Architecture: replacing Trino with ClickHouse

The previous stack was Superset querying Trino, with Trino reading table metadata from Hive Metastore and data files from MinIO. The new stack is Superset querying ClickHouse directly. **Hive Metastore is no longer required**, and MinIO changes from being the primary data store to an optional backup and cold-tier target.

```
Before:  Superset -> Trino -> Hive Metastore (catalog)
                          \-> MinIO (Parquet/ORC data files)

After:   Superset -> ClickHouse (own catalog + data) + Keeper (replication coord)
                          \-> MinIO (optional: backups, tiered storage, lake queries)
```

The reason is structural. Trino is a stateless compute engine with no storage of its own, so it has to ask Hive Metastore what tables exist, what their schemas are, and where their files live in object storage. ClickHouse is both the storage engine and the query engine. It keeps its own table catalog internally, including schemas, types, partitioning, and the part-to-file mapping. The Keeper cluster deployed here handles replication coordination for `ReplicatedMergeTree` tables. There is nothing left for an external metastore to do.

When you would still keep each component:

Hive Metastore can be retired entirely once data lives in native ClickHouse tables. The only case for keeping it is a transition period where you want ClickHouse to read existing Hive-cataloged tables in place through its `Hive` table engine, which can talk to HMS. That is a migration crutch, not a target state.

MinIO is worth keeping even after the migration. Use it as the backup target for `clickhouse-backup`, as a cold tier where infrequently accessed parts live on object storage via a storage policy while hot data stays on local SSD, and as a source for ingestion or occasional query-in-place over lake data.

The recommended end state is to ingest your datasets into native ClickHouse `ReplicatedMergeTree` tables, retire Hive Metastore, and keep MinIO only for backups and cold storage. Query-in-place over MinIO is worth keeping only for large, rarely queried lake data that you do not want to load.

---

## Loading Parquet from MinIO into ClickHouse

ClickHouse reads Parquet directly from any S3-compatible store, including MinIO, with the `s3()` table function. No Hive Metastore and no external catalog are involved. You give it an endpoint, credentials, a path, and the format.

This is the only place MinIO appears. It is plain ClickHouse SQL, run with `clickhouse-client`. Neither [build_dashboard.py](build_dashboard.py) nor [superset-values.yaml](superset-values.yaml) has any MinIO configuration; the Python script only builds the Superset dashboard and connects to ClickHouse, and Superset never talks to MinIO directly.

The SQL below uses four placeholders. Replace all of them with your real values before running:

| Placeholder | Replace with | Example |
| --- | --- | --- |
| `https://minio.../warehouse/sales/*.parquet` | Your MinIO endpoint, bucket, and object path or glob. Use `http://` if TLS is not enabled. | `http://minio.minio.svc.cluster.local:9000/lake/sales/*.parquet` |
| `<ACCESS_KEY>` | MinIO access key | from your MinIO credentials Secret |
| `<SECRET_KEY>` | MinIO secret key | from your MinIO credentials Secret |
| `'Parquet'` | The file format | `Parquet`, `ORC`, `CSVWithNames`, etc. |

Keep the access and secret keys out of SQL you commit. Either pass them only at the interactive prompt, or define a named credential in the server config with a `<named_collections>` entry and reference it, so the keys live in a Secret-mounted config file instead of inline. For ad hoc reads you can query the files in place:

```sql
SELECT *
FROM s3(
  'https://minio.minio.svc.cluster.local:9000/warehouse/sales/*.parquet',
  '<ACCESS_KEY>', '<SECRET_KEY>', 'Parquet'
)
LIMIT 10;
```

For the recommended path, copy the lake data into a native replicated table once and query that. The operator provisions the `default` database as a **Replicated database engine**, which has two consequences that differ from a vanilla ClickHouse install:

First, do not use `ON CLUSTER` for DDL. The Replicated database propagates `CREATE`, `ALTER`, and `DROP` to every replica automatically. Second, do not pass explicit ZooKeeper path or replica name arguments to `ReplicatedMergeTree`. The Replicated database manages those itself, and passing them fails with `BAD_ARGUMENTS`. Declare the engine with no arguments.

```sql
-- Runs on one replica; the Replicated database propagates it to all replicas.
CREATE TABLE default.sales (
  order_id   UInt64,
  order_date Date,
  country    LowCardinality(String),
  category   LowCardinality(String),
  quantity   UInt32,
  amount     Decimal(12,2)
) ENGINE = ReplicatedMergeTree
ORDER BY (order_date, country);

-- Bulk load from MinIO Parquet into the native table.
INSERT INTO default.sales
SELECT order_id, order_date, country, category, quantity, amount
FROM s3(
  'https://minio.minio.svc.cluster.local:9000/warehouse/sales/*.parquet',
  '<ACCESS_KEY>', '<SECRET_KEY>', 'Parquet'
);
```

Data inserted on one replica replicates to the others through Keeper. This matters for Superset, which connects through the load-balancing headless service and may land on either replica. With a replicated table, every replica returns identical results.

For repeating loads, an `S3` table engine pointed at the bucket gives you a reusable external table you can `INSERT INTO ... SELECT` from on a schedule. For Iceberg or Delta lake formats, use the native `Iceberg`, `DeltaLake`, or `Hudi` engines instead of `s3()`, which understand the table format's manifests without a Hive Metastore.

---

## Superset: install, connect, and build dashboards

Superset is installed in its own namespace via the official Apache Helm chart and connected to ClickHouse with the official `clickhouse-connect` driver. The values are in [superset-values.yaml](superset-values.yaml).

### Install

```bash
helm repo add superset https://apache.github.io/superset
helm repo update superset

helm install superset superset/superset \
  -n superset --create-namespace \
  -f superset-values.yaml \
  --version 0.15.5 --timeout 420s
```

Two non-obvious points are baked into the values file. The Superset 5.0 image runs from a virtualenv at `/app/.venv` that has no `pip` of its own, so drivers must be installed with `uv pip install --python /app/.venv/bin/python` in the bootstrap script; a plain `pip install` lands in the system Python, off the import path, and the pods crash with `ModuleNotFoundError: No module named 'psycopg2'`. The Celery worker also defaults to one process per node CPU, which gets OOMKilled under a small memory limit, so the worker is pinned to `--concurrency=2` with a 2Gi limit.

Before any shared use, replace the placeholder `SUPERSET_SECRET_KEY` with a managed value from 1Password and change the `admin/admin` bootstrap credentials. Both are flagged inline in the values file.

### Connect to ClickHouse

The connection uses the `clickhousedb://` SQLAlchemy dialect from `clickhouse-connect`. Register it from the CLI:

```bash
POD=$(kubectl get pod -n superset -l app=superset -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n superset $POD -- superset set_database_uri \
  -d ClickHouse \
  -u "clickhousedb://default:<password>@sample-clickhouse-headless.clickhouse.svc.cluster.local:8123/default"
```

The dev cluster's `default` user has no password, so the URI leaves it empty. For production, take the password from the `clickhouse-default-user` Secret and point the host at the production service. You can also add the connection in the UI under Settings, Database Connections, using the same URI.

### Access the UI

```bash
kubectl -n superset port-forward svc/superset 8088:8088
# http://localhost:8088  (admin / admin)
```

### Build the example "Sales Overview" dashboard (reproduce in your own cluster)

This repo ships a working example: a dashboard named **Sales Overview** with five charts (Total Revenue, Total Orders, Revenue by Day, Revenue by Category, Revenue by Country), all querying ClickHouse live through the `clickhousedb` driver. Reproduce it end to end with the steps below. Do every step in order; nothing else is required.

#### Step 1: Get a table to chart

Either load the real data from MinIO Parquet (see "Loading Parquet from MinIO into ClickHouse" above), or load the bundled synthetic data for testing. The example expects a `default.sales` table.

To load the synthetic data, apply [sample-data.sql](sample-data.sql) by piping it into `clickhouse-client` on any ClickHouse pod. Because the `default` database is a Replicated engine, this single statement propagates to all replicas and the rows replicate through Keeper:

```bash
kubectl exec -i -n clickhouse sample-clickhouse-0-0-0 -- \
  clickhouse-client --multiquery < sample-data.sql
```

Add `--password "<pw>"` once the `default` user has a password. Verify the rows landed on both replicas, which matters because Superset connects through the load-balancing headless service:

```bash
for p in sample-clickhouse-0-0-0 sample-clickhouse-0-1-0; do
  kubectl exec -n clickhouse $p -- clickhouse-client -q \
    "SELECT '$p', count(), round(sum(amount)) FROM default.sales"
done
```

Both pods should report the same count (200000) and sum.

#### Step 2: Port-forward Superset

The build script and the UI both reach Superset over this forward. Leave it running in a separate terminal:

```bash
kubectl -n superset port-forward svc/superset 8088:8088
```

#### Step 3: Build the dashboard, either scripted or by hand

**Option A, scripted (recommended).** Run the bundled [build_dashboard.py](build_dashboard.py). It logs into Superset, then creates (or reuses, if already present) the ClickHouse database connection, the `sales` dataset, the five charts, and the dashboard, and links them. It is idempotent, so re-running it is safe.

```bash
python3 build_dashboard.py
```

It needs only the Python standard library (no `pip install`). When it finishes it prints the dashboard URL, for example `http://localhost:8088/superset/dashboard/2/`. Override any default with environment variables, which is how you point it at production:

```bash
SUPERSET_URL=http://localhost:8088 \
SUPERSET_USER=admin SUPERSET_PASSWORD='<pw>' \
CH_DB_NAME=ClickHouse \
CLICKHOUSE_URI='clickhousedb://default:<pw>@sample-clickhouse-headless.clickhouse.svc.cluster.local:8123/default' \
CH_SCHEMA=default CH_TABLE=sales \
python3 build_dashboard.py
```

What the script does, mapped to the Superset REST API, so you can adapt it for your own tables:

1. `POST /api/v1/security/login` then `GET /api/v1/security/csrf_token/` for an access token and CSRF token.
2. Find or create the database connection with `GET`/`POST /api/v1/database/`, using the `clickhousedb://` SQLAlchemy URI.
3. Find or create the dataset with `GET`/`POST /api/v1/dataset/`, passing the database id, schema, and table name. Superset reads the columns and types from ClickHouse automatically.
4. Create each chart with `POST /api/v1/chart/`, where `params` is a JSON string holding the viz type, the dataset reference, and the metrics and dimensions.
5. Create the dashboard with `POST /api/v1/dashboard/`, where `position_json` is the grid layout that places each chart by its id.
6. Link every chart to the dashboard with `PUT /api/v1/chart/{id}` setting `dashboards: [id]`.

**Option B, by hand in the UI.** The same result through the web interface, useful for ad hoc charts:

1. Register the dataset. Go to Datasets, then the plus button, choose the ClickHouse database, the `default` schema, and the `sales` table, and save. On the dataset's Metrics tab, add `SUM(amount)` labeled Revenue and `COUNT(*)` labeled Orders so you do not redefine them per chart.
2. Create each chart. Go to Charts, then plus, pick the `sales` dataset and a visualization type. KPI: Big Number with metric `SUM(amount)`. Trend: Line Chart with X axis `order_date`, time grain Week, metric `SUM(amount)`. Breakdown: Pie with dimension `category`, metric `SUM(amount)`. Ranked list: Table in aggregate mode with dimension `country`, metric `SUM(amount)`, sorted descending. Run to preview, then Save with a name.
3. Assemble the dashboard. Go to Dashboards, then plus. Drag the saved charts onto the canvas, KPIs on the top row and larger charts below, resize by dragging edges, set the title, then Save and toggle Publish.
4. Add interactivity. In Edit dashboard, use the filter icon to add a date-range filter on `order_date` or a dropdown on `country`. Filters apply to every chart at once.

#### Step 4: Open the dashboard

With the port-forward running, open the printed URL or go to Dashboards and click **Sales Overview**.

### Promoting dashboards between environments

Export a dashboard from the Dashboards list as a ZIP bundle and import the same bundle in the target Superset. The bundle carries the charts, the dataset, and the database reference, so promoting dev to production is one import plus updating the database password. The scripted approach also works across environments by changing the environment variables in Step 3.

---

## Appendix: full settings reference

Every field below comes from the live CRD schemas (`clickhouse.com/v1alpha1`). Defaults and enums are the operator's own. Pod-level Kubernetes fields (`affinity`, `tolerations`, full container spec, etc.) follow the standard Kubernetes pod schema and are not re-listed exhaustively.

### `ClickHouseCluster` spec

Required: `keeperClusterRef`.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `shards` | integer | 1 | Number of shards. |
| `replicas` | integer | — | Replicas in each shard. |
| `keeperClusterRef.name` | string | — | Name of the `KeeperCluster` to coordinate with. Required. |
| `keeperClusterRef.namespace` | string | (same ns) | Namespace of the Keeper cluster. |
| `clusterDomain` | string | `cluster.local` | DNS suffix used for service resolution. |
| `upgradeChannel` | string | (minor only) | `stable`, `lts`, or a `major.minor` pin. Controls auto-proposed major upgrades. |
| `annotations` / `labels` | object | — | Extra metadata applied to all generated resources. |
| `externalSecret.name` | string | — | Reference to an externally managed Secret with credentials. |
| `externalSecret.policy` | string | `Observe` | How the operator treats that Secret's contents. |

#### `spec.settings` (ClickHouse server config)

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `defaultUserPassword.passwordType` | string | `password` | Use `sha256_hash` for hashed secrets. See ClickHouse user settings docs. |
| `defaultUserPassword.secret.name` / `.key` | string | — | Source the password from a Secret key (recommended). |
| `defaultUserPassword.configMap.name` / `.key` | string | — | Alternative source from a ConfigMap key. |
| `enableDatabaseSync` | boolean | `true` | Sync existing databases to newly created replicas. |
| `extraConfig` | object | — | Arbitrary ClickHouse server config merged with the operator defaults (free-form XML/YAML keys). |
| `extraUsersConfig` | object | — | Additional users/profiles/quotas config, merged with defaults. |
| `logger.level` | string | `trace` | One of `test, trace, debug, information, notice, warning, error, critical, fatal`. Use `information` in prod. |
| `logger.jsonLogs` | boolean | `false` | Emit structured JSON logs. |
| `logger.logToFile` | boolean | `true` | Disable to log only to stdout. |
| `logger.count` | integer | `50` | Max log files to retain. |
| `logger.size` | string | `1000M` | Max size per log file. |
| `tls.enabled` | boolean | `false` | Turn on secure (HTTPS/native-TLS) ports. |
| `tls.required` | boolean | `false` | Refuse all plaintext connections when true. |
| `tls.serverCertSecret.name` | string | `""` | Secret holding the server TLS cert/key. |
| `tls.caBundle.name` / `.key` | string | — | Secret holding the CA bundle for verification. |

#### `spec.containerTemplate` (ClickHouse container)

| Field | Type | Notes |
| --- | --- | --- |
| `image.repository` | string | e.g. `docker.io/clickhouse/clickhouse-server`. |
| `image.tag` | string | Mutually exclusive with `image.hash`. |
| `image.hash` | string | Pin by digest instead of tag. |
| `imagePullPolicy` | string | `Always`, `Never`, or `IfNotPresent`. |
| `resources.requests` / `.limits` | object | Standard `cpu`/`memory`. Do not set a CPU limit on ClickHouse. |
| `resources.claims` | array | Dynamic resource allocation claims. |
| `env`, `volumeMounts`, `livenessProbe`, `readinessProbe`, `securityContext` | various | Standard Kubernetes container fields, merged with operator defaults. |

#### `spec.podTemplate` (ClickHouse pod)

| Field | Type | Notes |
| --- | --- | --- |
| `nodeHostnameKey` | string | Set to `kubernetes.io/hostname` to enable hard one-replica-per-node anti-affinity. |
| `topologyZoneKey` | string | Set to `topology.kubernetes.io/zone` for soft zone spread. |
| `affinity` | object | Standard Kubernetes affinity, if you need custom rules beyond the two keys above. |
| `tolerations` | array | Schedule onto tainted (e.g. dedicated) nodes. |
| `topologySpreadConstraints` | array | Custom spread constraints. |
| `nodeSelector` | object | Pin to labeled nodes. |
| `priorityClassName` | string | Raise scheduling priority for the database. |
| `runtimeClassName` | string | Select a container runtime class. |
| `schedulerName` | string | Use a non-default scheduler. |
| `serviceAccountName` | string | Pod service account. |
| `terminationGracePeriodSeconds` | integer | Allow time for clean shutdown / flush. |
| `imagePullSecrets` | array | Pull from a private registry. |
| `initContainers` / `volumes` / `securityContext` | various | Standard pod fields. |

#### `spec.dataVolumeClaimSpec` (persistent storage)

Standard `PersistentVolumeClaimSpec`. Common fields:

| Field | Notes |
| --- | --- |
| `accessModes` | Typically `["ReadWriteOnce"]`. |
| `storageClassName` | Use an SSD-backed, `Retain`-policy class in prod. |
| `resources.requests.storage` | Size with ~30% merge headroom. |
| `volumeMode`, `selector`, `dataSource`, `dataSourceRef`, `volumeAttributesClassName` | Standard PVC options for advanced cases. |

#### `spec.podDisruptionBudget`

| Field | Notes |
| --- | --- |
| `minAvailable` / `maxUnavailable` | Set one. `maxUnavailable: 1` is the recommended default. |
| `policy` | Whether the operator creates the PDB at all. |
| `unhealthyPodEvictionPolicy` | When unhealthy pods may be evicted. |

### `KeeperCluster` spec

Required: none. Same shape as above minus ClickHouse-specific fields.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `replicas` | integer | — | Use an odd number; `3` is standard. |
| `clusterDomain` | string | `cluster.local` | DNS suffix. |
| `upgradeChannel` | string | — | Same semantics as ClickHouse. |
| `settings.extraConfig` | object | — | Free-form Keeper config merged with defaults. |
| `settings.logger.*` | — | (see above) | Same logger fields and defaults as ClickHouse. |
| `settings.tls.*` | — | (see above) | Same TLS fields as ClickHouse. |
| `containerTemplate.*` | — | — | Same image/resources/probes fields as ClickHouse. |
| `podTemplate.*` | — | — | Same scheduling fields, including `nodeHostnameKey` and `topologyZoneKey`. |
| `dataVolumeClaimSpec.*` | — | — | Keep small but on fast SSD; Keeper fsyncs the Raft log. |
| `podDisruptionBudget.*` | — | — | `maxUnavailable: 1` to protect quorum. |
