"""Integration catalog + real connection tests.

Each integration has plain `config` fields and `secret` fields (encrypted at rest, never returned).
`test()` makes a real, read-only API call and returns (ok, detail).
"""
from datetime import datetime, timezone

import requests

from . import db
from .crypto import seal, unseal

TIMEOUT = 8

# key: name, group, color, abbr, description, uses (what Sidekick does with it), fields
# field: k, label, type (text | secret | select), placeholder, help, options, default
CATALOG = {
    "aws": {
        "name": "Amazon Web Services", "group": "Cloud & infrastructure", "color": "#ff9900", "abbr": "AWS",
        "description": "EKS, EC2, S3, Kinesis, RDS, CloudWatch — read metrics/logs, apply infra changes.",
        "uses": ["sts", "eks", "cloudwatch", "ec2", "s3", "kinesis", "rds"],
        "fields": [
            {"k": "region", "label": "Region", "type": "text", "default": "ap-south-1", "placeholder": "ap-south-1"},
            {"k": "access_key_id", "label": "Access key ID", "type": "secret", "placeholder": "AKIA…", "help": "Leave both keys empty to use the default credential chain (env, ~/.aws, IAM role / IRSA)."},
            {"k": "secret_access_key", "label": "Secret access key", "type": "secret"},
            {"k": "session_token", "label": "Session token (optional)", "type": "secret"},
            {"k": "eks_cluster", "label": "EKS cluster name (optional)", "type": "text", "placeholder": "shop-prod"},
        ],
    },
    "openai": {
        "name": "OpenAI", "group": "AI runtime", "color": "#10a37f", "abbr": "OA",
        "description": "Use OpenAI models instead of Claude. Pick the provider and model in Configure → Model.",
        "uses": ["responses", "models"],
        "fields": [
            {"k": "api_key", "label": "API key", "type": "secret", "placeholder": "sk-…", "help": "Only needed when the Model provider is “OpenAI”."},
        ],
    },
    "akamai_ai": {
        "name": "Akamai AI inference", "group": "AI runtime", "color": "#0096d6", "abbr": "AI",
        "description": "OpenAI-compatible model endpoint on Akamai (e.g. the hackathon workshop). Pick “Akamai AI inference” in Configure → Model.",
        "uses": ["chat.completions", "models"],
        "fields": [
            {"k": "base_url", "label": "Base URL", "type": "text", "placeholder": "https://ws.akamai-workshops.com/v1", "help": "Ends in /v1."},
            {"k": "api_key", "label": "API key", "type": "secret", "placeholder": "hk_…", "help": "Only needed when the Model provider is “Akamai AI inference”."},
        ],
    },
    "anthropic": {
        "name": "Anthropic API", "group": "AI runtime", "color": "#d97757", "abbr": "AN",
        "description": "Call Claude directly instead of through Bedrock. Pick the provider and model in Configure → Model.",
        "uses": ["messages", "models"],
        "fields": [
            {"k": "api_key", "label": "API key", "type": "secret", "placeholder": "sk-ant-…", "help": "Only needed when the Model provider is “Anthropic API”. Bedrock uses the AWS credentials."},
        ],
    },
    "akamai": {
        "name": "Akamai", "group": "Edge / CDN", "color": "#0096d6", "abbr": "AK",
        "description": "Property Manager + reporting — cache rules, edge config, activation on staging/production.",
        "uses": ["papi", "reporting"],
        "fields": [
            {"k": "host", "label": "API host", "type": "text", "placeholder": "akab-xxxx.luna.akamaiapis.net", "help": "From your .edgerc (EdgeGrid API client)."},
            {"k": "client_token", "label": "Client token", "type": "secret"},
            {"k": "client_secret", "label": "Client secret", "type": "secret"},
            {"k": "access_token", "label": "Access token", "type": "secret"},
            {"k": "property", "label": "Property name (optional)", "type": "text", "placeholder": "shoplite.com"},
        ],
    },
    "github": {
        "name": "GitHub", "group": "Code & delivery", "color": "#24292f", "abbr": "GH",
        "description": "Read deploy history and diffs, create branches and pull requests.",
        "uses": ["repos", "pulls", "contents"],
        "fields": [
            {"k": "token", "label": "Personal access token", "type": "secret", "placeholder": "github_pat_… / ghp_…", "help": "Fine-grained PAT with Contents + Pull requests read/write on the repo."},
            {"k": "repo", "label": "Repository", "type": "text", "placeholder": "owner/shoplite"},
            {"k": "base_branch", "label": "Base branch", "type": "text", "default": "main"},
        ],
    },
    "shoplite": {
        "name": "ShopLite (target app)", "group": "Code & delivery", "color": "#2fb6a6", "abbr": "SL",
        "description": "The app Sidekick operates. Used to check health and link preview environments.",
        "uses": ["/api/health", "/metrics"],
        "fields": [
            {"k": "url", "label": "App URL", "type": "text", "default": "http://host.docker.internal:8080", "placeholder": "http://localhost:8080"},
            {"k": "preview_url_template", "label": "Preview URL template", "type": "text", "default": "http://localhost:{port}", "placeholder": "https://{branch}.preview.shoplite.dev"},
            {"k": "pr_preview_url_template", "label": "PR preview URL template (optional)", "type": "text", "placeholder": "https://{label}.<lke-nodebalancer>.sslip.io",
             "help": "Where the repo's PR-preview workflow serves pull requests ({label} = pr-<number>). Empty = use the Preview URL template."},
        ],
    },
    "prometheus": {
        "name": "Prometheus", "group": "Observability", "color": "#e6522c", "abbr": "PR",
        "description": "Query latency, error rate and saturation metrics.",
        "uses": ["query", "query_range"],
        "fields": [
            {"k": "url", "label": "URL", "type": "text", "placeholder": "http://prometheus:9090"},
            {"k": "token", "label": "Bearer token (optional)", "type": "secret"},
        ],
    },
    "grafana": {
        "name": "Grafana", "group": "Observability", "color": "#f46800", "abbr": "GF",
        "description": "Link dashboards in answers and add deploy annotations.",
        "uses": ["search", "annotations"],
        "fields": [
            {"k": "url", "label": "URL", "type": "text", "placeholder": "https://grafana.example.com"},
            {"k": "token", "label": "Service account token", "type": "secret"},
        ],
    },
    "slack": {
        "name": "Slack", "group": "Where people talk to Sidekick", "color": "#4a154b", "abbr": "SL",
        "description": "The Sidekick Slack app (Socket Mode). Status comes from the bot's heartbeat — tokens live in slack-bot/.env.",
        "uses": ["app_mention", "chat:write", "interactivity"],
        "fields": [
            {"k": "default_channel", "label": "Default channel for notifications (optional)", "type": "text", "placeholder": "#checkout-oncall"},
        ],
    },
    "devrev": {
        "name": "DevRev", "group": "Where people talk to Sidekick", "color": "#111111", "abbr": "DR",
        "description": "Read tickets & conversations, post updates, resolve tickets.",
        "uses": ["works", "timeline-entries"],
        "fields": [
            {"k": "pat", "label": "Personal access token", "type": "secret", "help": "DevRev → Settings → Account → Personal Access Token."},
            {"k": "part", "label": "Product / part (optional)", "type": "text", "placeholder": "ShopLite"},
        ],
    },
    "teams": {
        "name": "Microsoft Teams", "group": "Where people talk to Sidekick", "color": "#464eb8", "abbr": "TM",
        "description": "Coming soon.", "uses": ["bot"], "fields": [], "disabled": True,
    },
}


# ---------- storage ----------
def get_row(key):
    return db.one("SELECT * FROM integrations WHERE key = %s", (key,)) or {"key": key, "config": {}, "secrets": None, "status": "not_configured",
                                                                       "status_detail": None, "last_tested_at": None}


def creds(key):
    """config + decrypted secrets, for use by tests (and later by Strands tools)."""
    row = get_row(key)
    return {**row["config"], **unseal(row["secrets"])}


def public(key, row=None):
    row = row or get_row(key)
    meta = CATALOG[key]
    secrets = unseal(row["secrets"])
    fields = [{**f, "set": bool(secrets.get(f["k"]))} if f["type"] == "secret" else f for f in meta["fields"]]
    status = row["status"]
    if key == "slack":
        status, detail = _slack_status()
        row = {**row, "status_detail": detail}
    return {"key": key, **{k: v for k, v in meta.items() if k != "fields"}, "fields": fields, "config": row["config"],
            "status": status, "status_detail": row["status_detail"], "last_tested_at": row["last_tested_at"]}


def save(key, values: dict):
    meta = CATALOG[key]
    row = get_row(key)
    config, secrets = dict(row["config"]), unseal(row["secrets"])
    for f in meta["fields"]:
        if f["k"] not in values:
            continue
        v = (values[f["k"]] or "").strip() if isinstance(values[f["k"]], str) else values[f["k"]]
        if f["type"] == "secret":
            if v == "__clear__":
                secrets.pop(f["k"], None)
            elif v:  # empty = keep existing secret
                secrets[f["k"]] = v
        else:
            config[f["k"]] = v
    has_any = any(config.values()) or any(secrets.values())
    db.run("""INSERT INTO integrations (key, config, secrets, status, status_detail, updated_at) VALUES (%s, %s, %s, %s, NULL, now())
              ON CONFLICT (key) DO UPDATE SET config = EXCLUDED.config, secrets = EXCLUDED.secrets, status = EXCLUDED.status,
              status_detail = NULL, updated_at = now()""",
           (key, db.J(config), seal(secrets), "configured" if has_any else "not_configured"))
    return public(key)


def disconnect(key):
    db.run("DELETE FROM integrations WHERE key = %s", (key,))
    return public(key)


def test(key):
    fn = TESTS.get(key)
    if not fn:
        return public(key)
    try:
        ok, detail = fn(creds(key))
    except requests.RequestException as e:
        ok, detail = False, f"Network error: {e.__class__.__name__}: {str(e)[:160]}"
    except Exception as e:  # noqa: BLE001 — surface any SDK error to the user
        ok, detail = False, f"{e.__class__.__name__}: {str(e)[:200]}"
    if key != "slack":
        db.run("""INSERT INTO integrations (key, status, status_detail, last_tested_at) VALUES (%s, %s, %s, now())
                  ON CONFLICT (key) DO UPDATE SET status = EXCLUDED.status, status_detail = EXCLUDED.status_detail, last_tested_at = now()""",
               (key, "connected" if ok else "error", detail))
    return {**public(key), "test": {"ok": ok, "detail": detail}}


def connected(key) -> bool:
    if key == "slack":
        return _slack_status()[0] == "connected"
    return get_row(key)["status"] == "connected"


# ---------- tests (real, read-only calls) ----------
def _boto(service, c, region=None):
    import boto3
    aws = creds("aws")
    kw = {"region_name": region or aws.get("region") or "us-east-1"}
    if aws.get("access_key_id") and aws.get("secret_access_key"):
        kw.update(aws_access_key_id=aws["access_key_id"], aws_secret_access_key=aws["secret_access_key"])
        if aws.get("session_token"):
            kw["aws_session_token"] = aws["session_token"]
    return boto3.client(service, **kw)


def _test_aws(c):
    ident = _boto("sts", c).get_caller_identity()
    detail = f"Account {ident['Account']} · {ident['Arn'].split(':', 5)[-1]}"
    if c.get("eks_cluster"):
        cl = _boto("eks", c).describe_cluster(name=c["eks_cluster"])["cluster"]
        detail += f" · EKS {cl['name']} {cl['status']} (k8s {cl['version']})"
    return True, detail


def _test_anthropic(c):
    import anthropic
    if not c.get("api_key"):
        return False, "Missing API key"
    try:
        models = list(anthropic.Anthropic(api_key=c["api_key"], timeout=TIMEOUT, max_retries=0).models.list(limit=20))
    except anthropic.AuthenticationError:
        return False, "Invalid API key (401)"
    except anthropic.APIStatusError as e:
        return False, f"HTTP {e.status_code}: {e.message}"
    except anthropic.APIConnectionError as e:
        return False, f"Connection error: {e}"
    return True, f"Key valid · {len(models)} models available (e.g. {', '.join(m.id for m in models[:3])})"


def _test_akamai(c):
    from akamai.edgegrid import EdgeGridAuth
    for k in ("host", "client_token", "client_secret", "access_token"):
        if not c.get(k):
            return False, f"Missing {k.replace('_', ' ')}"
    s = requests.Session()
    s.auth = EdgeGridAuth(client_token=c["client_token"], client_secret=c["client_secret"], access_token=c["access_token"])
    host = c["host"].replace("https://", "").strip("/")
    r = s.get(f"https://{host}/papi/v1/groups", timeout=TIMEOUT)
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:160]}"
    groups = r.json().get("groups", {}).get("items", [])
    return True, f"Property Manager API OK · {len(groups)} group(s): {', '.join(g['groupName'] for g in groups[:3])}"


def _test_github(c):
    if not c.get("token"):
        return False, "Missing token"
    h = {"Authorization": f"Bearer {c['token']}", "Accept": "application/vnd.github+json"}
    r = requests.get("https://api.github.com/user", headers=h, timeout=TIMEOUT)
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.json().get('message', r.text[:120])}"
    detail = f"Signed in as {r.json()['login']}"
    if c.get("repo"):
        rr = requests.get(f"https://api.github.com/repos/{c['repo']}", headers=h, timeout=TIMEOUT)
        if rr.status_code != 200:
            return False, f"{detail}, but repo {c['repo']}: HTTP {rr.status_code} {rr.json().get('message', '')}"
        perms = rr.json().get("permissions", {})
        detail += f" · {c['repo']} ({'push' if perms.get('push') else 'read-only'} access)"
    return True, detail


def _test_shoplite(c):
    url = (c.get("url") or "").rstrip("/")
    if not url:
        return False, "Missing URL"
    h = requests.get(f"{url}/api/health", timeout=TIMEOUT).json()
    return h.get("status") == "ok", f"ShopLite {h.get('version')} on branch {h.get('branch')}"


def _test_prometheus(c):
    url = (c.get("url") or "").rstrip("/")
    if not url:
        return False, "Missing URL"
    h = {"Authorization": f"Bearer {c['token']}"} if c.get("token") else {}
    r = requests.get(f"{url}/api/v1/status/buildinfo", headers=h, timeout=TIMEOUT)
    return r.status_code == 200, (f"Prometheus {r.json()['data']['version']}" if r.status_code == 200 else f"HTTP {r.status_code}")


def _test_grafana(c):
    url = (c.get("url") or "").rstrip("/")
    if not url or not c.get("token"):
        return False, "Missing URL or token"
    r = requests.get(f"{url}/api/search?limit=1", headers={"Authorization": f"Bearer {c['token']}"}, timeout=TIMEOUT)
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    v = requests.get(f"{url}/api/health", timeout=TIMEOUT).json().get("version", "?")
    return True, f"Grafana {v} · token valid"


def _test_devrev(c):
    if not c.get("pat"):
        return False, "Missing personal access token"
    r = requests.get("https://api.devrev.ai/dev-users.self", headers={"Authorization": c["pat"]}, timeout=TIMEOUT)
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:160]}"
    u = r.json().get("dev_user", {})
    return True, f"Signed in as {u.get('display_name') or u.get('full_name') or u.get('email')}"


def _slack_status():
    hb = db.one("SELECT value FROM settings WHERE key = 'slack_bot'")
    if not hb:
        return "not_configured", "Bot has never connected — start slack-bot"
    v = hb["value"]
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(v["at"])).total_seconds()
    if age > 90:
        return "error", f"Bot offline (last heartbeat {int(age // 60)} min ago)"
    return "connected", f"Bot online as @{v.get('bot_user')} in {v.get('team')}"


def _test_slack(c):
    status, detail = _slack_status()
    return status == "connected", detail


def _test_openai(c):
    import openai
    if not c.get("api_key"):
        return False, "Missing API key"
    try:
        ids = [m.id for m in openai.OpenAI(api_key=c["api_key"], timeout=TIMEOUT, max_retries=0).models.list()]
    except openai.AuthenticationError:
        return False, "Invalid API key (401)"
    except openai.APIStatusError as e:
        return False, f"HTTP {e.status_code}: {e.message}"
    except openai.APIConnectionError as e:
        return False, f"Connection error: {e}"
    return True, f"Key valid · {len(ids)} models available"


def _test_akamai_ai(c):
    import openai
    if not c.get("api_key") or not c.get("base_url"):
        return False, "Missing base URL or API key"
    try:
        ids = [m.id for m in openai.OpenAI(api_key=c["api_key"], base_url=c["base_url"].rstrip("/"), timeout=TIMEOUT, max_retries=0).models.list()]
    except openai.AuthenticationError:
        return False, "Invalid API key (401)"
    except openai.APIStatusError as e:
        return False, f"HTTP {e.status_code}: {e.message}"
    except openai.APIConnectionError as e:
        return False, f"Connection error: {e}"
    return True, f"Key valid · models: {', '.join(ids)}"


TESTS = {"akamai_ai": _test_akamai_ai, "aws": _test_aws, "anthropic": _test_anthropic, "openai": _test_openai, "akamai": _test_akamai, "github": _test_github, "shoplite": _test_shoplite,
         "prometheus": _test_prometheus, "grafana": _test_grafana, "devrev": _test_devrev, "slack": _test_slack}
