"""Sidekick brain v0 — plain if/else on the conversation's status.

    new ──"spike / slow / why"──► investigating ──► root_caused ──"fix it"──► proposing_fix ──► awaiting_approval
    awaiting_approval ──approve──► shipping ──► resolved (or pr_open if the workflow only raises a PR)
                      └─reject───► rejected ──"fix it"──► (propose again)
    any ──"build wishlist / tinder"──► building ──► pr_open        any ──"test" / "cost"──► (one-off run, status unchanged)

Every "run" executes its steps in a background thread and writes progress to the `runs` table, so Slack and the
dashboard can both poll it. Steps are simulated for now; when Strands arrives, `_execute` calls real tools instead.
"""
import os
import random
import re
import threading
import time
from urllib.parse import urlparse

import requests

from . import db, integrations, models
from .catalog import SETTINGS, STEP_SIM

BUSY = {"investigating": "investigating", "proposing_fix": "preparing the fix", "shipping": "shipping the fix",
        "building": "building the feature", "testing": "running tests", "thinking": "working on your last message"}

STATUS_TEXT = {
    "new": "Nothing started yet in this thread.",
    "investigating": "Investigating the incident.",
    "root_caused": "Root cause found — say *fix it* and I'll prepare a fix.",
    "proposing_fix": "Preparing a fix.",
    "awaiting_approval": "A fix is ready and *waiting for approval*.",
    "shipping": "Shipping the approved fix.",
    "resolved": "Done — the fix is in production and the ticket is resolved.",
    "rejected": "The last proposed fix was rejected. Say *fix it* to propose again.",
    "building": "Building the feature.",
    "pr_open": "A PR with a preview environment is open for review.",
    "thinking": "Working on your last message (LLM agent).",
}

INTENTS = [  # checked in order
    ("escalation", r"^\s*Customer incident escalated from DevRev"),  # hand-off from the DevRev Computer connector
    ("reset", r"\b(start over|reset|new incident)\b"),
    ("status", r"\b(status|where are we|progress|update\?)\b"),
    ("approve", r"^\s*(approve|approved|lgtm|ship it)\b"),
    ("reject", r"^\s*(reject|rejected|cancel|don'?t ship)\b"),
    ("help", r"^\s*(help|\?|hi|hello|hey)\s*$"),
    ("test", r"\btests?\b|\bqa\b|k6|e2e|load[- ]test"),
    ("swipe", r"tinder|swipe"),
    ("fix", r"\bfix\b|remediat|patch|resolve it"),
    ("feature", r"wishlist|feature|build|implement|TKT-1039"),
    ("cost", r"running|inventory|cost|ec2|\bs3\b|kinesis|cluster|resources|\baws\b|bill"),
    ("spike", r"spike|7\s?(:00)?\s?am|latency|slow|outage|incident|why|down\b|errors?"),
]
SKILL_FOR = {"escalation": "debugger", "spike": "debugger", "fix": "infra", "ship": "infra", "feature": "developer", "swipe": "developer", "test": "tester", "cost": "analyst"}
TOOL_INTEGRATION = {"prometheus": "prometheus", "aws": "aws", "k8s": "aws", "argo": "aws", "infra": "aws", "akamai": "akamai",
                    "github": "github", "developer": "github", "devrev": "devrev", "slack": "slack", "debugger": "prometheus", "shoplite": "shoplite"}


def classify(text: str):
    clean = re.sub(r"<@[A-Z0-9]+>|@sidekick", "", text or "", flags=re.I).strip()
    for intent, rx in INTENTS:
        if re.search(rx, clean, re.I):
            return intent
    return None


# ---------- small helpers ----------
def setting(key):
    row = db.one("SELECT value FROM settings WHERE key = %s", (key,))
    return row["value"] if row else SETTINGS.get(key)


def policy_on(code):
    row = db.one("SELECT enabled FROM policies WHERE code = %s", (code,))
    return bool(row and row["enabled"])


def skill_on(key):
    row = db.one("SELECT enabled FROM skills WHERE key = %s", (key,))
    return bool(row and row["enabled"])


def skill_name(key):
    row = db.one("SELECT name FROM skills WHERE key = %s", (key,))
    return row["name"] if row else key


def get_conversation(source, channel, thread_ts=""):
    db.run("INSERT INTO conversations (source, channel, thread_ts) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (source, channel, thread_ts or ""))
    return db.one("SELECT * FROM conversations WHERE source = %s AND channel = %s AND thread_ts = %s", (source, channel, thread_ts or ""))


def set_status(conv_id, status):
    db.run("UPDATE conversations SET status = %s, updated_at = now() WHERE id = %s", (status, conv_id))


def reply(conv, text, actions=None, run_id=None):
    return {"conversation_id": conv["id"], "reply": {"text": text, "actions": actions or []}, "run_id": run_id}


def started(conv, run_id, text=None):
    return {"conversation_id": conv["id"], "reply": {"text": text, "actions": []} if text else None, "run_id": run_id}


def actor_label(actor):
    return actor.get("name") or actor.get("id") or "someone"


def incident_workflow():
    return db.one("SELECT * FROM workflows WHERE id = 'incident'")


def steps_after_approval(wf):
    steps = wf["steps"]
    i = next((n for n, s in enumerate(steps) if s["type"] == "approval"), None)
    if i is None:
        i = next((n for n, s in enumerate(steps) if s["type"] == "propose_fix"), -1)
    return steps[i + 1:]


# ---------- runs ----------
def start_run(conv, intent, title, steps, finish, actor):
    skill = SKILL_FOR.get(intent, "analyst")
    model = models.label(models.resolve(skill))  # which LLM this skill is configured to use (Configure → Model)
    row = db.one("INSERT INTO runs (conversation_id, intent, skill, title, source, requested_by, model) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                 (conv["id"], intent, skill, title, conv["source"], actor_label(actor), model))
    threading.Thread(target=_execute, args=(row["id"], conv, steps, finish), daemon=True).start()
    return row["id"]


def _execute(run_id, conv, steps, finish):
    lo, hi = setting("step_delay") or [2, 3]
    done = []
    try:
        for tool, detail in steps:
            integ = TOOL_INTEGRATION.get(tool.split(".")[0])
            entry = {"tool": tool, "detail": detail, "status": "running", "integration": integ,
                     "connected": bool(integ and integrations.connected(integ))}
            done.append(entry)
            db.run("UPDATE runs SET steps = %s WHERE id = %s", (db.J(done), run_id))
            secs = random.uniform(float(lo), float(hi))
            time.sleep(secs)
            entry.update(status="done", secs=round(secs, 1))
            db.run("UPDATE runs SET steps = %s WHERE id = %s", (db.J(done), run_id))
        text, actions = finish()
        db.run("UPDATE runs SET status = 'done', result = %s, finished_at = now() WHERE id = %s",
               (db.J({"text": text, "actions": actions}), run_id))
    except Exception as e:  # noqa: BLE001
        db.run("UPDATE runs SET status = 'failed', result = %s, finished_at = now() WHERE id = %s",
               (db.J({"text": f"❌ Run failed: {e}", "actions": []}), run_id))
        set_status(conv["id"], "new")


# ---------- custom replies (Dashboard → Replies) ----------
def clean_text(text):
    return re.sub(r"\s+", " ", re.sub(r"<@[A-Z0-9]+>|@sidekick", "", text or "", flags=re.I)).strip()


def reply_matches(r, text):
    t, p = clean_text(text).lower().rstrip("?!. "), r["pattern"].strip().lower().rstrip("?!. ")
    if not p:
        return False
    if r["match_type"] == "exact":
        return t == p
    if r["match_type"] == "regex":
        try:
            return re.search(r["pattern"], clean_text(text), re.I) is not None
        except re.error:
            return False
    return p in t  # contains


def match_custom_reply(text):
    for r in db.all("SELECT * FROM custom_replies WHERE enabled ORDER BY id"):
        if reply_matches(r, text):
            return r
    return None


def render_reply(template, conv, actor):
    values = {"agent_name": setting("agent_name") or "Sidekick", "handle": setting("handle") or "@sidekick",
              "user": actor_label(actor), "status": STATUS_TEXT.get(conv["status"], conv["status"]),
              "dashboard_url": os.getenv("SIDEKICK_PUBLIC_URL", "http://localhost:8090")}
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), m.group(0))), template)


# ---------- LLM mode (Strands agent, see llm_agent.py) ----------
def start_llm_run(conv, text, actor):
    model = models.label(models.resolve(None))
    title = re.sub(r"<@[A-Z0-9]+>|@sidekick", "", text, flags=re.I).strip()[:90] or "Message"
    row = db.one("INSERT INTO runs (conversation_id, intent, skill, title, source, requested_by, model) VALUES (%s, 'llm', 'agent', %s, %s, %s, %s) RETURNING id",
                 (conv["id"], title, conv["source"], actor_label(actor), model))
    prev = conv["status"]
    set_status(conv["id"], "thinking")
    threading.Thread(target=_execute_llm, args=(row["id"], conv, text, actor, prev), daemon=True).start()
    return row["id"]


def _execute_llm(run_id, conv, text, actor, prev_status):
    from . import llm_agent  # imported lazily: Strands is only needed in LLM mode
    try:
        reply, approval = llm_agent.run(run_id, conv, text, actor)
        set_status(conv["id"], "awaiting_approval" if approval else ("new" if prev_status in BUSY else prev_status))
        db.run("UPDATE runs SET status = 'done', result = %s, finished_at = now() WHERE id = %s",
               (db.J({"text": reply, "actions": APPROVE_ACTIONS if approval else []}), run_id))
    except Exception as e:  # noqa: BLE001 — surface model/credential errors to the user
        set_status(conv["id"], prev_status if prev_status not in BUSY else "new")
        db.run("UPDATE runs SET status = 'failed', result = %s, finished_at = now() WHERE id = %s",
               (db.J({"text": f"❌ *LLM brain error:* {e.__class__.__name__}: {str(e)[:400]}\n"
                              "Check Configure → Model → *Test model*, or switch Brain mode back to *Scripted demo*.", "actions": []}), run_id))


# ---------- the brain ----------
def handle_message(conv, text, actor):
    intent = classify(text)
    s = conv["status"]

    custom = match_custom_reply(text)  # configured in Dashboard → Replies; checked before any built-in behaviour
    if custom:
        return reply(conv, render_reply(custom["response"], conv, actor))

    if intent == "reset":
        set_status(conv["id"], "new")
        return reply(conv, "🧹 Starting fresh in this thread. What's going on?")
    if intent == "status":
        return reply(conv, f"*Status:* {STATUS_TEXT.get(s, s)}", _suggestions(s))
    if s in BUSY:
        return reply(conv, f"⏳ I'm still {BUSY[s]} — I'll post the result here as soon as it's done.")
    if intent == "approve":
        return approve(conv, actor)
    if intent == "reject":
        return reject(conv, actor)

    if setting("brain_mode") == "llm":  # Settings → Brain mode: hand everything else to the Strands agent
        return started(conv, start_llm_run(conv, text, actor))

    if intent in (None, "help"):
        return reply(conv, _help(s), _suggestions(s))

    skill = SKILL_FOR[intent]
    if not skill_on(skill):
        return reply(conv, f"🔌 The *{skill_name(skill)}* skill is turned off. Enable it in Dashboard → Skills.")

    if intent == "escalation":
        if s not in ("new", "resolved", "rejected", "pr_open", "root_caused"):
            return reply(conv, f"*Already on it* — {STATUS_TEXT.get(s, s)}", _suggestions(s))
        e = _parse_escalation(text)
        set_status(conv["id"], "investigating")
        return started(conv, start_run(conv, "escalation", f"Escalation {e['ticket']}: {e['subject'][:60]}", _escalation_steps(e),
                                       lambda: _finish_escalation(conv, e, actor), actor))

    if intent == "spike":
        set_status(conv["id"], "investigating")
        return started(conv, start_run(conv, "spike", "Investigate latency spike", SPIKE_STEPS, lambda: _finish_spike(conv), actor))

    if intent == "fix":
        if s == "new":
            return reply(conv, "🤔 I haven't investigated anything in this thread yet — tell me what's wrong first, "
                               "e.g. _“there was a latency spike at 7am, what's the reason?”_",
                         [{"id": "say", "label": "🔎 Investigate the 7am spike", "value": "there was a latency spike at 7am, what's the reason?", "style": "primary"}])
        if s == "awaiting_approval":
            return reply(conv, "✋ A fix is already proposed and *waiting for approval*.", APPROVE_ACTIONS)
        if s == "resolved":
            return reply(conv, "✅ This one is already fixed and shipped. Say *start over* for a new incident.")
        wf = incident_workflow()
        if not wf["active"]:
            return reply(conv, "⏸️ The *Incident → fix* workflow is paused in Dashboard → Workflows, so I won't prepare changes.")
        set_status(conv["id"], "proposing_fix")
        return started(conv, start_run(conv, "fix", "Propose fix: cache-header regression", FIX_STEPS, lambda: _finish_fix(conv, actor), actor))

    if intent in ("feature", "swipe"):
        if policy_on("read-only"):
            return reply(conv, "🔒 *Read-only mode* is on — I can describe the change but won't create branches or PRs.")
        set_status(conv["id"], "building")
        steps, title = (SWIPE_STEPS, "Build swipe-to-shop (TKT-1039)") if intent == "swipe" else (FEATURE_STEPS, "Build wishlist (TKT-1039)")
        return started(conv, start_run(conv, intent, title, steps, lambda: _finish_feature(conv, intent), actor))

    if intent == "test":
        return started(conv, start_run(conv, "test", "Load + e2e tests on the preview", TEST_STEPS, _finish_test, actor))

    if intent == "cost":
        return started(conv, start_run(conv, "cost", "AWS inventory & cost", COST_STEPS, _finish_cost, actor))

    return reply(conv, _help(s), _suggestions(s))


def handle_action(conv, action, value, actor):
    if action == "open":  # link buttons just open a URL — nothing for the brain to do
        return {"conversation_id": conv["id"], "reply": None, "run_id": None}
    if action == "approve":
        return approve(conv, actor)
    if action == "reject":
        return reject(conv, actor)
    return handle_message(conv, value or "", actor)  # "say" buttons


def can_approve(actor):
    if not policy_on("prod-changes-need-human"):
        return True, None
    if actor.get("source") == "dashboard":
        return True, None  # dashboard user is the Owner until we add login
    if actor.get("source") == "devrev":  # DevRev connector teammates: check their Sidekick role by email if they're listed
        user = db.one("SELECT * FROM users WHERE lower(email) = lower(%s)", (actor.get("id") or "",))
        if user and user["role"] not in ("Owner", "SRE"):
            return False, f"{user['name']} is a *{user['role']}* — only Owner / SRE can approve production changes."
        return True, None  # the connector already enforces per-teammate approval rights (teammates.json can_approve)
    user = db.one("SELECT * FROM users WHERE slack_user_id = %s", (actor.get("id"),))
    if user:
        if user["role"] in ("Owner", "SRE"):
            return True, None
        return False, f"{user['name']} is a *{user['role']}* — only Owner / SRE can approve production changes (policy `prod-changes-need-human`)."
    mapped = db.one("SELECT count(*) AS n FROM users WHERE slack_user_id IS NOT NULL")["n"]
    if not mapped:
        return True, None  # bootstrap: nobody mapped yet, so don't lock everyone out
    return False, "You're not in Sidekick's approver list. Ask an Owner to add your Slack user in Dashboard → Access control."


def approve(conv, actor):
    s = conv["status"]
    if s != "awaiting_approval":
        msg = "✅ Already shipped." if s == "resolved" else "Nothing in this thread is waiting for approval right now."
        return reply(conv, msg, _suggestions(s))
    if policy_on("read-only"):
        return reply(conv, "🔒 *Read-only mode* is on (Dashboard → Access control) — I can't ship changes.")
    ok, why = can_approve(actor)
    if not ok:
        return reply(conv, f"⛔ {why}")
    db.run("UPDATE approvals SET status = 'approved', decided_by = %s, decided_at = now() WHERE conversation_id = %s AND status = 'pending'",
           (actor_label(actor), conv["id"]))
    change = _context(conv).get("code_change")
    if change:  # a real code change proposed by the LLM agent → branch + commit + PR on GitHub
        set_status(conv["id"], "shipping")
        change = {**change, "approved_by": actor_label(actor)}
        run_id = start_task_run(conv, "ship", f"Open PR: {change['title'][:80]}", actor, lambda step: _ship_code_change(conv, change, step))
        return started(conv, run_id, f"✅ Approved by {actor_label(actor)} — creating a branch and opening a pull request.")
    wf = incident_workflow()
    after = steps_after_approval(wf)
    steps = []
    for st in after:
        tool, detail, _ = STEP_SIM.get(st["type"], (st["type"], "", None))
        if st["type"] == "raise_pr" and st["config"].get("autoMerge"):
            detail = "shoplite/catalog-api #133 · auto-merge on green"
        if st["type"] == "deploy_prod":
            detail = f"prod · {st['config'].get('strategy', 'canary')}" + (" · Akamai v58 on PRODUCTION" if st["config"].get("akamai", True) else "")
        steps.append((tool, detail))
    set_status(conv["id"], "shipping")
    run_id = start_run(conv, "ship", "Ship approved fix", steps, lambda: _finish_ship(conv, [st["type"] for st in after]), actor)
    return started(conv, run_id, f"✅ Approved by {actor_label(actor)} — following workflow *{wf['name']}* v{wf['version']}.")


def reject(conv, actor):
    if conv["status"] != "awaiting_approval":
        return reply(conv, "Nothing in this thread is waiting for approval right now.")
    db.run("UPDATE approvals SET status = 'rejected', decided_by = %s, decided_at = now() WHERE conversation_id = %s AND status = 'pending'",
           (actor_label(actor), conv["id"]))
    set_status(conv["id"], "rejected")
    ctx = _context(conv)
    if ctx.get("code_change") or ctx.get("approval_title"):  # proposed by the LLM agent: nothing was created yet
        db.run("UPDATE conversations SET context = context - 'code_change' - 'approval_title' - 'approval_plan' WHERE id = %s", (conv["id"],))
        return reply(conv, f"❌ Rejected by {actor_label(actor)} — nothing was changed and no branch or PR was created. Tell me what to do differently.")
    return reply(conv, f"❌ Rejected by {actor_label(actor)} — keeping branch `sidekick/fix-cache-header` as a draft. Say *fix it* to propose again.")


def _context(conv):
    return (db.one("SELECT context FROM conversations WHERE id = %s", (conv["id"],)) or {}).get("context") or {}


def _ship_code_change(conv, change, step):
    from . import github_ops
    pr = github_ops.open_pr(change, step)
    db.run("UPDATE conversations SET context = context - 'code_change' - 'approval_title' - 'approval_plan' WHERE id = %s", (conv["id"],))
    set_status(conv["id"], "pr_open")
    actions = [{"id": "open", "label": f"🔗 View PR #{pr['number']}", "url": pr["url"], "style": "primary"}]
    preview = ""
    cfg = integrations.creds("shoplite")  # PR previews may live elsewhere (LKE) than the demo previews (VM)
    template = cfg.get("pr_preview_url_template") or cfg.get("preview_url_template") or ""
    if template.startswith("https://"):  # hosted: the repo's PR-preview workflow builds every PR at pr-<number>.<host>
        url = template.format(label=f"pr-{pr['number']}", branch=pr["branch"], port="")
        preview = f"\n• Preview on Akamai LKE (ready in ~3–4 min, built by GitHub Actions): {url}"
        actions.append({"id": "open", "label": f"🔍 Open preview pr-{pr['number']}", "url": url})
    return (f"✅ *Pull request #{pr['number']} opened* on `{pr['repo']}`: {change['title']}\n"
            f"• Branch `{pr['branch']}` → `{pr['base']}`\n• Review and merge it on GitHub: {pr['url']}{preview}", actions)


# ---------- runs whose steps do real work (not simulated) ----------
def start_task_run(conv, intent, title, actor, task):
    """Like start_run, but `task(step)` performs the work; step(tool, detail, fn) runs fn and records it live."""
    skill = SKILL_FOR.get(intent, "analyst")
    row = db.one("INSERT INTO runs (conversation_id, intent, skill, title, source, requested_by, model) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                 (conv["id"], intent, skill, title, conv["source"], actor_label(actor), "GitHub API (no model)"))
    threading.Thread(target=_execute_task, args=(row["id"], conv, task), daemon=True).start()
    return row["id"]


def _execute_task(run_id, conv, task):
    done = []

    def step(tool, detail, fn):
        integ = TOOL_INTEGRATION.get(tool.split(".")[0])
        entry = {"tool": tool, "detail": detail, "status": "running", "integration": integ, "connected": bool(integ and integrations.connected(integ))}
        done.append(entry)
        db.run("UPDATE runs SET steps = %s WHERE id = %s", (db.J(done), run_id))
        t0 = time.perf_counter()
        try:
            out = fn()
        except Exception as e:
            entry.update(status="error", detail=f"{detail} → {str(e)[:200]}", secs=round(time.perf_counter() - t0, 1))
            db.run("UPDATE runs SET steps = %s WHERE id = %s", (db.J(done), run_id))
            raise
        entry.update(status="done", secs=round(time.perf_counter() - t0, 1))
        db.run("UPDATE runs SET steps = %s WHERE id = %s", (db.J(done), run_id))
        return out

    try:
        text, actions = task(step)
        db.run("UPDATE runs SET status = 'done', result = %s, finished_at = now() WHERE id = %s", (db.J({"text": text, "actions": actions}), run_id))
    except Exception as e:  # noqa: BLE001
        set_status(conv["id"], "awaiting_approval")  # the approved change is still in context: fix the cause and approve again
        db.run("UPDATE approvals SET status = 'pending', decided_by = NULL, decided_at = NULL WHERE id = "
               "(SELECT id FROM approvals WHERE conversation_id = %s ORDER BY created_at DESC LIMIT 1)", (conv["id"],))
        db.run("UPDATE runs SET status = 'failed', result = %s, finished_at = now() WHERE id = %s",
               (db.J({"text": f"❌ Couldn't open the PR: {str(e)[:400]}\nNothing was merged. Fix the cause and approve again, or reject.",
                      "actions": APPROVE_ACTIONS}), run_id))


def _help(status):
    return (f"*Status:* {STATUS_TEXT.get(status, status)}\n\nI can:\n"
            "• investigate an incident — _“there was a latency spike at 7am, what's the reason?”_\n"
            "• prepare a fix and ship it after approval — _“fix it”_\n"
            "• build a feature — _“build the wishlist from TKT-1039”_ (add _“tinder style”_ for a swipe UI)\n"
            "• run tests — _“run load tests on the preview”_ · answer infra questions — _“what's running in AWS and what does it cost?”_\n"
            "• _“status”_ · _“start over”_")


def _suggestions(status):
    if status in ("new", "resolved", "pr_open"):
        return [{"id": "say", "label": "🔎 Investigate the 7am spike", "value": "there was a latency spike at 7am, what's the reason?", "style": "primary"}]
    if status in ("root_caused", "rejected"):
        return [{"id": "say", "label": "🛠 Propose a fix", "value": "fix it", "style": "primary"}]
    if status == "awaiting_approval":
        return APPROVE_ACTIONS
    return []


APPROVE_ACTIONS = [{"id": "approve", "label": "Approve", "value": "approve", "style": "primary"},
                   {"id": "reject", "label": "Reject", "value": "reject", "style": "danger"}]

# ---------- scripted step lists ----------
SPIKE_STEPS = [("prometheus.query", "p99 checkout_request_seconds, 06:00–09:00"), ("aws.cloudwatch.get_metric_data", "EKS node CPU, RDS connections"),
               ("aws.kinesis.describe_stream", "orders-stream iterator age"), ("akamai.reporting.cache_hits", "property shoplite.com, /api/*"),
               ("k8s.get_events", "namespace shop-prod, HPA + OOM events"), ("github.list_deployments", "shoplite/* between 06:30 and 07:10")]
FIX_STEPS = [("github.fork_and_branch", "shoplite/catalog-api → sidekick/fix-cache-header"), ("code.edit", "src/routes/products.ts — restore Cache-Control"),
             ("akamai.property.create_version", "shoplite.com v58 (draft)"), ("k8s.diff", "hpa/catalog-api 6 → 12; checkout db pool 50 → 80")]
FEATURE_STEPS = [("devrev.get_ticket", "TKT-1039 “save products to a wishlist” + 14 upvotes"), ("github.fork_and_branch", "shoplite/web → sidekick/wishlist-1039"),
                 ("code.plan", "ProductCard, WishlistContext, /wishlist page, API route"), ("code.edit", "+184 −12 across 4 files"),
                 ("tests.run", "unit 48 passed · playwright 9 passed"), ("k8s.deploy_preview", "namespace pr-142"),
                 ("video.record", "playwright walkthrough, 0:42"), ("github.create_pull_request", "shoplite/web #142")]
SWIPE_STEPS = [("devrev.get_ticket", "TKT-1039 + 14 upvotes"), ("design.interpret", "“Tinder-style” → card stack · → wishlist · ← skip · ↑ cart"),
               ("github.fork_and_branch", "shoplite/web → sidekick/swipe-shop-1039"), ("code.edit", "+412 −18 across 6 files"),
               ("tests.run", "unit 61 · playwright 14 (touch + mouse gestures)"), ("k8s.deploy_preview", "namespace pr-143"),
               ("video.record", "swipe walkthrough, 0:38"), ("github.create_pull_request", "shoplite/web #143")]
TEST_STEPS = [("k6.run", "500 VUs × 5 min against the preview"), ("playwright.run", "full e2e suite, 3 browsers"), ("axe.audit", "/, /product/:id, /wishlist")]
COST_STEPS = [("aws.resource_explorer.search", "all regions"), ("aws.cost_explorer.get_cost", "month-to-date by service"), ("k8s.get_nodes", "shop-prod, shop-dev")]


# ---------- finishers: apply the state transition, return (text, actions) ----------
def _finish_spike(conv):
    set_status(conv["id"], "root_caused")
    return ("*p99 checkout latency went from ~220 ms to 2.3 s between 07:00 and 07:50* (peak 07:20).\n\n"
            "🔴 *Root cause — cache header regression in `catalog-api`*\n"
            "1. *06:52* deploy `catalog-api v2.14.0` (PR #131) changed `Cache-Control` on `/api/products` from `max-age=300` to `no-store`\n"
            "2. *07:00* flash sale starts → Akamai cache hit ratio on `/api/*` drops *94% → 11%*, origin traffic ×8.5\n"
            "3. *07:04* catalog-api pods CPU-throttled, HPA pinned at max (6 replicas)\n"
            "4. *07:09* checkout-service exhausts its RDS pool (50/50); Kinesis `orders-stream` iterator age → 94 s\n"
            "5. *07:55* traffic subsides and it recovers on its own — *the bug is still live*\n\n"
            "*Impact:* ~1,240 checkout sessions slowed · 312 abandoned carts (est. $9.8k)\n*Confidence:* high — timing, deploy diff and edge logs agree",
            [{"id": "say", "label": "🛠 Propose a fix", "value": "fix it", "style": "primary"}])


def _finish_fix(conv, actor):
    wf = incident_workflow()
    plan = " → ".join(STEP_SIM.get(s["type"], (s["type"],))[0].split(".")[-1].replace("_", " ") for s in steps_after_approval(wf))
    approvers = next((s["config"].get("approvers") for s in wf["steps"] if s["type"] == "approval"), None)
    db.run("INSERT INTO approvals (conversation_id, title, summary, requested_by) VALUES (%s, %s, %s, %s)",
           (conv["id"], "Ship fix: restore Cache-Control on /api/products",
            "Code (catalog-api #133) + Akamai property v58 + HPA 6 → 12 / DB pool 50 → 80", actor_label(actor)))
    set_status(conv["id"], "awaiting_approval")
    return ("I've prepared a fix in three layers:\n"
            "```- res.set(\"Cache-Control\", \"no-store\");\n+ res.set(\"Cache-Control\", \"public, max-age=300, stale-while-revalidate=60\");```\n"
            "• *Code:* branch `sidekick/fix-cache-header` · 1 file · +1 −1\n• *Akamai:* property `shoplite.com` v58 (draft)\n"
            "• *Kubernetes:* HPA max 6 → 12 · checkout DB pool 50 → 80\n\n"
            f"⚠️ *Approval required* ({approvers or 'human approver'}) — workflow `{wf['name']}` v{wf['version']}\n*Next:* {plan}",
            APPROVE_ACTIONS)


def _finish_ship(conv, types):
    ctx = (db.one("SELECT context FROM conversations WHERE id = %s", (conv["id"],)) or {}).get("context") or {}
    if ctx.get("approval_title"):  # approval came from the LLM agent — summarise its plan, not the scripted incident
        db.run("UPDATE conversations SET context = context - 'approval_title' - 'approval_plan' WHERE id = %s", (conv["id"],))
        done = " → ".join(STEP_SIM.get(t, (t,))[0].split(".")[-1].replace("_", " ") for t in types)
        set_status(conv["id"], "resolved" if "deploy_prod" in types else "pr_open")
        return (f"✅ *{ctx['approval_title']}* — approved and run through the workflow: {done}.\n"
                f"_Plan:_\n```{ctx.get('approval_plan', '')}```\n"
                "_Delivery steps are simulated until the GitHub / deploy tools are wired in._", [])
    if "deploy_prod" not in types:
        set_status(conv["id"], "pr_open")
        return ("*PR raised:* shoplite/catalog-api #133 — tests green. Per the workflow, a human merges and deploys.", [])
    set_status(conv["id"], "resolved")
    via = " (via staging)" if "deploy_staging" in types else " — straight from the approved PR"
    guard = "\n• *Rollback:* auto-rollback guard armed (5xx > 1%)" if "rollback" in types else ""
    return (f"✅ *Deployed to production{via}.* Everything is green.\n• *p99 latency:* 212 ms (under SLO)\n• *Akamai cache hit:* 94%{guard}\n"
            "• *Ticket:* TKT-1042 resolved · reply sent to the customer\n_Also added a regression test asserting `Cache-Control` on `/api/products`._", [])


# ---------- DevRev escalations (scripted; the LLM brain handles these with real tools) ----------
def _parse_escalation(text):
    g = lambda rx, d="": (re.search(rx, text) or [None, d])[1]
    return {"ticket": g(r"\b(TKT-\d+)", "TKT-?"), "priority": g(r"\((P\d)\)", "P1"), "subject": g(r'"([^"]+)"', "customer issue"),
            "account": g(r'"\s*—\s*([^,]+),', "the customer"), "arr": g(r"ARR (\$[\d,]+)"), "component": g(r"Component: ([^.]+)\.", "the affected component"),
            "issue": g(r"issue (ISS-\d+)"), "issue_title": g(r'issue ISS-\d+ "([^"]+)"'), "owner": g(r'ISS-\d+ "[^"]+" \([^,]+, ([^)]+)\)'),
            "sla": g(r"(SLA first response .+? breached by [\d,.]+ days)")}


def _escalation_steps(e):
    slug = re.sub(r"[^a-z0-9]+", "-", e["component"].lower()).strip("-")[:30]
    return [("devrev.get_ticket", f"{e['ticket']} · {e['account']} · {e['priority']}"),
            ("devrev.get_issue", f"{e['issue']} {e['issue_title'][:50]}" if e["issue"] else f"search open issues on {e['component']}"),
            ("github.search_code", f"maple/{slug} — code paths for “{e['subject'][:40]}”"),
            ("github.list_commits", f"maple/{slug} · last 30 days"),
            ("tests.reproduce", f"regression test for {e['ticket']}")]


def _finish_escalation(conv, e, actor):
    wf = incident_workflow()
    plan_steps = " → ".join(STEP_SIM.get(s["type"], (s["type"],))[0].split(".")[-1].replace("_", " ") for s in steps_after_approval(wf))
    title = f"Fix {e['ticket']}" + (f" / {e['issue']}" if e["issue"] else "") + f": {e['subject'][:70]}"
    plan = "\n".join([
        f"1. Branch sidekick/{e['ticket'].lower()} with a failing regression test reproducing {e['ticket']}",
        f"2. Patch {e['component']}" + (f" per {e['issue']} (owner {e['owner']})" if e["issue"] else ""),
        f"3. Preview environment + demo video, PR linked to {e['ticket']}" + (f" and {e['issue']}" if e["issue"] else ""),
        f"4. After merge: reply to {e['account']} on {e['ticket']} and close the SLA clock"])
    db.run("INSERT INTO approvals (conversation_id, title, summary, requested_by) VALUES (%s, %s, %s, %s)",
           (conv["id"], title[:200], f"{e['account']} ({e['arr'] or 'ARR n/a'}) · {e['component']} · {e['sla'] or 'SLA ok'}\n\nPlan:\n{plan}",
            actor_label(actor)))
    db.run("UPDATE conversations SET context = context || %s WHERE id = %s", (db.J({"approval_title": title[:200], "approval_plan": plan}), conv["id"]))
    set_status(conv["id"], "awaiting_approval")
    lines = [f"🔎 *{e['ticket']} — {e['subject']}*",
             f"• *Customer:* {e['account']}" + (f" · ARR {e['arr']}" if e["arr"] else ""),
             f"• *Component:* {e['component']}",
             (f"• *Known engineering issue:* {e['issue']} “{e['issue_title']}” — owner {e['owner']}. The ticket is a customer-facing "
              "instance of it, so the fix belongs there (not a one-off workaround).") if e["issue"]
             else "• *No matching engineering issue* — I'll open one with the fix."]
    if e["sla"]:
        lines.append(f"• ⛔ *{e['sla']}* — reply to the customer first.")
    lines += ["", "*Proposed fix*", f"```{plan}```", f"⚠️ *Approval required* — workflow `{wf['name']}` v{wf['version']}",
              f"*Next:* {plan_steps}", "_Scripted demo brain: switch Brain mode to LLM for a real investigation._"]
    return "\n".join(lines), APPROVE_ACTIONS


PREVIEWS = {"feature": ("pr-142", 8142), "swipe": ("pr-143", 8143)}  # started by shoplite/scripts/demo-previews.sh


def preview_link(label, port):
    """(url shown to people, is it really running?) — URL from Integrations → ShopLite → preview URL template."""
    cfg = integrations.creds("shoplite")
    url = (cfg.get("preview_url_template") or "http://localhost:{port}").format(port=port, branch=label, label=label)
    host = urlparse(cfg.get("url") or "http://host.docker.internal:8080").hostname or "host.docker.internal"
    try:  # check from inside the Sidekick container (host.docker.internal), link for humans uses the template
        h = requests.get(f"http://{host}:{port}/api/health", timeout=3).json()
        return url, h.get("preview") == label
    except (requests.RequestException, ValueError):
        return url, False


def _finish_feature(conv, intent):
    set_status(conv["id"], "pr_open")
    label, port = PREVIEWS[intent]
    url, live = preview_link(label, port)
    preview = (f"• Preview: <{url}|{label}> — ✅ live" if live
               else f"• Preview: `{label}` — ⚠️ not running yet (start it with `scripts/demo-previews.sh up` in shoplite/)")
    actions = ([{"id": "open", "label": f"🔗 Open {label} preview", "url": url, "style": "primary"}] if live else []) + \
              [{"id": "say", "label": "🧪 Run tests on the preview", "value": "run load and e2e tests on the preview", "style": None}]
    if intent == "swipe":
        return ("Built a *Tinder-style “Discover” mode* — products come as a card stack:\n• *Swipe right* → wishlist\n• *Swipe left* → skip\n• *Swipe up* → cart\n\n"
                "*PR #143* feat(web): swipe-to-shop Discover mode (TKT-1039)\n• Branch `sidekick/swipe-shop-1039` · 6 files · +412 −18 · ✅ build ✅ unit 61 ✅ e2e 14\n"
                f"{preview} · 🎥 swipe walkthrough attached (0:38)", actions)
    return ("Wishlist is built and ready for review — shoppers tap ♥ on any product and see saved items in the ♥ Wishlist drawer.\n\n"
            "*PR #142* feat(web): add wishlist (TKT-1039)\n• Branch `sidekick/wishlist-1039` → `main` · 4 files · +184 −12\n"
            f"• Checks: ✅ build ✅ unit 48 ✅ e2e 9 ✅ lint\n{preview} · 🎥 demo video attached to the PR (0:42)", actions)


def _finish_test():
    return ("All checks passed on the preview.\n• *Load (k6):* ✅ p95 184 ms · 0.0% errors · 2.1k rps\n"
            "• *E2E (Playwright):* ✅ 27/27 on Chromium, Firefox, WebKit\n• *Accessibility:* 1 minor — ♥ button needed `aria-pressed`, fixup commit pushed", [])


def _finish_cost():
    return ("*Running now, month-to-date:*\n• EKS — 2 clusters, 14 nodes — $2,140\n• EC2 — 8 instances (*3 idle <2% CPU for 14 days*) — $610\n"
            "• RDS — postgres shop-prod, Multi-AZ — $890\n• S3 — 9 buckets, 4.2 TB (*no lifecycle on logs*) — $312\n• Kinesis — 2 streams — $186 · Akamai — 38 TB egress — $674\n\n"
            "*Savings I can make (~$410/mo):* stop 3 idle `legacy-cron-*` instances ($240) · Glacier lifecycle on `shoplite-logs` ($170)", [])
