"""Sidekick control plane API + dashboard.

Dashboard (static) is served at /, the API under /api. Bound to localhost by docker-compose — no auth yet.
"""
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import brain, db, devrev, integrations, models, previews
from .catalog import POLICIES, SETTINGS, SKILLS, default_workflows

OWNER_EMAIL = os.getenv("SIDEKICK_OWNER_EMAIL", "owner@example.com")
OWNER_NAME = os.getenv("SIDEKICK_OWNER_NAME", "Owner")


CUSTOM_REPLIES_DDL = """CREATE TABLE IF NOT EXISTS custom_replies (
  id          SERIAL PRIMARY KEY,
  pattern     TEXT        NOT NULL,
  match_type  TEXT        NOT NULL DEFAULT 'contains',   -- contains | exact | regex
  response    TEXT        NOT NULL,
  enabled     BOOLEAN     NOT NULL DEFAULT true,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now())"""


def seed():
    db.run(CUSTOM_REPLIES_DDL)  # added after the first release — create on existing databases too
    db.run("ALTER TABLE runs ADD COLUMN IF NOT EXISTS model TEXT")
    if not db.one("SELECT 1 AS x FROM custom_replies LIMIT 1"):
        db.run("INSERT INTO custom_replies (pattern, response) VALUES (%s, %s)",
               ("where are you from", "I'm *{agent_name}* 👋 — I run from the Sidekick dashboard. Configure what I can do at {dashboard_url}"))
    for k, v in SETTINGS.items():
        db.run("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT DO NOTHING", (k, db.J(v)))
    for key, name, desc, tools, autonomy in SKILLS:
        db.run("INSERT INTO skills (key, name, description, tools, autonomy) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
               (key, name, desc, db.J(tools), autonomy))
    for code, title, desc, enabled in POLICIES:
        db.run("INSERT INTO policies (code, title, description, enabled) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", (code, title, desc, enabled))
    if not db.one("SELECT 1 AS x FROM workflows LIMIT 1"):
        for wf in default_workflows():
            _save_workflow(wf)
    db.run("INSERT INTO users (name, email, role) VALUES (%s, %s, 'Owner') ON CONFLICT DO NOTHING", (OWNER_NAME, OWNER_EMAIL))


@asynccontextmanager
async def lifespan(_app):
    db.open_pool()
    seed()
    devrev.start_feedback_sync()  # Slack feedback (synced into DevRev as a chat) → searchable tickets, every 5 min
    # runs interrupted by a restart can't resume (threads died) — mark them failed and unstick their conversations
    db.run("UPDATE conversations SET status = 'new' WHERE id IN (SELECT conversation_id FROM runs WHERE status = 'running')")
    db.run("UPDATE runs SET status = 'failed', finished_at = now(), result = %s WHERE status = 'running'",
           (db.J({"text": "❌ Interrupted by a restart", "actions": []}),))
    yield
    db.close_pool()


app = FastAPI(title="Sidekick API", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.get("/api/health")
def health():
    db.one("SELECT 1 AS ok")
    return {"status": "ok"}


@app.get("/api/summary")
def summary():
    return {
        "pending_approvals": db.one("SELECT count(*) AS n FROM approvals WHERE status = 'pending'")["n"],
        "running": db.one("SELECT count(*) AS n FROM runs WHERE status = 'running'")["n"],
        "owner": {"name": OWNER_NAME, "email": OWNER_EMAIL},
        "brain_mode": brain.setting("brain_mode") or "scripted",
    }


# ---------- settings ----------
@app.get("/api/settings")
def get_settings():
    rows = db.all("SELECT key, value FROM settings WHERE key = ANY(%s)", (list(SETTINGS),))
    return {**SETTINGS, **{r["key"]: r["value"] for r in rows}}


class SettingsIn(BaseModel):
    agent_name: str | None = None
    handle: str | None = None
    model: str | None = None
    step_delay: list[float] | None = Field(default=None, min_length=2, max_length=2)
    brain_mode: str | None = Field(default=None, pattern="^(scripted|llm)$")


@app.put("/api/settings")
def put_settings(body: SettingsIn):
    for k, v in body.model_dump(exclude_none=True).items():
        if k == "step_delay":
            v = [max(0.0, min(v)), max(0.0, max(v))]
        db.run("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (k, db.J(v)))
    return get_settings()


# ---------- integrations ----------
def _check_integration(key):
    if key not in integrations.CATALOG:
        raise HTTPException(404, "Unknown integration")


@app.get("/api/integrations")
def list_integrations():
    rows = {r["key"]: r for r in db.all("SELECT * FROM integrations")}
    return [integrations.public(k, rows.get(k)) for k in integrations.CATALOG]


@app.put("/api/integrations/{key}")
def save_integration(key: str, body: dict):
    _check_integration(key)
    return integrations.save(key, body)


@app.post("/api/integrations/{key}/test")
def test_integration(key: str):
    _check_integration(key)
    return integrations.test(key)


@app.delete("/api/integrations/{key}")
def delete_integration(key: str):
    _check_integration(key)
    return integrations.disconnect(key)


# ---------- workflows ----------
def _save_workflow(wf):
    db.run("""INSERT INTO workflows (id, name, description, active, mode, version, steps, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, now())
              ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, description = EXCLUDED.description, active = EXCLUDED.active,
              mode = EXCLUDED.mode, version = EXCLUDED.version, steps = EXCLUDED.steps, updated_at = now()""",
           (wf["id"], wf["name"], wf.get("description", ""), wf.get("active", True), wf.get("mode", "Live"), wf.get("version", 1), db.J(wf.get("steps", []))))


@app.get("/api/workflows")
def list_workflows():
    return db.all("SELECT id, name, description, active, mode, version, steps, updated_at FROM workflows ORDER BY CASE id WHEN 'incident' THEN 0 WHEN 'feature' THEN 1 ELSE 2 END, id")


class WorkflowIn(BaseModel):
    name: str
    description: str = ""
    active: bool = True
    mode: str = "Live"
    version: int = 1
    steps: list[dict]


@app.put("/api/workflows/{wf_id}")
def put_workflow(wf_id: str, body: WorkflowIn):
    _save_workflow({"id": wf_id, **body.model_dump()})
    return db.one("SELECT * FROM workflows WHERE id = %s", (wf_id,))


@app.post("/api/workflows/{wf_id}/reset")
def reset_workflow(wf_id: str):
    wf = next((w for w in default_workflows() if w["id"] == wf_id), None)
    if not wf:
        raise HTTPException(404, "No default for this workflow")
    cur = db.one("SELECT version FROM workflows WHERE id = %s", (wf_id,))
    _save_workflow({**wf, "version": (cur["version"] if cur else 1)})
    return db.one("SELECT * FROM workflows WHERE id = %s", (wf_id,))


# ---------- skills / policies / users ----------
@app.get("/api/skills")
def list_skills():
    return db.all("SELECT * FROM skills ORDER BY CASE key WHEN 'debugger' THEN 0 WHEN 'developer' THEN 1 WHEN 'tester' THEN 2 WHEN 'infra' THEN 3 ELSE 4 END")


class SkillIn(BaseModel):
    enabled: bool | None = None
    autonomy: str | None = Field(default=None, pattern="^(suggest|approval|auto)$")


@app.patch("/api/skills/{key}")
def patch_skill(key: str, body: SkillIn):
    for k, v in body.model_dump(exclude_none=True).items():
        db.run(f"UPDATE skills SET {k} = %s WHERE key = %s", (v, key))  # k is from a fixed model field set
    return db.one("SELECT * FROM skills WHERE key = %s", (key,))


@app.get("/api/policies")
def list_policies():
    return db.all("SELECT * FROM policies ORDER BY code = 'read-only', code")


class PolicyIn(BaseModel):
    enabled: bool


@app.patch("/api/policies/{code}")
def patch_policy(code: str, body: PolicyIn):
    db.run("UPDATE policies SET enabled = %s WHERE code = %s", (body.enabled, code))
    return db.one("SELECT * FROM policies WHERE code = %s", (code,))


ROLES = "^(Owner|SRE|Developer|PM|Viewer)$"


class UserIn(BaseModel):
    name: str = Field(min_length=1)
    email: str = Field(min_length=3)
    role: str = Field(pattern=ROLES)
    slack_user_id: str | None = None


@app.get("/api/users")
def list_users():
    return db.all("SELECT * FROM users ORDER BY id")


@app.post("/api/users", status_code=201)
def add_user(body: UserIn):
    try:
        return db.one("INSERT INTO users (name, email, role, slack_user_id) VALUES (%s, %s, %s, %s) RETURNING *",
                      (body.name, body.email, body.role, (body.slack_user_id or "").strip() or None))
    except Exception as e:  # unique violations
        raise HTTPException(409, "A user with that email or Slack ID already exists") from e


class UserPatch(BaseModel):
    role: str | None = Field(default=None, pattern=ROLES)
    slack_user_id: str | None = None


@app.patch("/api/users/{uid}")
def patch_user(uid: int, body: UserPatch):
    if body.role:
        db.run("UPDATE users SET role = %s WHERE id = %s", (body.role, uid))
    if body.slack_user_id is not None:
        db.run("UPDATE users SET slack_user_id = %s WHERE id = %s", ((body.slack_user_id or "").strip() or None, uid))
    return db.one("SELECT * FROM users WHERE id = %s", (uid,))


@app.delete("/api/users/{uid}", status_code=204)
def delete_user(uid: int):
    db.run("DELETE FROM users WHERE id = %s", (uid,))


# ---------- model ----------
def _model_view(cfg=None):
    cfg = cfg or models.get_config()
    skills = db.all("SELECT key, name FROM skills ORDER BY CASE key WHEN 'debugger' THEN 0 WHEN 'developer' THEN 1 WHEN 'tester' THEN 2 WHEN 'infra' THEN 3 ELSE 4 END")
    default = models.resolve(None, cfg)
    oa_models, oa_error = models.openai_models(provider=cfg["provider"]) if models.family(cfg["provider"]) == "openai" else ([], None)
    return {"config": cfg, "providers": models.PROVIDERS, "models": models.MODELS, "efforts": models.EFFORTS,
            "openai": {"models": oa_models, "error": oa_error},
            "default": {**default, "label": models.label(default), "request": models.request_body(default)},
            "skills": [{**(r := models.resolve(s["key"], cfg)), "key": s["key"], "skill_name": s["name"], "label": models.label(r)} for s in skills]}


@app.post("/api/model/openai-models/refresh")
def refresh_openai_models():
    provider = models.get_config()["provider"]
    ids, err = models.openai_models(force=True, provider=provider if models.family(provider) == "openai" else "openai")
    return {"models": ids, "error": err}


@app.get("/api/model")
def get_model():
    return _model_view()


class ModelSkillIn(BaseModel):
    model: str = ""
    effort: str = ""


class ModelIn(BaseModel):
    provider: str = Field(pattern="^(bedrock|anthropic|openai|akamai_ai)$")
    model: str = Field(min_length=1)
    custom_model: str = ""
    region: str = "us-east-1"
    effort: str = Field(pattern="^(low|medium|high|xhigh|max)$")
    max_tokens: int = Field(ge=256, le=128000)
    skills: dict[str, ModelSkillIn] = {}
    bedrock_transport: str = Field(default="messages", pattern="^(messages|converse)$")


@app.put("/api/model")
def put_model(body: ModelIn):
    for m in [body.model] + [s.model for s in body.skills.values() if s.model]:
        if not models.valid_model(body.provider, m):
            raise HTTPException(422, f"{m!r} isn't a {models.PROVIDERS[body.provider]['name']} model — pick one from the list or use Custom")
    return _model_view(models.save_config(body.model_dump()))


class ModelTestIn(BaseModel):
    skill: str | None = None


@app.post("/api/model/test")
def test_model(body: ModelTestIn):
    ok, details = models.test(body.skill)
    return {"ok": ok, **details}


# ---------- custom replies ----------
MATCH = "^(contains|exact|regex)$"


class ReplyIn(BaseModel):
    pattern: str = Field(min_length=1, max_length=500)
    match_type: str = Field(default="contains", pattern=MATCH)
    response: str = Field(min_length=1, max_length=3000)
    enabled: bool = True


class ReplyPatch(BaseModel):
    pattern: str | None = Field(default=None, min_length=1, max_length=500)
    match_type: str | None = Field(default=None, pattern=MATCH)
    response: str | None = Field(default=None, min_length=1, max_length=3000)
    enabled: bool | None = None


def _check_regex(body):
    if body.match_type == "regex" and body.pattern:
        try:
            re.compile(body.pattern)
        except re.error as e:
            raise HTTPException(422, f"Invalid regex: {e}") from e


@app.get("/api/replies")
def list_replies():
    return db.all("SELECT * FROM custom_replies ORDER BY id")


@app.post("/api/replies", status_code=201)
def add_reply(body: ReplyIn):
    _check_regex(body)
    return db.one("INSERT INTO custom_replies (pattern, match_type, response, enabled) VALUES (%s, %s, %s, %s) RETURNING *",
                  (body.pattern.strip(), body.match_type, body.response, body.enabled))


@app.patch("/api/replies/{rid}")
def patch_reply(rid: int, body: ReplyPatch):
    cur = db.one("SELECT * FROM custom_replies WHERE id = %s", (rid,))
    if not cur:
        raise HTTPException(404)
    merged = ReplyIn(**{**cur, **body.model_dump(exclude_none=True)})
    _check_regex(merged)
    return db.one("UPDATE custom_replies SET pattern = %s, match_type = %s, response = %s, enabled = %s WHERE id = %s RETURNING *",
                  (merged.pattern.strip(), merged.match_type, merged.response, merged.enabled, rid))


@app.delete("/api/replies/{rid}", status_code=204)
def delete_reply(rid: int):
    db.run("DELETE FROM custom_replies WHERE id = %s", (rid,))


class TryIn(BaseModel):
    text: str


@app.post("/api/replies/try")
def try_reply(body: TryIn):
    """What would Sidekick say to this message? (custom reply, or which built-in intent handles it)"""
    r = brain.match_custom_reply(body.text)
    if r:
        conv = {"status": "new"}
        return {"source": "custom", "reply_id": r["id"], "text": brain.render_reply(r["response"], conv, {"name": "you"})}
    return {"source": "builtin", "intent": brain.classify(body.text) or "help"}


# ---------- agent (Slack + dashboard console) ----------
class Actor(BaseModel):
    source: str = "dashboard"
    id: str | None = None
    name: str | None = None


class MessageIn(BaseModel):
    source: str = Field(pattern="^(slack|dashboard|devrev)$")
    channel: str
    thread_ts: str = ""
    text: str
    actor: Actor = Actor()
    thread_context: str = Field(default="", max_length=8000)  # earlier messages in the Slack thread (people, DevRev, …)


class ActionIn(BaseModel):
    source: str = Field(pattern="^(slack|dashboard|devrev)$")
    channel: str
    thread_ts: str = ""
    action: str
    value: str = ""
    actor: Actor = Actor()


@app.post("/api/agent/message")
def agent_message(body: MessageIn):
    conv = brain.get_conversation(body.source, body.channel, body.thread_ts)
    return brain.handle_message(conv, body.text, body.actor.model_dump(), body.thread_context)


@app.post("/api/agent/action")
def agent_action(body: ActionIn):
    conv = brain.get_conversation(body.source, body.channel, body.thread_ts)
    return brain.handle_action(conv, body.action, body.value, body.actor.model_dump())


@app.get("/api/conversation")
def lookup_conversation(source: str, channel: str, thread_ts: str = ""):
    c = db.one("SELECT * FROM conversations WHERE source = %s AND channel = %s AND thread_ts = %s", (source, channel, thread_ts))
    return c or {"status": "new"}


@app.get("/api/conversations/{cid}")
def get_conversation(cid: int):
    c = db.one("SELECT * FROM conversations WHERE id = %s", (cid,))
    if not c:
        raise HTTPException(404)
    return c


# ---------- runs & approvals ----------
@app.get("/api/runs")
def list_runs(limit: int = 50):
    return db.all("""SELECT r.*, c.source AS conv_source, c.channel, c.thread_ts, c.status AS conv_status
                     FROM runs r LEFT JOIN conversations c ON c.id = r.conversation_id ORDER BY r.created_at DESC LIMIT %s""", (min(limit, 200),))


@app.get("/api/runs/{run_id}")
def get_run(run_id: int):
    r = db.one("SELECT r.*, c.status AS conv_status FROM runs r LEFT JOIN conversations c ON c.id = r.conversation_id WHERE r.id = %s", (run_id,))
    if not r:
        raise HTTPException(404, "Run not found")
    return r


@app.get("/api/approvals")
def list_approvals(status: str = "pending"):
    return db.all("""SELECT a.*, c.source, c.channel, c.thread_ts FROM approvals a JOIN conversations c ON c.id = a.conversation_id
                     WHERE (%s = 'all' OR a.status = %s) ORDER BY a.created_at DESC LIMIT 100""", (status, status))


class DecisionIn(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    actor: Actor | None = None  # who decided (e.g. a DevRev teammate via the MCP connector); default: dashboard owner


@app.post("/api/approvals/{aid}/decide")
def decide(aid: int, body: DecisionIn):
    a = db.one("SELECT * FROM approvals WHERE id = %s", (aid,))
    if not a:
        raise HTTPException(404)
    conv = db.one("SELECT * FROM conversations WHERE id = %s", (a["conversation_id"],))
    actor = body.actor.model_dump() if body.actor else {"source": "dashboard", "id": OWNER_EMAIL, "name": f"{OWNER_NAME} (dashboard)"}
    res = brain.approve(conv, actor) if body.decision == "approve" else brain.reject(conv, actor)
    if conv["source"] == "slack":  # tell the Slack thread what happened; the bot follows the run there
        text = (res.get("reply") or {}).get("text") or ("✅ Approved from the dashboard" if body.decision == "approve" else "❌ Rejected from the dashboard")
        db.run("INSERT INTO outbox (channel, thread_ts, text, run_id) VALUES (%s, %s, %s, %s)", (conv["channel"], conv["thread_ts"], text, res.get("run_id")))
    return res


# ---------- LKE PR previews + kill switch (see previews.py / deploy/gate.sh) ----------
def _preview_audit(title, detail):
    db.run("""INSERT INTO runs (intent, skill, title, status, source, requested_by, steps, result, finished_at, model)
              VALUES ('previews', 'infra', %s, 'done', 'dashboard', %s, %s, %s, now(), 'LKE via deploy gate (no model)')""",
           (title, f"{OWNER_NAME} (dashboard)", db.J([{"tool": "lke.kpreview", "detail": detail, "status": "done", "integration": "akamai", "connected": True}]),
            db.J({"text": title, "actions": []})))


@app.get("/api/previews")
def get_previews():
    if not previews.configured():
        return {"configured": False, "previews": [], "limit": 5}
    try:
        return {"configured": True, "previews": previews.list_previews(), "limit": int(os.getenv("MAX_K8S_PREVIEWS", "5"))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Couldn't reach the cluster through the deploy gate: {e}")


@app.delete("/api/previews/{pr}")
def delete_preview(pr: int):
    try:
        previews.delete(pr)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))
    _preview_audit(f"Deleted LKE preview pr-{pr}", f"kpreview down {pr}")
    return {"deleted": [pr]}


@app.delete("/api/previews")
def delete_all_previews():
    try:
        n = previews.delete_all()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))
    _preview_audit(f"Kill switch: deleted all {n} LKE preview(s)", "kpreview down-all")
    return {"deleted": n}


# ---------- DevRev Computer MCP connector (audit trail) ----------
class ConnectorLogIn(BaseModel):
    tool: str = Field(max_length=100)
    args: dict = {}
    summary: str = Field(default="", max_length=4000)
    evidence: list[str] = []
    actor: Actor = Actor(source="devrev")


@app.post("/api/connector/log", status_code=201)
def connector_log(body: ConnectorLogIn):
    """Every connector tool call shows up in Runs (source: DevRev) with its arguments and evidence."""
    args = ", ".join(f"{k}={v}" for k, v in body.args.items()) or "—"
    evidence = ("\n\n*Evidence:* " + " · ".join(body.evidence[:40])) if body.evidence else ""
    return db.one("""INSERT INTO runs (intent, skill, title, status, source, requested_by, steps, result, finished_at, model)
                     VALUES ('connector', 'devrev', %s, 'done', 'devrev', %s, %s, %s, now(), 'DevRev Computer · MCP connector') RETURNING id""",
                  (f"DevRev: {body.tool}", body.actor.name or body.actor.id,
                   db.J([{"tool": f"devrev.{body.tool}", "detail": args[:300], "status": "done", "secs": 0, "integration": "devrev", "connected": True}]),
                   db.J({"text": body.summary + evidence, "actions": []})))


# ---------- Slack bot plumbing ----------
@app.post("/api/bot/heartbeat")
def heartbeat(body: dict):
    db.run("INSERT INTO settings (key, value) VALUES ('slack_bot', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
           (db.J({**body, "at": datetime.now(timezone.utc).isoformat()}),))
    return {"ok": True}


@app.get("/api/outbox/pending")
def outbox_pending():
    return db.all("SELECT * FROM outbox WHERE NOT delivered ORDER BY id LIMIT 20")


@app.post("/api/outbox/{oid}/delivered")
def outbox_delivered(oid: int):
    db.run("UPDATE outbox SET delivered = true WHERE id = %s", (oid,))
    return {"ok": True}


# ---------- dashboard ----------
app.mount("/", StaticFiles(directory=os.getenv("DASHBOARD_DIR", "/srv/dashboard"), html=True), name="dashboard")
