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
Tools only READ. The one action tool, `request_approval`, creates a pending approval; shipping still goes through
the human Approve button and the configured workflow.
"""
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
from strands import Agent, tool
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider

from . import db, integrations, models

TIMEOUT = 10
MAX_TOOL_TEXT = 6000  # keep tool results small enough for the model's context

SYSTEM_PROMPT = """You are {agent_name}, an operations and developer agent for the ShopLite e-commerce app.
You talk to engineers in Slack, Microsoft Teams, DevRev and a web dashboard.

How to work:
- Investigate with your tools before concluding. Prefer real data (metrics, headers, deploy history, edge config) over guesses.
- If a tool says an integration isn't connected, say which one and continue with what you can check.
- Be concise and concrete: lead with the answer, then the evidence (numbers, commit SHAs, header values), then next steps.
- You can READ infrastructure and code. You cannot change anything yourself. When you have a specific fix or change to
  propose, call `request_approval` with a short title, a summary of the evidence and the exact plan. A human approves it
  and the configured workflow ships it. Never claim a change was made.
- Format for Slack mrkdwn: *bold*, _italic_, `code`, ```code blocks```, and bullet lines starting with "• ".

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
        namespace: e.g. AWS/EC2, AWS/RDS, AWS/Kinesis, ContainerInsights
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


READ_TOOLS = [shoplite_health, shoplite_route_latency, shoplite_catalog_cache_headers, prometheus_query, cloudwatch_metric,
              eks_cluster_status, github_recent_commits, github_commit_diff, akamai_list_properties, akamai_property_caching_rules]
TOOL_INTEGRATION = {"shoplite_health": "shoplite", "shoplite_route_latency": "shoplite", "shoplite_catalog_cache_headers": "shoplite",
                    "prometheus_query": "prometheus", "cloudwatch_metric": "aws", "eks_cluster_status": "aws", "github_recent_commits": "github",
                    "github_commit_diff": "github", "akamai_list_properties": "akamai", "akamai_property_caching_rules": "akamai"}


# ---------- model ----------
def build_model(r):
    """Strands model for the resolved Model-page settings."""
    if r["provider"] == "akamai_ai":  # OpenAI-compatible Chat Completions on Akamai (vLLM)
        from strands.models.openai import OpenAIModel
        return OpenAIModel(client_args=models.openai_client_args("akamai_ai"), model_id=r["model_id"],
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


# ---------- live step reporting ----------
class StepReporter(HookProvider):
    """Writes every tool call into runs.steps as it starts / finishes (Slack + dashboard poll this)."""

    def __init__(self, run_id):
        self.run_id, self.steps, self.started = run_id, [], {}

    def register_hooks(self, registry, **kwargs):
        registry.add_callback(BeforeToolCallEvent, self.before)
        registry.add_callback(AfterToolCallEvent, self.after)

    def _save(self):
        db.run("UPDATE runs SET steps = %s WHERE id = %s", (db.J(self.steps), self.run_id))

    def before(self, event):
        tu = event.tool_use
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
    limit = SMALL_CONTEXT_HISTORY if r["provider"] == "akamai_ai" else MAX_HISTORY
    history = _trim((conv.get("context") or {}).get("llm_messages") or [], limit)
    flag = {"requested": False}

    agent = Agent(model=build_model(r), system_prompt=system, messages=history,
                  tools=READ_TOOLS + [make_request_approval(conv, actor, flag)], hooks=[StepReporter(run_id)], callback_handler=None)
    reply = str(agent(text)).strip() or "(no text reply)"

    db.run("UPDATE conversations SET context = context || %s, updated_at = now() WHERE id = %s",
           (db.J({"llm_messages": json.loads(json.dumps(_trim(agent.messages, limit), default=str))}), conv["id"]))
    return reply, flag["requested"]
