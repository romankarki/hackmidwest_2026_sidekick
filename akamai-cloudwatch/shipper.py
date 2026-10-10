"""Ship ShopLite metrics from Akamai Cloud to Amazon CloudWatch (multi-cloud observability).

Every INTERVAL seconds: scrape each target's Prometheus /metrics, turn the counters into per-interval values and
PutMetricData into the `ShopLite/Akamai` namespace:

  Requests, Errors5xx, LatencyAvg (ms), LatencyP95 (ms)   dims {Environment, Route} and {Environment, Route=ALL}
  Orders                                                   dims {Environment}
  HostCPUUtilization, HostMemoryUtilization (%)            dims {Host}   (the Akamai VM this runs on)

Optional SYNTHETIC_TRAFFIC=1 runs a light read-only canary (browse, view cart) so dashboards have data during demos.
Env: TARGETS="prod=http://host.docker.internal:8080" · AWS_REGION · AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (an IAM user
that may only PutMetricData into this namespace) · INTERVAL=60 · HOST_NAME=sidekick-prod
"""
import os
import random
import re
import threading
import time
from collections import defaultdict

import boto3
import requests

NAMESPACE = os.getenv("CW_NAMESPACE", "ShopLite/Akamai")
INTERVAL = int(os.getenv("INTERVAL", "60"))
TARGETS = dict(t.split("=", 1) for t in os.getenv("TARGETS", "prod=http://host.docker.internal:8080").split(",") if "=" in t)
HOST = os.getenv("HOST_NAME", "sidekick-prod")
NOISE = {"/metrics", "/api/health"}  # scrapes and health checks aren't user traffic
LINE = re.compile(r'^(\w+)\{(.*)\}\s+([0-9.eE+-]+|NaN|\+Inf)$')  # greedy: route labels like /api/products/{product_id} contain braces


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def scrape(base):
    """{(route, status): {"count", "sum", "buckets": {le: n}}} and orders total, all cumulative."""
    series, orders = defaultdict(lambda: {"count": 0.0, "sum": 0.0, "buckets": {}}), 0.0
    for line in requests.get(f"{base}/metrics", timeout=10).text.splitlines():
        m = LINE.match(line)
        if not m:
            if line.startswith("shoplite_orders_total "):
                orders = float(line.split()[1])
            continue
        name, labels, value = m.group(1), dict(re.findall(r'(\w+)="([^"]*)"', m.group(2))), float(m.group(3))
        if name == "shoplite_orders_total":
            orders += value
        if not name.startswith("http_request_duration_seconds"):
            continue
        s = series[(labels.get("route", "?"), labels.get("status", "200"))]
        if name.endswith("_count"):
            s["count"] = value
        elif name.endswith("_sum"):
            s["sum"] = value
        elif name.endswith("_bucket"):
            s["buckets"][float("inf") if labels["le"] == "+Inf" else float(labels["le"])] = value
    return series, orders


def p95(buckets):
    """95th percentile (seconds) from per-interval histogram bucket counts, linear within the bucket."""
    total = buckets.get(float("inf"), 0)
    if total <= 0:
        return None
    target, prev_le, prev_n = 0.95 * total, 0.0, 0.0
    for le in sorted(buckets):
        n = buckets[le]
        if n >= target:
            if le == float("inf"):
                return prev_le
            return prev_le + (le - prev_le) * ((target - prev_n) / max(n - prev_n, 1e-9))
        prev_le, prev_n = le, n
    return prev_le


def interval_metrics(env, prev, cur, prev_orders, cur_orders):
    by_route = defaultdict(lambda: {"count": 0.0, "sum": 0.0, "errors": 0.0, "buckets": defaultdict(float)})
    for key, c in cur.items():
        p = prev.get(key, {"count": 0.0, "sum": 0.0, "buckets": {}})
        dc = c["count"] - p["count"]
        if dc < 0:  # app restarted → counters reset
            p, dc = {"count": 0.0, "sum": 0.0, "buckets": {}}, c["count"]
        if dc == 0:
            continue
        route, status = key
        for r in (route, None if route in NOISE else "ALL"):
            if r is None:
                continue
            agg = by_route[r]
            agg["count"] += dc
            agg["sum"] += c["sum"] - p["sum"]
            agg["errors"] += dc if status.startswith("5") else 0
            for le, n in c["buckets"].items():
                agg["buckets"][le] += n - p["buckets"].get(le, 0.0)
    data = []
    for route, a in by_route.items():
        dims = [{"Name": "Environment", "Value": env}, {"Name": "Route", "Value": route}]
        data += [{"MetricName": "Requests", "Dimensions": dims, "Value": a["count"], "Unit": "Count"},
                 {"MetricName": "Errors5xx", "Dimensions": dims, "Value": a["errors"], "Unit": "Count"},
                 {"MetricName": "LatencyAvg", "Dimensions": dims, "Value": a["sum"] / a["count"] * 1000, "Unit": "Milliseconds"}]
        q = p95(a["buckets"])
        if q is not None:
            data.append({"MetricName": "LatencyP95", "Dimensions": dims, "Value": q * 1000, "Unit": "Milliseconds"})
    if "ALL" not in by_route:  # no user traffic this interval: still report zeros so alarms/graphs don't go blank
        dims = [{"Name": "Environment", "Value": env}, {"Name": "Route", "Value": "ALL"}]
        data += [{"MetricName": "Requests", "Dimensions": dims, "Value": 0, "Unit": "Count"},
                 {"MetricName": "Errors5xx", "Dimensions": dims, "Value": 0, "Unit": "Count"}]
    data.append({"MetricName": "Orders", "Dimensions": [{"Name": "Environment", "Value": env}],
                 "Value": max(cur_orders - prev_orders, 0), "Unit": "Count"})
    return data


def host_metrics(state):
    with open("/proc/stat") as f:
        cpu = [float(x) for x in f.readline().split()[1:]]
    idle, total = cpu[3] + cpu[4], sum(cpu)
    out = []
    if state.get("cpu"):
        di, dt = idle - state["cpu"][0], total - state["cpu"][1]
        if dt > 0:
            out.append({"MetricName": "HostCPUUtilization", "Dimensions": [{"Name": "Host", "Value": HOST}], "Value": 100 * (1 - di / dt), "Unit": "Percent"})
    state["cpu"] = (idle, total)
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            mem[k] = float(v.split()[0])
    out.append({"MetricName": "HostMemoryUtilization", "Dimensions": [{"Name": "Host", "Value": HOST}],
                "Value": 100 * (1 - mem["MemAvailable"] / mem["MemTotal"]), "Unit": "Percent"})
    return out


def canary(base):
    """Light synthetic traffic: browse the catalog, view two products, view the cart (read-only, no orders)."""
    s = requests.Session()
    s.headers["User-Agent"] = "sidekick-canary"
    while True:
        try:
            prods = s.get(f"{base}/api/products", timeout=10).json()
            items = prods if isinstance(prods, list) else prods.get("products", [])
            for p in random.sample(items, min(2, len(items))):
                s.get(f"{base}/api/products/{p['id']}", timeout=10)
            s.get(f"{base}/api/cart", timeout=10)
        except Exception as e:  # noqa: BLE001
            log("canary:", e)
        time.sleep(random.uniform(8, 15))


def main():
    if not (os.getenv("AWS_ACCESS_KEY_ID") or os.path.exists(os.path.expanduser("~/.aws/credentials"))):
        log("No AWS credentials (CW_AWS_ACCESS_KEY_ID in sidekick/.env) — idle. Nothing is sent.")
        while True:
            time.sleep(3600)
    cw = boto3.client("cloudwatch", region_name=os.getenv("AWS_REGION", "us-west-2"))
    if os.getenv("SYNTHETIC_TRAFFIC") == "1":
        for base in TARGETS.values():
            threading.Thread(target=canary, args=(base,), daemon=True).start()
    log(f"shipping {list(TARGETS)} → CloudWatch {NAMESPACE} every {INTERVAL}s")
    prev, state = {}, {}
    while True:
        batch = host_metrics(state)
        for env, base in TARGETS.items():
            try:
                series, orders = scrape(base)
            except requests.RequestException as e:
                log(f"{env}: scrape failed: {e}")
                batch.append({"MetricName": "ScrapeFailures", "Dimensions": [{"Name": "Environment", "Value": env}], "Value": 1, "Unit": "Count"})
                continue
            if env in prev:
                batch += interval_metrics(env, prev[env][0], series, prev[env][1], orders)
            prev[env] = (series, orders)
        for i in range(0, len(batch), 500):  # PutMetricData: up to 1000 metrics per call
            cw.put_metric_data(Namespace=NAMESPACE, MetricData=batch[i:i + 500])
        all_rows = [m for m in batch if m["MetricName"] == "Requests" and m["Dimensions"][-1]["Value"] == "ALL"]
        log(f"sent {len(batch)} datapoints · requests " + ", ".join(f"{m['Dimensions'][0]['Value']}={m['Value']:.0f}" for m in all_rows))
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
