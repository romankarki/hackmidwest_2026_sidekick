"""What's running and what it costs, across the clouds Sidekick is connected to (read-only).

Akamai Cloud (Linode API v4, token from Integrations → Akamai Cloud): VMs, LKE clusters, NodeBalancers, firewalls,
month-to-date usage (balance_uninvoiced), promo credit; monthly run-rate from the plans' list prices.
AWS (Integrations → AWS): month-to-date cost per service + forecast (Cost Explorer: $0.01 per request → cached 1 h),
budgets, and running resources (EC2, Lambda, S3, CloudWatch dashboards/alarms, IAM users).
Every function returns plain dicts; a provider that isn't configured returns {"configured": False}.
"""
import time
from datetime import date, timedelta

import requests

from . import integrations

LINODE = "https://api.linode.com/v4"
_cache = {}


def _cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (time.time(), value)
    return value


# ---------- Akamai Cloud (Linode) ----------
def _linode(path, **params):
    token = integrations.creds("akamai_cloud").get("api_token")
    r = requests.get(f"{LINODE}{path}", headers={"Authorization": f"Bearer {token}"}, params={"page_size": 200, **params}, timeout=15)
    r.raise_for_status()
    return r.json()


def _linode_type(type_id):
    return _cached(f"linode-type:{type_id}", 86400, lambda: _linode(f"/linode/types/{type_id}"))


def akamai_cloud():
    if not integrations.creds("akamai_cloud").get("api_token"):
        return {"configured": False}
    vms, run_rate = [], 0.0
    for v in _linode("/linode/instances")["data"]:
        t = _linode_type(v["type"])
        monthly = t["price"]["monthly"] or 0
        run_rate += monthly  # a powered-off Linode still bills; LKE worker nodes are Linodes too, so they're counted here
        vms.append({"label": v["label"], "status": v["status"], "region": v["region"], "plan": f"{t['label']} ({v['type']})",
                    "ipv4": v["ipv4"][0] if v["ipv4"] else None, "monthly_usd": monthly, "tags": v.get("tags", []),
                    "lke_node": v["label"].startswith("lke")})
    clusters = []
    for c in _linode("/lke/clusters")["data"]:
        pools = _linode(f"/lke/clusters/{c['id']}/pools")["data"]
        nodes = sum(p["count"] for p in pools)
        ha = c.get("control_plane", {}).get("high_availability")
        clusters.append({"label": c["label"], "id": c["id"], "k8s": c["k8s_version"], "region": c["region"], "nodes": nodes,
                         "node_plans": [f"{p['count']}× {p['type']}" for p in pools], "ha_control_plane": bool(ha),
                         "control_plane_monthly_usd": 60.0 if ha else 0.0})
        run_rate += 60.0 if ha else 0.0  # node VMs are already counted in `vms`
    nbs = [{"label": n["label"], "region": n["region"], "ipv4": n["ipv4"], "hostname": n["hostname"], "monthly_usd": 10.0}
           for n in _linode("/nodebalancers")["data"]]
    run_rate += 10.0 * len(nbs)
    fws = [{"label": f["label"], "status": f["status"], "attached": len(f.get("entities", []))} for f in _linode("/networking/firewalls")["data"]]
    acct = _linode("/account")
    promos = [{"promo": p.get("summary"), "credit_remaining_usd": float(p.get("credit_remaining") or 0), "expires": p.get("expire_dt")}
              for p in acct.get("active_promotions", [])]
    return {"configured": True, "provider": "Akamai Cloud (Linode)",
            "vms": vms, "lke_clusters": clusters, "nodebalancers": nbs, "firewalls": fws,
            "costs": {"month_to_date_usd": acct.get("balance_uninvoiced"), "balance_due_usd": acct.get("balance"),
                      "run_rate_monthly_usd": round(run_rate, 2), "promotions": promos,
                      "note": "month_to_date = usage accrued this month, before promo credit; run rate = list prices of what is running now"}}


# ---------- AWS ----------
def _aws_configured():
    return integrations.get_row("aws")["status"] != "not_configured"


def _safe(fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — one missing permission shouldn't hide the rest
        return {"error": f"{e.__class__.__name__}: {str(e)[:160]}"}


def aws():
    if not _aws_configured():
        return {"configured": False}
    region = integrations.creds("aws").get("region") or "us-east-2"
    b = integrations._boto

    def ec2():
        rs = b("ec2", None).describe_instances(Filters=[{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])
        return [{"id": i["InstanceId"], "type": i["InstanceType"], "state": i["State"]["Name"],
                 "name": next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), None)} for r in rs["Reservations"] for i in r["Instances"]]

    def lam():
        return [{"name": f["FunctionName"], "runtime": f.get("Runtime")} for f in b("lambda", None).list_functions()["Functions"]]

    def s3():
        return [x["Name"] for x in b("s3", None).list_buckets()["Buckets"]]

    def cw():
        c = b("cloudwatch", None)
        alarms = c.describe_alarms(AlarmTypes=["MetricAlarm"])["MetricAlarms"]
        return {"dashboards": [d["DashboardName"] for d in c.list_dashboards()["DashboardEntries"]],
                "alarms": [{"name": a["AlarmName"], "state": a["StateValue"]} for a in alarms]}

    def iam():
        return [u["UserName"] for u in b("iam", None).list_users()["Users"]]

    sts = _safe(lambda: b("sts", None).get_caller_identity()["Account"])
    return {"configured": True, "provider": "AWS", "account": sts, "region": region,
            "ec2_instances": _safe(ec2), "lambda_functions": _safe(lam), "s3_buckets": _safe(s3),
            "cloudwatch": _safe(cw), "iam_users": _safe(iam), "costs": aws_costs()}


def aws_costs():
    if not _aws_configured():
        return {"configured": False}

    def fetch():
        ce = integrations._boto("ce", None, region="us-east-1")  # Cost Explorer is global (us-east-1)
        today = date.today()
        start = today.replace(day=1)
        end = today + timedelta(days=1)
        res = ce.get_cost_and_usage(TimePeriod={"Start": start.isoformat(), "End": end.isoformat()}, Granularity="MONTHLY",
                                    Metrics=["UnblendedCost"], GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}])
        by_service = {}
        for period in res["ResultsByTime"]:
            for g in period["Groups"]:
                by_service[g["Keys"][0]] = by_service.get(g["Keys"][0], 0.0) + float(g["Metrics"]["UnblendedCost"]["Amount"])
        services = sorted(({"service": k, "usd": round(v, 4)} for k, v in by_service.items() if v >= 0.0001), key=lambda x: -x["usd"])
        out = {"month_to_date_usd": round(sum(by_service.values()), 4), "by_service": services[:15]}
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        if today + timedelta(days=1) < next_month:
            fc = _safe(lambda: ce.get_cost_forecast(TimePeriod={"Start": (today + timedelta(days=1)).isoformat(), "End": next_month.isoformat()},
                                                     Metric="UNBLENDED_COST", Granularity="MONTHLY"))
            out["forecast_rest_of_month_usd"] = round(float(fc["Total"]["Amount"]), 2) if "Total" in fc else fc
        acct = integrations._boto("sts", None).get_caller_identity()["Account"]
        budgets = _safe(lambda: integrations._boto("budgets", None, region="us-east-1").describe_budgets(AccountId=acct).get("Budgets", []))
        out["budgets"] = [{"name": x["BudgetName"], "limit_usd": float(x["BudgetLimit"]["Amount"]),
                           "actual_usd": float(x.get("CalculatedSpend", {}).get("ActualSpend", {}).get("Amount", 0)),
                           "forecast_usd": float(x.get("CalculatedSpend", {}).get("ForecastedSpend", {}).get("Amount", 0) or 0)}
                          for x in budgets] if isinstance(budgets, list) else budgets
        out["note"] = "Cost Explorer data lags up to ~24 h; cached for 1 hour (each Cost Explorer call costs $0.01)"
        return out
    return _cached("aws-costs", 3600, lambda: _safe(fetch))
