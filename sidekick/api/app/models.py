"""Which LLM Sidekick uses — provider, model, effort — resolved per skill.

This is the single source of truth the Strands agent will read (`resolve(skill)` → provider, model ID,
region, effort, max_tokens). Today it's used to label runs and for a real "Test model" call.

Providers: Claude via Amazon Bedrock or the Anthropic API, or OpenAI (models listed live from the account).
Model IDs / clients follow the providers' docs:
  • Amazon Bedrock: `AnthropicBedrockMantle(aws_region=…)`, IDs carry an `anthropic.` prefix (anthropic.claude-opus-5)
  • Anthropic API:  `Anthropic(api_key=…)`, bare IDs (claude-opus-5)
  • OpenAI:         `OpenAI(api_key=…).responses.create(model, input, max_output_tokens)`
  • Akamai AI inference (OpenAI-compatible, vLLM): `OpenAI(api_key=…, base_url=…).chat.completions.create(model, messages, max_tokens)`
    — Chat Completions only (no Responses API), 32k context, so replies are capped at AKAMAI_MAX_TOKENS
Thinking/effort differ per model: Haiku 4.5 takes no `effort`; Opus 4.8 needs adaptive thinking set explicitly;
Opus 5 / Sonnet 5 / Opus 5.5 / Fable 5.1 think adaptively by default. No temperature on current models.
"""
import time

from . import db, integrations

PROVIDERS = {
    "bedrock": {"name": "Amazon Bedrock", "client": "AnthropicBedrockMantle", "auth": "AWS credentials from Integrations → AWS (or the default AWS credential chain)", "family": "claude"},
    "anthropic": {"name": "Anthropic API", "client": "Anthropic", "auth": "API key from Integrations → Anthropic API", "family": "claude"},
    "openai": {"name": "OpenAI", "client": "OpenAI", "auth": "API key from Integrations → OpenAI · models load from your account", "family": "openai"},
    "akamai_ai": {"name": "Akamai AI inference", "client": "OpenAI(base_url=…)", "auth": "API key + base URL from Integrations → Akamai AI inference · OpenAI-compatible Chat Completions", "family": "openai"},
}
AKAMAI_MAX_TOKENS = 4096  # the endpoint's context is 32k tokens shared by prompt, tools, history and reply
_model_cache = {}  # provider → {"at", "key", "models"}

MODELS = [
    {"id": "claude-opus-5", "name": "Claude Opus 5", "note": "Default — strong all-rounder for agentic work", "effort": True, "default_effort": "high"},
    {"id": "claude-sonnet-5", "name": "Claude Sonnet 5", "note": "Faster and cheaper", "effort": True, "default_effort": "high"},
    {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5", "note": "Fastest and cheapest — no effort control", "effort": False, "default_effort": None},
    {"id": "claude-opus-5-5", "name": "Claude Opus 5.5", "note": "Newest Opus — effort defaults to medium", "effort": True, "default_effort": "medium"},
    {"id": "claude-fable-5-1", "name": "Claude Fable 5.1", "note": "Most capable — highest cost, longer turns", "effort": True, "default_effort": "high"},
    {"id": "claude-opus-4-8", "name": "Claude Opus 4.8", "note": "Previous-generation Opus", "effort": True, "default_effort": "high"},
]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
DEFAULT = {"provider": "bedrock", "model": "claude-opus-5", "custom_model": "", "region": "us-east-1", "effort": "high", "max_tokens": 16000, "skills": {},
           "bedrock_transport": "messages"}  # messages = Claude in Bedrock Messages API (Mantle) · converse = Bedrock Converse API


def get_config():
    row = db.one("SELECT value FROM settings WHERE key = 'model_config'")
    return {**DEFAULT, **(row["value"] if row else {})}


def save_config(cfg):
    clean = {**DEFAULT, **{k: v for k, v in cfg.items() if k in DEFAULT}}
    clean["skills"] = {k: {"model": (v or {}).get("model") or "", "effort": (v or {}).get("effort") or ""}
                       for k, v in (clean.get("skills") or {}).items() if v and ((v.get("model") or v.get("effort")))}
    db.run("INSERT INTO settings (key, value) VALUES ('model_config', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (db.J(clean),))
    return clean


def _meta(model):
    return next((m for m in MODELS if m["id"] == model), None)


def family(provider):
    return PROVIDERS[provider]["family"]


def valid_model(provider, model):
    if model == "custom":
        return True
    if family(provider) == "claude":
        return _meta(model) is not None
    return bool(model) and _meta(model) is None  # OpenAI: any ID from the account list (Claude IDs don't belong here)


def model_id(provider, model, custom=""):
    if model == "custom":
        return custom.strip()
    return f"anthropic.{model}" if provider == "bedrock" else model


def resolve(skill=None, cfg=None):
    """Effective settings for a skill (skill override → default)."""
    cfg = cfg or get_config()
    o = (cfg.get("skills") or {}).get(skill or "", {})
    if o.get("model") and not valid_model(cfg["provider"], o["model"]):
        o = {**o, "model": ""}  # override from another provider family — ignore
    model = o.get("model") or cfg["model"]
    meta = _meta(model)
    effort = o.get("effort") or cfg["effort"]
    if family(cfg["provider"]) == "openai" or (meta and not meta["effort"]):
        effort = None  # Haiku 4.5 rejects `effort`; OpenAI reasoning effort isn't configured here yet
    mid = model_id(cfg["provider"], model, cfg.get("custom_model", ""))
    return {"provider": cfg["provider"], "model": model, "model_id": mid,
            "name": meta["name"] if meta else mid, "region": cfg["region"] if cfg["provider"] == "bedrock" else None,
            "effort": effort, "max_tokens": int(cfg["max_tokens"]), "skill": skill, "overridden": bool(o.get("model") or o.get("effort"))}


def openai_client_args(provider="openai"):
    """api_key (+ base_url for OpenAI-compatible endpoints) from the provider's integration card."""
    c = integrations.creds(provider)
    if not c.get("api_key"):
        raise RuntimeError(f"No API key — add one in Integrations → {PROVIDERS[provider]['name']}")
    args = {"api_key": c["api_key"]}
    if provider == "akamai_ai":
        args["base_url"] = (c.get("base_url") or "").strip().rstrip("/")
        if not args["base_url"]:
            raise RuntimeError("No base URL — add it in Integrations → Akamai AI inference")
    return args


def openai_models(force=False, provider="openai"):
    """Model IDs the configured key can use (cached 10 min). Returns (ids, error)."""
    import openai
    name = PROVIDERS[provider]["name"]
    try:
        args = openai_client_args(provider)
    except RuntimeError as e:
        return [], f"{e} to load models"
    cache = _model_cache.get(provider) or {}
    if not force and cache.get("key") == args and time.time() - cache.get("at", 0) < 600:
        return cache["models"], None
    try:
        ids = sorted(m.id for m in openai.OpenAI(**args, timeout=15.0, max_retries=0).models.list())
    except openai.AuthenticationError:
        return [], f"{name} rejected the API key (401)"
    except openai.APIStatusError as e:
        return [], f"{name} HTTP {e.status_code}: {e.message}"
    except openai.APIConnectionError as e:
        return [], f"Couldn't reach {name}: {e}"
    if provider == "akamai_ai":  # the endpoint also serves image + embedding models; only chat models can run the agent
        ids = [i for i in ids if "image" not in i and "embedding" not in i]
    _model_cache[provider] = {"at": time.time(), "key": args, "models": ids}
    return ids, None


def request_body(r):
    """The request the agent will send (minus messages/tools/system)."""
    if r["provider"] == "akamai_ai":
        return {"model": r["model_id"], "max_tokens": min(r["max_tokens"], AKAMAI_MAX_TOKENS)}  # Chat Completions
    if family(r["provider"]) == "openai":
        return {"model": r["model_id"], "max_output_tokens": r["max_tokens"]}  # OpenAI Responses API
    body = {"model": r["model_id"], "max_tokens": r["max_tokens"]}
    if r["effort"]:
        body["output_config"] = {"effort": r["effort"]}
    if r["model"] == "claude-opus-4-8":
        body["thinking"] = {"type": "adaptive"}  # Opus 4.8 runs without thinking unless asked
    return body


def label(r):
    return f"{r['name'] or 'no model selected'} · {PROVIDERS[r['provider']]['name']}" + (f" · effort {r['effort']}" if r["effort"] else "")


def _client(r):
    import anthropic
    if r["provider"] == "bedrock":
        aws = integrations.creds("aws")
        kw = {"aws_region": r["region"]}
        if aws.get("access_key_id") and aws.get("secret_access_key"):
            kw.update(aws_access_key=aws["access_key_id"], aws_secret_key=aws["secret_access_key"])
            if aws.get("session_token"):
                kw["aws_session_token"] = aws["session_token"]
        return anthropic.AnthropicBedrockMantle(timeout=60.0, max_retries=0, **kw)
    key = integrations.creds("anthropic").get("api_key")
    if not key:
        raise RuntimeError("No Anthropic API key — add one in Integrations → Anthropic API")
    return anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=0)


def _test_openai(r):
    import openai
    try:
        args = openai_client_args(r["provider"])
    except RuntimeError as e:
        return False, {"error": str(e)}
    t0 = time.perf_counter()
    try:
        client = openai.OpenAI(**args, timeout=60.0, max_retries=0)
        if r["provider"] == "akamai_ai":  # Chat Completions only
            resp = client.chat.completions.create(model=r["model_id"], max_tokens=256,
                                                  messages=[{"role": "user", "content": "Reply with exactly: Sidekick model check OK"}])
            u = resp.usage
            return True, {"text": resp.choices[0].message.content, "model": resp.model, "latency_ms": round((time.perf_counter() - t0) * 1000),
                          "stop_reason": resp.choices[0].finish_reason, "input_tokens": u.prompt_tokens if u else 0,
                          "output_tokens": u.completion_tokens if u else 0, "label": label(r)}
        resp = client.responses.create(
            model=r["model_id"], input="Reply with exactly: Sidekick model check OK", max_output_tokens=1024)
    except openai.AuthenticationError as e:
        return False, {"error": f"Authentication failed (401): {e.message}"}
    except openai.PermissionDeniedError as e:
        return False, {"error": f"Permission denied (403): {e.message}"}
    except openai.NotFoundError as e:
        return False, {"error": f"Model not found (404): {r['model_id']} — {e.message}"}
    except openai.RateLimitError as e:
        return False, {"error": f"Rate limited / quota (429): {e.message}"}
    except openai.APIStatusError as e:
        return False, {"error": f"HTTP {e.status_code}: {e.message}"}
    except openai.APIConnectionError as e:
        return False, {"error": f"Connection error: {e}"}
    ms = round((time.perf_counter() - t0) * 1000)
    u = resp.usage
    return True, {"text": resp.output_text, "model": resp.model, "latency_ms": ms, "stop_reason": resp.status,
                  "input_tokens": u.input_tokens if u else 0, "output_tokens": u.output_tokens if u else 0, "label": label(r)}


def _test_converse(r):
    """Bedrock Converse API (bedrock-runtime), e.g. with a cross-region inference profile like us.anthropic.claude-haiku-4-5-…
    Some accounts can only reach Claude this way: the Messages API (Mantle) answers 'not available for this account'."""
    from botocore.exceptions import BotoCoreError, ClientError
    t0 = time.perf_counter()
    try:
        resp = integrations._boto("bedrock-runtime", None, region=r["region"]).converse(
            modelId=r["model_id"], messages=[{"role": "user", "content": [{"text": "Reply with exactly: Sidekick model check OK"}]}],
            inferenceConfig={"maxTokens": 256})
    except ClientError as e:
        err = e.response.get("Error", {})
        return False, {"error": f"{err.get('Code', 'Error')}: {err.get('Message', str(e))}"}
    except BotoCoreError as e:
        return False, {"error": f"{e.__class__.__name__}: {e}"}
    text = next((c["text"] for c in resp["output"]["message"]["content"] if "text" in c), "")
    u = resp.get("usage", {})
    return True, {"text": text, "model": r["model_id"], "latency_ms": round((time.perf_counter() - t0) * 1000), "stop_reason": resp.get("stopReason"),
                  "input_tokens": u.get("inputTokens", 0), "output_tokens": u.get("outputTokens", 0), "label": label(r) + " · Converse"}


def test(skill=None):
    """One real, tiny request with the resolved settings. Returns (ok, details)."""
    import anthropic
    r = resolve(skill)
    if not r["model_id"]:
        return False, {"error": "No model ID set"}
    if family(r["provider"]) == "openai":
        return _test_openai(r)
    if r["provider"] == "bedrock" and get_config().get("bedrock_transport") == "converse":
        return _test_converse(r)  # the same API the agent's Strands BedrockModel uses
    body = {**request_body(r), "max_tokens": 1024,
            "messages": [{"role": "user", "content": "Reply with exactly: Sidekick model check OK"}]}
    if r["effort"]:
        body["output_config"] = {"effort": "low"}  # keep the check cheap
    t0 = time.perf_counter()
    try:
        msg = _client(r).messages.create(**body)
    except anthropic.AuthenticationError as e:
        return False, {"error": f"Authentication failed (401): {e.message}"}
    except anthropic.PermissionDeniedError as e:
        return False, {"error": f"Permission denied (403) — is model access enabled for this account/region? {e.message}"}
    except anthropic.NotFoundError as e:
        return False, {"error": f"Model not found (404): {r['model_id']} — {e.message}"}
    except anthropic.RateLimitError as e:
        return False, {"error": f"Rate limited (429): {e.message}"}
    except anthropic.APIStatusError as e:
        return False, {"error": f"HTTP {e.status_code}: {e.message}"}
    except anthropic.APIConnectionError as e:
        return False, {"error": f"Connection error: {e}"}
    except Exception as e:  # noqa: BLE001 — e.g. no AWS credentials / region
        if r["provider"] == "bedrock" and "credentials" in str(e).lower():
            return False, {"error": "No AWS credentials found — add an access key in Integrations → AWS (or run Sidekick with an IAM role)"}
        return False, {"error": f"{e.__class__.__name__}: {e}"}
    ms = round((time.perf_counter() - t0) * 1000)
    if msg.stop_reason == "refusal":
        return False, {"error": "The model declined the request (stop_reason: refusal)", "latency_ms": ms}
    text = next((b.text for b in msg.content if b.type == "text"), "")
    return True, {"text": text, "model": msg.model, "latency_ms": ms, "stop_reason": msg.stop_reason,
                  "input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens, "label": label(r)}
