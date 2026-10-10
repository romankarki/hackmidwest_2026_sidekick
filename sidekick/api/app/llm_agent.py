"""LLM brain — a Strands agent with real, read-only tools wired to Sidekick's integrations.

Enabled with Settings → Brain mode = "LLM agent" (settings key `brain_mode` = "llm").
The model comes from Configure → Model (`models.resolve()`):

  provider  | transport                     | Strands model
  ----------|-------------------------------|------------------------------------------------------------
  bedrock   | messages (default)            | AnthropicModel + anthropic.AsyncAnthropicBedrockMantle client
  bedrock   | converse                      | BedrockModel (Bedrock Converse API)
  anthropic | —                             | AnthropicModel (api key from Integrations → Anthropic API)
  openai    | —                             | OpenAIResponsesModel (api key from Integrations → OpenAI)
  akamai_ai | —                             | OpenAIModel (Chat Completions, base URL + key from Integrations → Akamai AI inference)

Every tool call is reported into `runs.steps` through Strands hooks, so Slack and the dashboard show it live.
Tools only READ. The action tools only PROPOSE: `request_approval` (infra) and `propose_code_change` (validated edits;
after approval github_ops opens a real branch + PR) create a pending approval; shipping still goes through
the human Approve button and the configured workflow.
"""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
from strands import Agent, tool
from strands.hooks import AfterToolCallEvent, BeforeModelCallEvent, BeforeToolCallEvent, HookProvider

from . import db, github_ops, integrations, inventory, models

TIMEOUT = 10
MAX_TOOL_TEXT = 6000  # keep tool results small enough for the model's context

SYSTEM_PROMPT = """You are {agent_name}, the operations and developer agent for the ShopLite e-commerce app.
Engineers talk to you in Slack, DevRev and a web dashboard. They expect you to DO the work, not to interview them.

How you work:
- Act first. When asked to build, add, change, enable, rename or fix something, investigate with your tools and deliver a
  concrete proposal in the same turn: a pull request via `propose_code_change`, or an infra change via `request_approval`.
  A human approves before anything ships, so a reasonable proposal is always better than a list of questions.
- Don't ask what you can find out. Repo layout, frameworks, existing endpoints, feature flags and file contents are one
  tool call away — look them up. Never assume a technology (e.g. React, mobile apps) the repo brief doesn't mention.
- Make sensible assumptions and state them in one line ("Assumed web only; enabled via the existing `swipe` flag").
  Ask a question only when the request is truly ambiguous AND a wrong guess would be costly; then ask one short question.
- Prefer what already exists: if the feature is already implemented behind a flag, enable it instead of rebuilding it.
- Code changes: find the code (`github_search_code` / `github_list_files`), read it (`github_read_file`), then call
  `propose_code_change` ONCE with all edits. Each `find` is copied exactly from the file; an empty `find` creates a new
  file. Keep changes minimal and in the repo's style. Pass your `assumptions` and concrete `follow_ups`: after approval
  Sidekick opens the PR with a live preview, and keeps them for a later hand-off.
- Hand-off: when the user says the preview/PR looks good and asks to file an issue / hand it to a developer, call
  `file_review_issue` right away (it defaults to this conversation's PR; pass their words as `notes`). Only when asked.
- Production problems: ShopLite runs on Akamai Cloud and ships metrics to Amazon CloudWatch (namespace ShopLite/Akamai).
  Start with `cloudwatch_shoplite_overview` and `cloudwatch_alarms`, correlate with `github_recent_commits`, then propose a fix.
- Infra questions ("what's running", "what services do we have", "what does it cost", "usage this month"): call
  `infra_inventory` and/or `infra_costs` and answer per cloud (Akamai Cloud, AWS) with names, counts and dollar amounts.
- Identity questions ("who are you", "where are you from", "how do I configure you"): call `sidekick_about` and answer
  with the dashboard URL and the configure links, the model you run on, and where you're hosted.
- If an integration isn't connected, say which one in one line and continue with what you can check.
- Never claim a change was made before it was approved and executed. After proposing, say "Proposed …" and that
  approving opens a pull request with a live preview — it does not merge or deploy anything.

Style: concise. Lead with what you did or found, then the evidence (numbers, SHAs, file names), then the next step.
No greetings, no checklists of questions, no emoji headers. Slack mrkdwn: *bold*, _italic_, `code`, ```blocks```, "• " bullets.

Current conversation status: {status}. Requested by: {user}."""


# ---------- helpers ----------
def _clip(obj) -> str:
    text = obj if isinstance(obj, str) else json.dumps(obj, default=str, indent=1)
    return text if len(text) <= MAX_TOOL_TEXT else text[:MAX_TOOL_TEXT] + f"\n… (truncated, {len(text)} chars total)"


def _not_connected(name, key):
    return f"{name} is not connected. Configure it in Sidekick → Integrations → {integrations.CATALOG[key]['name']}."


def _shoplite_base():
    url = (integrations.creds("shoplite").get("url") or "").rstrip("/")
    return url or None


# ---------- tools (real, read-only) ----------
@tool
def shoplite_health() -> str:
    """Check whether the ShopLite app is up, and which version / branch / feature flags it runs."""
    base = _shoplite_base()
    if not base:
        return _not_connected("ShopLite", "shoplite")
    t0 = time.perf_counter()
    r = requests.get(f"{base}/api/health", timeout=TIMEOUT)
    return _clip({"http_status": r.status_code, "latency_ms": round((time.perf_counter() - t0) * 1000), "body": r.json()})


@tool
def shoplite_route_latency() -> str:
    """Read ShopLite's live Prometheus metrics and summarise request count, error count and average latency per API route."""
    base = _shoplite_base()
    if not base:
        return _not_connected("ShopLite", "shoplite")
    text = requests.get(f"{base}/metrics", timeout=TIMEOUT).text
    agg = {}
    for line in text.splitlines():
        if not line.startswith("http_request_duration_seconds_") or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        if "_bucket" in name:
            continue
        kind = "count" if "_count{" in name else "sum" if "_sum{" in name else None
        if not kind:
            continue
        labels = dict(p.split("=", 1) for p in name[name.index("{") + 1:-1].split(","))
        labels = {k: v.strip('"') for k, v in labels.items()}
        key = f'{labels.get("method")} {labels.get("route")}'
        row = agg.setdefault(key, {"requests": 0, "errors": 0, "total_seconds": 0.0})
        if kind == "count":
            row["requests"] += int(float(value))
            if labels.get("status", "200").startswith("5"):
                row["errors"] += int(float(value))
        else:
            row["total_seconds"] += float(value)
    out = {k: {"requests": v["requests"], "5xx": v["errors"], "avg_ms": round(v["total_seconds"] / v["requests"] * 1000, 1) if v["requests"] else None}
           for k, v in sorted(agg.items(), key=lambda kv: -kv[1]["requests"])}
    return _clip(out or "No request metrics yet — the app hasn't served API traffic since it started.")


@tool
def shoplite_catalog_cache_headers() -> str:
    """Fetch ShopLite's product catalog endpoint and report its caching headers (what a CDN like Akamai will do with it)."""
    base = _shoplite_base()
    if not base:
        return _not_connected("ShopLite", "shoplite")
    t0 = time.perf_counter()
    r = requests.get(f"{base}/api/products", timeout=TIMEOUT)
    keep = ("cache-control", "expires", "etag", "last-modified", "vary", "age", "x-cache", "x-check-cacheable")
    return _clip({"url": "/api/products", "http_status": r.status_code, "latency_ms": round((time.perf_counter() - t0) * 1000),
                  "headers": {k: v for k, v in r.headers.items() if k.lower() in keep}})


@tool
def prometheus_query(promql: str) -> str:
    """Run an instant PromQL query against the connected Prometheus.

    Args:
        promql: The PromQL expression, e.g. histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, route))
    """
    c = integrations.creds("prometheus")
    if not c.get("url"):
        return _not_connected("Prometheus", "prometheus")
    h = {"Authorization": f"Bearer {c['token']}"} if c.get("token") else {}
    r = requests.get(f"{c['url'].rstrip('/')}/api/v1/query", params={"query": promql}, headers=h, timeout=TIMEOUT)
    return _clip(r.json())


@tool
def cloudwatch_metric(namespace: str, metric_name: str, dimensions: str = "{}", minutes: int = 180, statistic: str = "Average") -> str:
    """Get a CloudWatch metric time series (5-minute points) for the last N minutes.

    Args:
        namespace: e.g. ShopLite/Akamai (production ShopLite on Akamai Cloud), AWS/EC2, AWS/RDS, ContainerInsights
        metric_name: e.g. CPUUtilization, DatabaseConnections, GetRecords.IteratorAgeMilliseconds
        dimensions: JSON object of dimension name to value, e.g. {"DBInstanceIdentifier": "shop-prod"}
        minutes: How far back to look (max 1440)
        statistic: Average, Maximum, Minimum, Sum or SampleCount
    """
    if integrations.get_row("aws")["status"] == "not_configured":
        return _not_connected("AWS", "aws")
    cw = integrations._boto("cloudwatch", None)
    dims = [{"Name": k, "Value": str(v)} for k, v in json.loads(dimensions or "{}").items()]
    end = datetime.now(timezone.utc)
    res = cw.get_metric_statistics(Namespace=namespace, MetricName=metric_name, Dimensions=dims, StartTime=end - timedelta(minutes=min(int(minutes), 1440)),
                                   EndTime=end, Period=300, Statistics=[statistic])
    pts = sorted(res["Datapoints"], key=lambda p: p["Timestamp"])
    return _clip([{"t": p["Timestamp"].strftime("%H:%M"), statistic: round(p[statistic], 2), "unit": p.get("Unit")} for p in pts] or "No datapoints for that metric / dimensions.")


CW_NAMESPACE = "ShopLite/Akamai"  # shipped from the Akamai VM by akamai-cloudwatch/shipper.py


@tool
def cloudwatch_shoplite_overview(minutes: int = 60, environment: str = "prod") -> str:
    """Production health of ShopLite (hosted on Akamai Cloud) from Amazon CloudWatch: requests, 5xx errors, average and p95
    latency per minute for all traffic, the slowest routes, orders, and the Akamai VM's CPU / memory. Start here when
    debugging slowness or errors in production.

    Args:
        minutes: How far back to look (max 1440)
        environment: ShopLite environment shipped to CloudWatch, normally "prod"
    """
    if integrations.get_row("aws")["status"] == "not_configured":
        return _not_connected("AWS", "aws")
    cw = integrations._boto("cloudwatch", None)
    minutes = max(5, min(int(minutes), 1440))
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    period = 60 if minutes <= 180 else 300
    per = "per_min" if period == 60 else "per_5min"  # counts are per period, NOT per second

    def q(qid, name, dims, stat, unit_label):
        return {"Id": qid, "Label": unit_label, "ReturnData": True, "MetricStat": {
            "Metric": {"Namespace": CW_NAMESPACE, "MetricName": name, "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()]},
            "Period": period, "Stat": stat}}
    env_all = {"Environment": environment, "Route": "ALL"}
    res = cw.get_metric_data(StartTime=start, EndTime=end, ScanBy="TimestampAscending", MetricDataQueries=[
        q("req", "Requests", env_all, "Sum", f"requests_{per}"), q("err", "Errors5xx", env_all, "Sum", f"errors_5xx_{per}"),
        q("avg", "LatencyAvg", env_all, "Average", "avg_ms"), q("p95", "LatencyP95", env_all, "Maximum", "p95_ms"),
        q("orders", "Orders", {"Environment": environment}, "Sum", f"orders_{per}"),
        q("cpu", "HostCPUUtilization", {"Host": "sidekick-prod"}, "Average", "cpu_pct"),
        q("mem", "HostMemoryUtilization", {"Host": "sidekick-prod"}, "Average", "mem_pct")])
    series = {r["Label"]: dict(zip((t.strftime("%H:%M") for t in r["Timestamps"]), r["Values"])) for r in res["MetricDataResults"]}
    times = sorted({t for s in series.values() for t in s})
    rows = [{"t": t, **{k: round(v[t], 1) for k, v in series.items() if t in v}} for t in times]
    summary = {k: {"max": round(max(v.values()), 1), "latest": round(v[max(v)], 1)} for k, v in series.items() if v}
    # per-route over the whole window: slowest (max p95) and busiest (total requests, 5xx)
    def route_stats(metric, stat):
        ms = [m for m in cw.list_metrics(Namespace=CW_NAMESPACE, MetricName=metric, Dimensions=[{"Name": "Environment", "Value": environment}])["Metrics"]
              if any(d["Name"] == "Route" and d["Value"] not in ("ALL", "/metrics", "/api/health") for d in m["Dimensions"])][:20]
        if not ms:
            return {}
        rr = cw.get_metric_data(StartTime=start, EndTime=end, MetricDataQueries=[
            {"Id": f"r{i}", "Label": next(d["Value"] for d in m["Dimensions"] if d["Name"] == "Route"),
             "MetricStat": {"Metric": m, "Period": period, "Stat": stat}} for i, m in enumerate(ms)])  # per-period buckets, aggregated below
        return {r["Label"]: (max(r["Values"]) if stat == "Maximum" else sum(r["Values"])) for r in rr["MetricDataResults"] if r["Values"]}
    p95s, reqs, errs = route_stats("LatencyP95", "Maximum"), route_stats("Requests", "Sum"), route_stats("Errors5xx", "Sum")
    slow = sorted(({"route": k, "p95_ms_max": round(v, 1)} for k, v in p95s.items()), key=lambda x: -x["p95_ms_max"])[:5]
    busy = sorted(({"route": k, f"requests_in_{minutes}min": int(v), "errors_5xx": int(errs.get(k, 0))} for k, v in reqs.items()),
                  key=lambda x: -x[f"requests_in_{minutes}min"])[:5]
    if not rows:
        return f"No CloudWatch data in {CW_NAMESPACE} for environment {environment!r} in the last {minutes} min (is akamai-cloudwatch shipping?)."
    return _clip({"source": f"CloudWatch {CW_NAMESPACE} (shipped from Akamai Cloud)", "window_min": minutes, "period_s": period,
                  "note": f"request/error/order counts are totals {per.replace('_', ' ')}, not per second",
                  "summary": summary, "busiest_routes": busy, "slowest_routes_p95": slow, "per_period": rows[-40:]})


@tool
def cloudwatch_alarms(state: str = "") -> str:
    """List CloudWatch alarms (e.g. the ShopLite latency / 5xx alarms) with their current state and reason.

    Args:
        state: Optional filter: ALARM, OK or INSUFFICIENT_DATA (empty = all)
    """
    if integrations.get_row("aws")["status"] == "not_configured":
        return _not_connected("AWS", "aws")
    kw = {"StateValue": state} if state else {}
    alarms = integrations._boto("cloudwatch", None).describe_alarms(AlarmTypes=["MetricAlarm"], **kw)["MetricAlarms"]
    return _clip([{"name": a["AlarmName"], "state": a["StateValue"], "since": a["StateUpdatedTimestamp"].strftime("%Y-%m-%d %H:%M UTC"),
                   "metric": f"{a.get('Namespace')}/{a.get('MetricName')}", "threshold": f"{a.get('ComparisonOperator')} {a.get('Threshold')}",
                   "reason": a.get("StateReason", "")[:200]} for a in alarms] or "No CloudWatch alarms found.")


@tool
def eks_cluster_status() -> str:
    """Describe the EKS cluster configured in Integrations → AWS (status, Kubernetes version, endpoint)."""
    c = integrations.creds("aws")
    if not c.get("eks_cluster"):
        return "No EKS cluster configured (Integrations → AWS → EKS cluster name)."
    cl = integrations._boto("eks", None).describe_cluster(name=c["eks_cluster"])["cluster"]
    return _clip({k: cl.get(k) for k in ("name", "status", "version", "endpoint", "platformVersion", "createdAt")})


def _gh():
    c = integrations.creds("github")
    if not c.get("token") or not c.get("repo"):
        return None, None
    return c["repo"], {"Authorization": f"Bearer {c['token']}", "Accept": "application/vnd.github+json"}


@tool
def github_recent_commits(limit: int = 10) -> str:
    """List the most recent commits on the ShopLite repo's base branch (who changed what, when).

    Args:
        limit: How many commits (max 30)
    """
    repo, h = _gh()
    if not repo:
        return _not_connected("GitHub", "github")
    branch = integrations.creds("github").get("base_branch") or "main"
    r = requests.get(f"https://api.github.com/repos/{repo}/commits", params={"sha": branch, "per_page": min(int(limit), 30)}, headers=h, timeout=TIMEOUT)
    if r.status_code != 200:
        return f"GitHub HTTP {r.status_code}: {r.text[:300]}"
    return _clip([{"sha": c["sha"][:7], "date": c["commit"]["author"]["date"], "author": c["commit"]["author"]["name"],
                   "message": c["commit"]["message"].split("\n")[0]} for c in r.json()])


@tool
def github_commit_diff(sha: str) -> str:
    """Show the files and patch of one commit in the ShopLite repo.

    Args:
        sha: Commit SHA (short or full)
    """
    repo, h = _gh()
    if not repo:
        return _not_connected("GitHub", "github")
    r = requests.get(f"https://api.github.com/repos/{repo}/commits/{sha}", headers=h, timeout=TIMEOUT)
    if r.status_code != 200:
        return f"GitHub HTTP {r.status_code}: {r.text[:300]}"
    c = r.json()
    return _clip({"sha": c["sha"][:7], "message": c["commit"]["message"], "files": [
        {"file": f["filename"], "status": f["status"], "+": f["additions"], "-": f["deletions"], "patch": (f.get("patch") or "")[:1500]} for f in c.get("files", [])]})


@tool
def github_list_files(prefix: str = "") -> str:
    """List the files in the ShopLite repo (base branch), optionally under a folder.

    Args:
        prefix: Folder to list, e.g. "web/public" (empty = whole repo)
    """
    try:
        return _clip(github_ops.list_files(prefix) or f"No files under {prefix!r}")
    except github_ops.GitHubError as e:
        return str(e)


@tool
def github_read_file(path: str, start_line: int = 1, max_lines: int = 150) -> str:
    """Read a file from the ShopLite repo (base branch). Returns the exact text, so you can copy `find` strings from it.

    Args:
        path: File path, e.g. "web/public/index.html"
        start_line: First line to return (1-based)
        max_lines: How many lines to return (max 200)
    """
    try:
        text, _ = github_ops.read_file(path)
    except github_ops.GitHubError as e:
        return str(e)
    lines = text.splitlines()
    s = max(1, int(start_line))
    chunk = lines[s - 1:s - 1 + min(int(max_lines), 200)]
    more = f"\n… lines {s + len(chunk)}–{len(lines)} not shown (call again with start_line)" if s - 1 + len(chunk) < len(lines) else ""
    return f"{path} · lines {s}–{s + len(chunk) - 1} of {len(lines)}\n" + "\n".join(chunk) + more


@tool
def github_search_code(query: str) -> str:
    """Find where text appears in the ShopLite repo (case-insensitive, all text files). Returns file, line number and line.

    Args:
        query: Text to look for, e.g. "<title>" or "ShopLite"
    """
    try:
        return _clip(github_ops.search(query) or f"No matches for {query!r}")
    except github_ops.GitHubError as e:
        return str(e)


def make_propose_code_change(conv, actor, flag):
    @tool
    def propose_code_change(title: str, summary: str, edits: list[dict], assumptions: list[str] | None = None,
                            follow_ups: list[str] | None = None) -> str:
        """Propose a code change to the ShopLite repo for human approval. After approval Sidekick creates a branch,
        commits exactly these edits and opens a pull request with a live preview. Read the files first: each `find` must be copied exactly
        from github_read_file and must occur exactly once in that file.

        Args:
            title: Short PR title, e.g. "Rename store to ShopLite Plus in the page title"
            summary: What changes and why (1-3 sentences); becomes the PR description
            edits: List of {"path": "web/public/index.html", "find": "exact existing text", "replace": "new text"};
                use "find": "" to create a new file with content "replace"
            assumptions: Short list of assumptions you made (e.g. "web only", "reused the existing swipe flag")
            follow_ups: Short list of concrete next steps for the developer (edge cases, tests, polish, related work)
        """
        try:
            prepared = github_ops.prepare_edits(edits)
        except github_ops.GitHubError as e:
            return f"Not proposed: {e}"
        diff = prepared["diff"]
        plan = f"Branch → commit {len(prepared['files'])} file(s) → pull request\n\n{diff[:1500]}"
        db.run("UPDATE approvals SET status = 'rejected', decided_by = 'superseded' WHERE conversation_id = %s AND status = 'pending'", (conv["id"],))
        db.run("INSERT INTO approvals (conversation_id, title, summary, requested_by) VALUES (%s, %s, %s, %s)",
               (conv["id"], f"PR: {title}"[:200], f"{summary}\n\n{diff}"[:4000], actor.get("name") or actor.get("id")))
        change = {"title": title[:200], "summary": summary[:2000], "edits": edits,
                  "requested_by": actor.get("name") or actor.get("id") or f"{actor.get('source') or 'dashboard'} user",
                  "assumptions": [str(a)[:300] for a in (assumptions or [])][:8], "follow_ups": [str(f)[:300] for f in (follow_ups or [])][:8]}
        db.run("UPDATE conversations SET context = context || %s WHERE id = %s",
               (db.J({"code_change": change, "approval_title": f"PR: {title}"[:200], "approval_plan": plan}), conv["id"]))
        flag["requested"] = True
        flag["diff"] = diff
        return ("Proposed. The human sees this diff with Approve / Reject; after approval Sidekick opens the PR. "
                "Do not claim the PR exists yet.\n" + diff[:2000])
    return propose_code_change


def _akamai():
    c = integrations.creds("akamai")
    if not all(c.get(k) for k in ("host", "client_token", "client_secret", "access_token")):
        return None, None
    from akamai.edgegrid import EdgeGridAuth
    s = requests.Session()
    s.auth = EdgeGridAuth(client_token=c["client_token"], client_secret=c["client_secret"], access_token=c["access_token"])
    return s, "https://" + c["host"].replace("https://", "").strip("/")


def _akamai_properties(s, base):
    groups = s.get(f"{base}/papi/v1/groups", timeout=TIMEOUT).json().get("groups", {}).get("items", [])
    out = []
    for g in groups[:10]:
        for contract in g.get("contractIds", [])[:3]:
            props = s.get(f"{base}/papi/v1/properties", params={"contractId": contract, "groupId": g["groupId"]}, timeout=TIMEOUT).json()
            for p in props.get("properties", {}).get("items", []):
                out.append({"name": p["propertyName"], "propertyId": p["propertyId"], "contractId": contract, "groupId": g["groupId"],
                            "latestVersion": p.get("latestVersion"), "productionVersion": p.get("productionVersion"), "stagingVersion": p.get("stagingVersion")})
    return out


@tool
def akamai_list_properties() -> str:
    """List Akamai Property Manager properties (CDN configurations) the API client can see, with their latest versions."""
    s, base = _akamai()
    if not s:
        return _not_connected("Akamai", "akamai")
    return _clip(_akamai_properties(s, base) or "No properties visible to this API client.")


@tool
def akamai_property_caching_rules(property_name: str) -> str:
    """Show the caching-related behaviors (caching TTLs, downstream cache, origin cache-control honoring) in an Akamai property's latest version.

    Args:
        property_name: The property's name, as returned by akamai_list_properties
    """
    s, base = _akamai()
    if not s:
        return _not_connected("Akamai", "akamai")
    p = next((x for x in _akamai_properties(s, base) if x["name"] == property_name), None)
    if not p:
        return f"Property {property_name!r} not found. Use akamai_list_properties to see names."
    rules = s.get(f"{base}/papi/v1/properties/{p['propertyId']}/versions/{p['latestVersion']}/rules",
                  params={"contractId": p["contractId"], "groupId": p["groupId"]}, timeout=TIMEOUT).json()
    found = []

    def walk(rule, path):
        for b in rule.get("behaviors", []):
            if b.get("name") in ("caching", "downstreamCache", "cacheKeyQueryParams", "prefreshCache", "cacheError", "edgeRedirector"):
                found.append({"rule": " / ".join(path) or "default", "behavior": b["name"], "options": b.get("options")})
        for child in rule.get("children", []):
            walk(child, path + [child.get("name", "?")])
    walk(rules.get("rules", {}), [])
    return _clip({"property": property_name, "version": p["latestVersion"], "caching_behaviors": found})


@tool
def infra_inventory() -> str:
    """Everything running on the clouds Sidekick is connected to: Akamai Cloud (VMs, LKE Kubernetes clusters,
    NodeBalancers, firewalls) and AWS (EC2, Lambda, S3, CloudWatch dashboards/alarms, IAM users), plus costs.
    Use for "what's running", "what services do we have", "show our infra"."""
    out = {}
    for name, fn in (("akamai_cloud", inventory.akamai_cloud), ("aws", inventory.aws)):
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            out[name] = {"error": f"{e.__class__.__name__}: {str(e)[:200]}"}
    for k, v in out.items():
        if v.get("configured") is False:
            out[k] = f"not connected (Sidekick → Integrations → {integrations.CATALOG[k]['name']})"
    return _clip(out)


@tool
def infra_costs() -> str:
    """What the infrastructure costs: Akamai Cloud month-to-date usage, monthly run rate and remaining promo credit;
    AWS month-to-date cost by service, rest-of-month forecast and budgets. Use for "what are we spending", "usage costs"."""
    out = {}
    try:
        ac = inventory.akamai_cloud()
        out["akamai_cloud"] = ac["costs"] if ac.get("configured") else "not connected (Integrations → Akamai Cloud)"
    except Exception as e:  # noqa: BLE001
        out["akamai_cloud"] = {"error": str(e)[:200]}
    out["aws"] = inventory.aws_costs()
    if isinstance(out["aws"], dict) and out["aws"].get("configured") is False:
        out["aws"] = "not connected (Integrations → AWS)"
    return _clip(out)


@tool
def sidekick_about() -> str:
    """Who/what Sidekick is: its dashboard URL (where to configure it), where it is hosted, the model it runs on,
    connected integrations, channels and capabilities. Use for "who are you", "where are you from", "how do I configure you"."""
    public = os.getenv("SIDEKICK_PUBLIC_URL", "http://localhost:8090").rstrip("/")
    name = (db.one("SELECT value FROM settings WHERE key = 'agent_name'") or {}).get("value") or "Sidekick"
    connected = [integrations.CATALOG[r["key"]]["name"] for r in db.all("SELECT key FROM integrations WHERE status = 'connected'")
                 if r["key"] in integrations.CATALOG]
    mcp = public.replace("://sidekick.", "://mcp.") + "/mcp" if "://sidekick." in public else None
    return _clip({
        "name": name, "dashboard": public, "hosted_on": os.getenv("SIDEKICK_HOSTING", "Docker (local)"),
        "model": models.label(models.resolve(None)), "connected_integrations": connected,
        "channels": {"slack": "@" + name.lower() + " in Slack", "devrev_mcp": mcp, "dashboard_console": f"{public}/#ask"},
        "configure": {"integrations / keys": f"{public}/#integrations", "model & brain mode": f"{public}/#model",
                      "custom replies": f"{public}/#replies", "access & approvers": f"{public}/#access",
                      "PR previews (kill switch)": f"{public}/#previews", "runs & approvals": f"{public}/#runs"},
        "can_do": ["investigate production (CloudWatch metrics + alarms for ShopLite on Akamai)",
                   "read code and propose PRs with a live preview on Akamai LKE (human approval first)",
                   "file a hand-off GitHub issue for a developer when asked",
                   "inventory and costs across Akamai Cloud and AWS",
                   "answer infra questions with live data"],
        "source": "github.com/romankarki/hackmidwest_2026_sidekick"})


def make_file_review_issue(conv, actor, flag):
    @tool
    def file_review_issue(pr_number: int = 0, notes: str = "", follow_ups: list[str] | None = None, assignee: str = "") -> str:
        """File a GitHub issue that hands a pull request to a developer: review it in the live preview and build on top of
        it (PR + preview links, what changed, assumptions, review checklist, follow-ups, diff; the PR then closes it).
        Call this ONLY when the user asks for it, e.g. "preview looks good, file an issue" / "hand this to a dev".

        Args:
            pr_number: The PR to hand off; 0 = the PR Sidekick opened in this conversation
            notes: What the user said about it, in their words (e.g. "Preview looks good; check it on mobile")
            follow_ups: Extra concrete next steps for the developer (optional; added to the ones from the proposal)
            assignee: GitHub username to assign (optional; default from Integrations → GitHub → Review issue assignee)
        """
        ctx = (db.one("SELECT context FROM conversations WHERE id = %s", (conv["id"],)) or {}).get("context") or {}
        saved = ctx.get("last_pr") or {}
        number = int(pr_number) or int(saved.get("number") or 0)
        if not number:
            return "No PR in this conversation yet: ask for the PR number (e.g. 'file an issue for PR #7')."
        try:
            pr = github_ops.pr_info(number)
        except github_ops.GitHubError as e:
            return f"Couldn't read PR #{number}: {e}"
        if pr["state"] != "open":
            return f"PR #{number} is {pr['state']}; a hand-off issue is for open PRs. Say so if you still want one."
        mine = saved if int(saved.get("number") or 0) == number else {}
        # PRs from other threads: Sidekick's PR body says "**Requested by:** X · **Approved by:** Y"
        who = re.search(r"\*\*Requested by:\*\* (.+?) · \*\*Approved by:\*\* (.+?)\n", pr["body"] + "\n")
        change = {"title": mine.get("title") or pr["title"],
                  "summary": mine.get("summary") or pr["body"].split("\n\n**Requested by")[0][:1500],
                  "assumptions": mine.get("assumptions") or [], "requested_by": mine.get("requested_by") or (who.group(1) if who and who.group(1) != "None" else "?"),
                  "approved_by": mine.get("approved_by") or (who.group(2) if who else "?"), "filed_by": actor.get("name") or actor.get("id") or "the reviewer",
                  "follow_ups": (mine.get("follow_ups") or []) + [str(f)[:300] for f in (follow_ups or [])][:8]}
        preview = mine.get("preview_url") or github_ops.pr_preview_url(number, pr["branch"])
        try:
            issue = github_ops.open_review_issue(change, pr, preview, notes=notes.strip()[:2000], assignee=assignee or None)
        except github_ops.GitHubError as e:
            return f"Couldn't file the issue: {e}"
        assigned = f", assigned to @{issue['assignee']}" if issue["assignee"] else ""
        flag["issue"] = {"number": issue["number"], "url": issue["url"], "pr": number, "assigned": assigned, "preview": preview}
        return f"Filed issue #{issue['number']}{assigned}: {issue['url']} (PR #{number} now closes it on merge). Preview: {preview or 'n/a'}"
    return file_review_issue


def make_request_approval(conv, actor, flag):
    @tool
    def request_approval(title: str, summary: str, plan: str) -> str:
        """Ask a human to approve a specific change. Use this once you have a concrete fix or change to propose.

        Args:
            title: Short title of the change, e.g. "Restore Cache-Control on /api/products"
            summary: The evidence and why this change fixes the problem (2-5 sentences)
            plan: The exact change and rollout, one step per line
        """
        db.run("UPDATE approvals SET status = 'rejected', decided_by = 'superseded' WHERE conversation_id = %s AND status = 'pending'", (conv["id"],))
        db.run("INSERT INTO approvals (conversation_id, title, summary, requested_by) VALUES (%s, %s, %s, %s)",
               (conv["id"], title[:200], f"{summary}\n\nPlan:\n{plan}"[:4000], actor.get("name") or actor.get("id")))
        db.run("UPDATE conversations SET context = context || %s WHERE id = %s",
               (db.J({"approval_title": title[:200], "approval_plan": plan[:2000]}), conv["id"]))
        flag["requested"] = True
        return "Approval requested. A human will see Approve / Reject buttons; do not claim the change is made."
    return request_approval


READ_TOOLS = [shoplite_health, shoplite_route_latency, shoplite_catalog_cache_headers, prometheus_query, cloudwatch_shoplite_overview,
              cloudwatch_alarms, cloudwatch_metric, infra_inventory, infra_costs, sidekick_about,
              eks_cluster_status, github_recent_commits, github_commit_diff, github_list_files, github_read_file, github_search_code,
              akamai_list_properties, akamai_property_caching_rules]
TOOL_INTEGRATION = {"shoplite_health": "shoplite", "shoplite_route_latency": "shoplite", "shoplite_catalog_cache_headers": "shoplite",
                    "prometheus_query": "prometheus", "cloudwatch_metric": "aws", "cloudwatch_shoplite_overview": "aws", "cloudwatch_alarms": "aws", "infra_inventory": "akamai_cloud", "infra_costs": "akamai_cloud", "eks_cluster_status": "aws", "github_recent_commits": "github",
                    "github_commit_diff": "github", "github_list_files": "github", "github_read_file": "github", "github_search_code": "github",
                    "propose_code_change": "github", "file_review_issue": "github", "akamai_list_properties": "akamai", "akamai_property_caching_rules": "akamai"}


# ---------- model ----------
def build_model(r):
    """Strands model for the resolved Model-page settings."""
    if r["provider"] == "akamai_ai":  # OpenAI-compatible Chat Completions on Akamai (vLLM)
        import httpx
        from strands.models.openai import OpenAIModel
        # shared hackathon endpoint: connections can be slow under load, so be patient and retry (default connect timeout is 5s)
        patient = {"timeout": httpx.Timeout(120.0, connect=20.0), "max_retries": 4}
        return OpenAIModel(client_args={**models.openai_client_args("akamai_ai"), **patient}, model_id=r["model_id"],
                           params={"max_tokens": models.request_body(r)["max_tokens"]})
    if r["provider"] == "openai":
        from strands.models.openai_responses import OpenAIResponsesModel
        key = integrations.creds("openai").get("api_key")
        if not key:
            raise RuntimeError("No OpenAI API key — add one in Integrations → OpenAI")
        return OpenAIResponsesModel(client_args={"api_key": key}, model_id=r["model_id"], params={"max_output_tokens": r["max_tokens"]})

    params = {k: v for k, v in models.request_body(r).items() if k not in ("model", "max_tokens")}  # output_config / thinking
    if r["provider"] == "bedrock" and models.get_config().get("bedrock_transport") == "converse":
        import boto3
        from strands.models import BedrockModel
        aws = integrations.creds("aws")
        sess = boto3.Session(aws_access_key_id=aws.get("access_key_id") or None, aws_secret_access_key=aws.get("secret_access_key") or None,
                             aws_session_token=aws.get("session_token") or None, region_name=r["region"])
        return BedrockModel(boto_session=sess, model_id=r["model_id"], max_tokens=r["max_tokens"])

    from strands.models.anthropic import AnthropicModel
    import anthropic
    if r["provider"] == "bedrock":  # Claude in Amazon Bedrock — Messages API via the Mantle client
        model = AnthropicModel(model_id=r["model_id"], max_tokens=r["max_tokens"], params=params)
        aws = integrations.creds("aws")
        kw = {"aws_region": r["region"]}
        if aws.get("access_key_id") and aws.get("secret_access_key"):
            kw.update(aws_access_key=aws["access_key_id"], aws_secret_key=aws["secret_access_key"])
            if aws.get("session_token"):
                kw["aws_session_token"] = aws["session_token"]
        model.client = anthropic.AsyncAnthropicBedrockMantle(**kw)
        return model
    key = integrations.creds("anthropic").get("api_key")
    if not key:
        raise RuntimeError("No Anthropic API key — add one in Integrations → Anthropic API")
    return AnthropicModel(client_args={"api_key": key}, model_id=r["model_id"], max_tokens=r["max_tokens"], params=params)


def _clean_tool_input(raw):
    """A tool input Bedrock accepts: a JSON object whose keys are real argument names."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raw = {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)}


# ---------- live step reporting ----------
class StepReporter(HookProvider):
    """Writes every tool call into runs.steps as it starts / finishes (Slack + dashboard poll this)."""

    def __init__(self, run_id):
        self.run_id, self.steps, self.started = run_id, [], {}

    def register_hooks(self, registry, **kwargs):
        registry.add_callback(BeforeToolCallEvent, self.before)
        registry.add_callback(AfterToolCallEvent, self.after)
        registry.add_callback(BeforeModelCallEvent, self.sanitize_history)

    def _save(self):
        db.run("UPDATE runs SET steps = %s WHERE id = %s", (db.J(self.steps), self.run_id))

    def sanitize_history(self, event):
        """Before every model call: every toolUse.input in the history must be a JSON object with real argument names
        (Bedrock rejects anything else). gpt-oss sometimes emits "", a JSON string, or {"": {}} for argument-less tools."""
        for m in event.agent.messages:
            for c in m.get("content", []):
                tu = c.get("toolUse") if isinstance(c, dict) else None
                if tu is not None:
                    clean = _clean_tool_input(tu.get("input"))
                    if clean != tu.get("input"):
                        print(f"[llm] normalised toolUse.input for {tu.get('name')}: {str(tu.get('input'))[:60]!r} → {clean}", flush=True)
                        tu["input"] = clean

    def before(self, event):
        tu = event.tool_use
        tu["input"] = _clean_tool_input(tu.get("input"))  # see sanitize_history
        args = ", ".join(f"{k}={json.dumps(v)[:60]}" for k, v in (tu.get("input") or {}).items())
        integ = TOOL_INTEGRATION.get(tu["name"])
        self.started[tu["toolUseId"]] = (len(self.steps), time.perf_counter())
        self.steps.append({"tool": tu["name"], "detail": args or "—", "status": "running", "integration": integ,
                           "connected": bool(integ and integrations.connected(integ))})
        self._save()

    def after(self, event):
        i, t0 = self.started.get(event.tool_use["toolUseId"], (None, time.perf_counter()))
        if i is None:
            return
        step = self.steps[i]
        step["secs"] = round(time.perf_counter() - t0, 1)
        res = event.result or {}
        if event.exception or res.get("status") == "error":
            step["status"] = "error"
            msg = str(event.exception) if event.exception else " ".join(c.get("text", "") for c in res.get("content", []))
            step["detail"] += f" → {msg[:120]}"
        else:
            step["status"] = "done"
        self._save()


# ---------- run ----------
MAX_HISTORY = 40
SMALL_CONTEXT_HISTORY = 12  # Akamai inference endpoint: 32k-token context


def _trim(messages, limit=MAX_HISTORY):
    """Keep recent history, cutting only at a plain user turn so tool calls and their results stay paired."""
    if len(messages) <= limit:
        return messages
    for i in range(len(messages) - limit, len(messages)):
        m = messages[i]
        if m.get("role") == "user" and any("text" in c for c in m.get("content", [])) and not any("toolResult" in c for c in m.get("content", [])):
            return messages[i:]
    return messages[-2:]


def run(run_id, conv, text, actor):
    """Execute one LLM turn for a conversation. Returns (reply_text, approval_requested)."""
    r = models.resolve(None)
    name = (db.one("SELECT value FROM settings WHERE key = 'agent_name'") or {}).get("value") or "Sidekick"
    system = SYSTEM_PROMPT.format(agent_name=name, status=conv["status"], user=actor.get("name") or actor.get("id") or "someone")
    brief = github_ops.repo_brief()  # the target repo's SIDEKICK.md: stack, file map, feature flags, conventions
    if brief:
        system += f"\n\n## Repository brief (SIDEKICK.md in {integrations.creds('github').get('repo')})\n{brief}"
    limit = SMALL_CONTEXT_HISTORY if r["provider"] == "akamai_ai" else MAX_HISTORY
    history = _trim((conv.get("context") or {}).get("llm_messages") or [], limit)
    flag = {"requested": False}

    agent = Agent(model=build_model(r), system_prompt=system, messages=history,
                  tools=READ_TOOLS + [make_request_approval(conv, actor, flag), make_propose_code_change(conv, actor, flag),
                                        make_file_review_issue(conv, actor, flag)], hooks=[StepReporter(run_id)], callback_handler=None)
    reply = str(agent(text)).strip() or "(no text reply)"

    db.run("UPDATE conversations SET context = context || %s, updated_at = now() WHERE id = %s",
           (db.J({"llm_messages": json.loads(json.dumps(_trim(agent.messages, limit), default=str))}), conv["id"]))
    if flag.get("issue"):  # always link the hand-off issue, whatever the model wrote
        i = flag["issue"]
        reply += f"\n\n📝 *Hand-off issue #{i['number']}*{i['assigned']}: {i['url']}\n• PR #{i['pr']}" + (f" · preview {i['preview']}" if i["preview"] else "")
    if flag.get("diff"):  # always show the exact diff the approver is approving, whatever the model wrote
        reply = ("📝 *Proposed, not applied yet.* *Approve* opens a pull request with a live preview; "
                 "nothing is merged or deployed.\n\n" + reply +
                 f"\n\n*Proposed change:*\n```{flag['diff'][:2500]}```")
    return reply, flag["requested"]
