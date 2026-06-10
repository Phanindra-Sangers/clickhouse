#!/usr/bin/env python3
"""
Build the example "Sales Overview" dashboard in Superset on a ClickHouse table.

Creates (and reuses if already present): the ClickHouse database connection, the
`sales` dataset, five charts, and the dashboard. Safe to re-run.

This script has NO MinIO configuration. Loading Parquet from MinIO is a separate
ClickHouse SQL step (see sample-data.sql and the README); this script only builds
the Superset dashboard and reads the already-loaded table through ClickHouse.

Prerequisites:
  1. Superset is running and the `sales` table is loaded in ClickHouse
     (see sample-data.sql or your MinIO Parquet ingestion).
  2. A port-forward to Superset:
       kubectl -n superset port-forward svc/superset 8088:8088

Usage:
  python3 build_dashboard.py

Override defaults via environment variables:
  SUPERSET_URL        default http://localhost:8088
  SUPERSET_USER       default admin
  SUPERSET_PASSWORD   default admin
  CLICKHOUSE_URI      default clickhousedb://default:@sample-clickhouse-headless.clickhouse.svc.cluster.local:8123/default
  CH_DB_NAME          default ClickHouse           (name of the Superset DB connection)
  CH_SCHEMA           default default              (ClickHouse database/schema)
  CH_TABLE            default sales
"""
import os, json, urllib.request, urllib.error, urllib.parse, http.cookiejar

BASE  = os.environ.get("SUPERSET_URL", "http://localhost:8088")
USER  = os.environ.get("SUPERSET_USER", "admin")
PASS  = os.environ.get("SUPERSET_PASSWORD", "admin")
CH_URI = os.environ.get("CLICKHOUSE_URI",
    "clickhousedb://default:@sample-clickhouse-headless.clickhouse.svc.cluster.local:8123/default")
DB_NAME = os.environ.get("CH_DB_NAME", "ClickHouse")
SCHEMA  = os.environ.get("CH_SCHEMA", "default")
TABLE   = os.environ.get("CH_TABLE", "sales")

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
_token = _csrf = None

def req(method, path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if _token: r.add_header("Authorization", "Bearer " + _token)
    if _csrf:
        r.add_header("X-CSRFToken", _csrf)
        r.add_header("Referer", BASE)
    try:
        with opener.open(r) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, method, path, e.read().decode()[:400])
        raise

def get_all(path):
    """Fetch a list endpoint (page_size 100) and return result list."""
    q = urllib.parse.quote("(page_size:100)")
    return req("GET", f"{path}?q={q}").get("result", [])

# --- auth ---
_token = req("POST", "/api/v1/security/login",
             {"username": USER, "password": PASS, "provider": "db", "refresh": True})["access_token"]
_csrf = req("GET", "/api/v1/security/csrf_token/")["result"]

# --- 1. database connection (find or create) ---
db_id = next((d["id"] for d in get_all("/api/v1/database/") if d["database_name"] == DB_NAME), None)
if db_id is None:
    db_id = req("POST", "/api/v1/database/",
                {"database_name": DB_NAME, "sqlalchemy_uri": CH_URI})["id"]
    print("created database connection", DB_NAME, "id", db_id)
else:
    print("using existing database connection", DB_NAME, "id", db_id)

# --- 2. dataset (find or create) ---
ds_id = next((d["id"] for d in get_all("/api/v1/dataset/")
              if d["table_name"] == TABLE and d["database"]["id"] == db_id), None)
if ds_id is None:
    ds_id = req("POST", "/api/v1/dataset/",
                {"database": db_id, "schema": SCHEMA, "table_name": TABLE})["id"]
    print("created dataset", TABLE, "id", ds_id)
else:
    print("using existing dataset", TABLE, "id", ds_id)

# --- 3. charts (find or create by name) ---
def metric(agg, col, label, opt):
    return {"expressionType": "SIMPLE", "column": {"column_name": col}, "aggregate": agg,
            "label": label, "hasCustomLabel": True, "optionName": opt}

REV = metric("SUM", "amount", "Revenue", "m_rev")
ORD = metric("COUNT", "order_id", "Orders", "m_ord")

chart_defs = [
 ("Total Revenue", "big_number_total", {
    "datasource": f"{ds_id}__table", "viz_type": "big_number_total", "metric": REV,
    "adhoc_filters": [], "y_axis_format": "$,.0f", "header_font_size": 0.4, "subheader_font_size": 0.15}),
 ("Total Orders", "big_number_total", {
    "datasource": f"{ds_id}__table", "viz_type": "big_number_total", "metric": ORD,
    "adhoc_filters": [], "y_axis_format": ",d"}),
 ("Revenue by Day", "echarts_timeseries_line", {
    "datasource": f"{ds_id}__table", "viz_type": "echarts_timeseries_line",
    "x_axis": "order_date", "time_grain_sqla": "P1W", "metrics": [REV], "groupby": [],
    "adhoc_filters": [], "x_axis_sort_asc": True, "y_axis_format": "$,.0f"}),
 ("Revenue by Category", "pie", {
    "datasource": f"{ds_id}__table", "viz_type": "pie", "groupby": ["category"], "metric": REV,
    "adhoc_filters": [], "row_limit": 25, "show_legend": True, "label_type": "key_value"}),
 ("Revenue by Country", "table", {
    "datasource": f"{ds_id}__table", "viz_type": "table", "query_mode": "aggregate",
    "groupby": ["country"], "metrics": [REV], "adhoc_filters": [],
    "order_desc": True, "row_limit": 50, "server_pagination": False}),
]

existing = {c["slice_name"]: c["id"] for c in get_all("/api/v1/chart/")}
ids = []
for name, viz, params in chart_defs:
    if name in existing:
        ids.append((existing[name], name)); print("reusing chart", name); continue
    cid = req("POST", "/api/v1/chart/", {
        "slice_name": name, "viz_type": viz,
        "datasource_id": ds_id, "datasource_type": "table",
        "params": json.dumps(params)})["id"]
    ids.append((cid, name)); print("created chart", cid, name)

# --- 4. dashboard (find or create) + layout ---
pos = {
 "DASHBOARD_VERSION_KEY": "v2",
 "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
 "GRID_ID": {"type": "GRID", "id": "GRID_ID", "parents": ["ROOT_ID"], "children": ["ROW-1", "ROW-2"]},
 "HEADER_ID": {"type": "HEADER", "id": "HEADER_ID", "meta": {"text": "Sales Overview"}},
 "ROW-1": {"type": "ROW", "id": "ROW-1", "parents": ["ROOT_ID", "GRID_ID"],
           "children": ["CHART-1", "CHART-2", "CHART-3"], "meta": {"background": "BACKGROUND_TRANSPARENT"}},
 "ROW-2": {"type": "ROW", "id": "ROW-2", "parents": ["ROOT_ID", "GRID_ID"],
           "children": ["CHART-4", "CHART-5"], "meta": {"background": "BACKGROUND_TRANSPARENT"}},
}
widths = [3, 3, 6, 5, 7]
for i, (cid, name) in enumerate(ids, start=1):
    row = "ROW-1" if i <= 3 else "ROW-2"
    pos[f"CHART-{i}"] = {"type": "CHART", "id": f"CHART-{i}", "children": [],
                         "parents": ["ROOT_ID", "GRID_ID", row],
                         "meta": {"chartId": cid, "width": widths[i-1], "height": 50, "sliceName": name}}

TITLE = "Sales Overview"
dash_id = next((d["id"] for d in get_all("/api/v1/dashboard/") if d["dashboard_title"] == TITLE), None)
payload = {"dashboard_title": TITLE, "published": True, "position_json": json.dumps(pos)}
if dash_id is None:
    dash_id = req("POST", "/api/v1/dashboard/", payload)["id"]
    print("created dashboard", dash_id)
else:
    req("PUT", f"/api/v1/dashboard/{dash_id}", payload)
    print("updated dashboard", dash_id)

# --- 5. link charts to the dashboard ---
for cid, name in ids:
    req("PUT", f"/api/v1/chart/{cid}", {"dashboards": [dash_id]})

print(f"\nDone. Dashboard: {BASE}/superset/dashboard/{dash_id}/")
