/* ===== Workflow model — step catalog, default workflows, presets =====
   Shared by the dashboard builder and the agent (agent.js reads the incident
   workflow to decide what happens after a fix is approved). */

const STEP_GROUPS = ["Triggers", "Agent skills", "Gates", "Delivery", "Notify"];

// kind drives color/icon; sim = the tool call Sidekick "executes" for this step in the demo
const STEP_CATALOG = {
  trigger_alert:   { label: "Alert fired", group: "Triggers", kind: "trigger", desc: "PagerDuty, CloudWatch or Prometheus alert on a watched service.",
                     fields: [{ k: "source", l: "Source", t: "select", o: ["PagerDuty", "CloudWatch alarm", "Prometheus Alertmanager"] }, { k: "severity", l: "Min severity", t: "select", o: ["SEV1", "SEV2", "SEV3"] }],
                     defaults: { source: "PagerDuty", severity: "SEV2" } },
  trigger_ticket:  { label: "DevRev ticket", group: "Triggers", kind: "trigger", desc: "New ticket or conversation matching a filter.",
                     fields: [{ k: "filter", l: "Filter", t: "text" }], defaults: { filter: "type = bug AND tag = checkout" } },
  trigger_mention: { label: "@sidekick mention", group: "Triggers", kind: "trigger", desc: "Someone mentions the agent in Slack, Teams or DevRev.",
                     fields: [{ k: "channels", l: "Channels", t: "text" }], defaults: { channels: "Slack, Teams, DevRev" } },
  trigger_schedule:{ label: "Schedule", group: "Triggers", kind: "trigger", desc: "Run on a cron schedule.",
                     fields: [{ k: "cron", l: "Cron", t: "text" }], defaults: { cron: "0 2 * * *" } },

  investigate:     { label: "Investigate root cause", group: "Agent skills", kind: "skill", skill: "Debugger", desc: "Correlates metrics, logs, deploys and edge data to find the cause.",
                     fields: [{ k: "window", l: "Look-back window", t: "select", o: ["1h", "6h", "24h"] }], defaults: { window: "6h" },
                     sim: ["debugger.investigate", "root cause: Cache-Control regression (catalog-api v2.14.0)"] },
  propose_fix:     { label: "Propose fix", group: "Agent skills", kind: "skill", skill: "Developer", desc: "Writes the code / config change and explains it.",
                     fields: [{ k: "scope", l: "Allowed changes", t: "select", o: ["Code + infra config", "Code only", "Infra config only"] }], defaults: { scope: "Code + infra config" },
                     sim: ["developer.propose_fix", "1 file + Akamai property v58 + HPA patch"] },
  build_feature:   { label: "Build feature", group: "Agent skills", kind: "skill", skill: "Developer", desc: "Forks the repo, plans and implements the feature from the ticket.",
                     fields: [{ k: "model", l: "Model", t: "select", o: ["Claude Opus 5.5", "Claude Sonnet 5"] }], defaults: { model: "Claude Opus 5.5" },
                     sim: ["developer.build", "+184 −12 across 4 files"] },
  run_tests:       { label: "Run tests", group: "Agent skills", kind: "skill", skill: "Tester", desc: "Unit, e2e and load tests against the change.",
                     fields: [{ k: "suites", l: "Suites", t: "text" }, { k: "block", l: "Block on failure", t: "toggle" }], defaults: { suites: "unit, e2e, load", block: true },
                     sim: ["tests.run", "unit 212 passed · e2e 27 passed · k6 p95 184 ms"] },
  record_video:    { label: "Record demo video", group: "Agent skills", kind: "skill", skill: "Tester", desc: "Playwright walkthrough of the change, attached to the PR.",
                     fields: [], defaults: {}, sim: ["video.record", "playwright walkthrough, 0:42"] },
  infra_change:    { label: "Infra change", group: "Agent skills", kind: "skill", skill: "Infra Ops", desc: "Applies AWS / Kubernetes / Akamai config.",
                     fields: [{ k: "targets", l: "Targets", t: "text" }], defaults: { targets: "k8s, akamai" },
                     sim: ["infra.apply", "hpa/catalog-api, akamai property v58"] },

  approval:        { label: "Human approval", group: "Gates", kind: "gate", desc: "Pauses until a person with the right role approves.",
                     fields: [{ k: "approvers", l: "Approvers", t: "select", o: ["SRE or Eng Manager", "Any developer", "Owner only"] }, { k: "where", l: "Ask in", t: "select", o: ["Same thread", "Slack DM", "DevRev ticket"] }, { k: "timeout", l: "Timeout", t: "select", o: ["1h", "4h", "24h"] }],
                     defaults: { approvers: "SRE or Eng Manager", where: "Same thread", timeout: "4h" },
                     sim: ["approval.wait", "approved by @priya"] },
  policy_check:    { label: "Policy check", group: "Gates", kind: "gate", desc: "Evaluates guardrail policies (cost, scope, prod rules).",
                     fields: [], defaults: {}, sim: ["policy.check", "prod-changes-need-human, cost-guardrail ✓"] },
  verify_metrics:  { label: "Verify metrics", group: "Gates", kind: "gate", desc: "Waits for SLOs to hold; fails the run otherwise.",
                     fields: [{ k: "query", l: "Condition", t: "text" }, { k: "onFail", l: "On failure", t: "select", o: ["Rollback", "Pause & notify"] }],
                     defaults: { query: "p99 < 300 ms for 5 min", onFail: "Rollback" },
                     sim: ["prometheus.verify", "p99 < 300 ms for 5 min ✓"] },

  raise_pr:        { label: "Raise PR", group: "Delivery", kind: "delivery", desc: "Opens a pull request from Sidekick's fork with description, diff and evidence.",
                     fields: [{ k: "draft", l: "Open as draft", t: "toggle" }, { k: "autoMerge", l: "Auto-merge when checks pass", t: "toggle" }],
                     defaults: { draft: false, autoMerge: false },
                     sim: ["github.create_pull_request", "shoplite/catalog-api #133"] },
  merge_pr:        { label: "Merge PR", group: "Delivery", kind: "delivery", desc: "Squash-merges the PR into main.",
                     fields: [], defaults: {}, sim: ["github.merge", "#133 (squash) → main"] },
  deploy_preview:  { label: "Deploy preview env", group: "Delivery", kind: "delivery", desc: "Spins up an isolated namespace on EKS for the branch.",
                     fields: [{ k: "cluster", l: "Cluster", t: "select", o: ["EKS shop-dev", "EKS shop-prod"] }], defaults: { cluster: "EKS shop-dev" },
                     sim: ["k8s.deploy_preview", "namespace pr-133 on shop-dev"] },
  deploy_staging:  { label: "Deploy to staging", group: "Delivery", kind: "delivery", desc: "Rolls the change out to staging (incl. Akamai staging network).",
                     fields: [], defaults: {}, sim: ["argo.rollout", "catalog-api → staging · Akamai v58 on STAGING"] },
  deploy_prod:     { label: "Deploy to production", group: "Delivery", kind: "delivery", desc: "Rolls out to production and activates edge config.",
                     fields: [{ k: "strategy", l: "Strategy", t: "select", o: ["Canary 10 → 50 → 100%", "Blue / green", "All at once"] }, { k: "akamai", l: "Activate Akamai on production", t: "toggle" }],
                     defaults: { strategy: "Canary 10 → 50 → 100%", akamai: true },
                     sim: ["argo.rollout", "prod canary 10% → 50% → 100% · Akamai v58 on PRODUCTION"] },
  rollback:        { label: "Auto-rollback guard", group: "Delivery", kind: "delivery", desc: "Watches error rate after deploy and rolls back automatically.",
                     fields: [{ k: "threshold", l: "Roll back if", t: "text" }], defaults: { threshold: "5xx rate > 1% for 2 min" },
                     sim: ["argo.rollback_guard", "armed: 5xx > 1% → rollback"] },

  update_ticket:   { label: "Update DevRev ticket", group: "Notify", kind: "notify", desc: "Posts the summary and resolves / updates the ticket.",
                     fields: [{ k: "reply", l: "Reply to customer", t: "toggle" }], defaults: { reply: true },
                     sim: ["devrev.update_ticket", "TKT-1042 → Resolved, customer notified"] },
  post_channel:    { label: "Post to channel", group: "Notify", kind: "notify", desc: "Posts a summary to Slack or Teams.",
                     fields: [{ k: "channel", l: "Channel", t: "text" }], defaults: { channel: "#checkout-oncall" },
                     sim: ["slack.post_message", "summary → #checkout-oncall"] },
};

let _uid = 0;
function mkStep(type, config) {
  const def = STEP_CATALOG[type];
  return { uid: "s" + Date.now().toString(36) + (_uid++), type, config: { ...(def.defaults || {}), ...(config || {}) } };
}

// What happens after the fix is approved — the "matter of configuration" part
const DELIVERY_PRESETS = {
  pr_only:  { label: "Raise PR only", desc: "Sidekick opens the PR; a human merges and deploys.", steps: () => [mkStep("raise_pr"), mkStep("run_tests"), mkStep("update_ticket", { reply: false })] },
  staging:  { label: "PR → staging → production", desc: "Default. Verify on staging before prod.", steps: () => [mkStep("raise_pr", { autoMerge: true }), mkStep("run_tests"), mkStep("deploy_staging"), mkStep("verify_metrics"), mkStep("deploy_prod"), mkStep("update_ticket")] },
  straight: { label: "PR → straight to production", desc: "Approval is the only gate. Auto-merge + canary to prod.", steps: () => [mkStep("raise_pr", { autoMerge: true }), mkStep("run_tests"), mkStep("deploy_prod"), mkStep("rollback"), mkStep("update_ticket")] },
};

// ---- persistence: workflows live in the Sidekick backend (/api/workflows) ----
// The UI edits an in-memory copy; every change is PUT back (debounced). `desc` ⇄ API `description`.
let WF_CACHE = [];
const WF_SAVED = {};
let wfSaveTimer = null;

async function loadWorkflows() {
  const rows = await fetch("/api/workflows").then(r => r.json());
  WF_CACHE = rows.map(w => ({ ...w, desc: w.description }));
  WF_CACHE.forEach(w => (WF_SAVED[w.id] = JSON.stringify(w)));
  return WF_CACHE;
}
function getWorkflows() { return WF_CACHE; }
function getWorkflow(id) { return WF_CACHE.find(w => w.id === id); }
function saveWorkflows(fn) {
  fn(WF_CACHE);
  clearTimeout(wfSaveTimer);
  wfSaveTimer = setTimeout(flushWorkflows, 300);
}
async function flushWorkflows() {
  for (const w of WF_CACHE) {
    const json = JSON.stringify(w);
    if (WF_SAVED[w.id] === json) continue;
    const body = { name: w.name, description: w.desc ?? w.description ?? "", active: !!w.active, mode: w.mode, version: w.version, steps: w.steps };
    const r = await fetch(`/api/workflows/${w.id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (r.ok) WF_SAVED[w.id] = json; else if (typeof toast === "function") toast("Couldn't save workflow — " + r.status);
  }
}
async function resetWorkflow(id) {
  const w = await fetch(`/api/workflows/${id}/reset`, { method: "POST" }).then(r => r.json());
  const i = WF_CACHE.findIndex(x => x.id === id);
  WF_CACHE[i] = { ...w, desc: w.description };
  WF_SAVED[id] = JSON.stringify(WF_CACHE[i]);
}
function stepsAfterApproval(wf) {
  const i = wf.steps.findIndex(s => s.type === "approval");
  return i >= 0 ? wf.steps.slice(i + 1) : wf.steps.slice(wf.steps.findIndex(s => s.type === "propose_fix") + 1);
}

// Which preset matches the steps after the first approval gate (or "custom")
function detectPreset(wf) {
  const i = wf.steps.findIndex(s => s.type === "approval");
  if (i < 0) return "custom";
  const after = wf.steps.slice(i + 1).map(s => s.type).join(",");
  for (const [k, p] of Object.entries(DELIVERY_PRESETS)) if (p.steps().map(s => s.type).join(",") === after) return k;
  return "custom";
}
function applyPreset(wfId, key) {
  saveWorkflows(ws => {
    const wf = ws.find(w => w.id === wfId);
    let i = wf.steps.findIndex(s => s.type === "approval");
    if (i < 0) { wf.steps.splice(3, 0, mkStep("approval")); i = 3; }
    wf.steps = [...wf.steps.slice(0, i + 1), ...DELIVERY_PRESETS[key].steps()];
  });
}

// Validation shown in the builder
function validateWorkflow(wf) {
  const out = [];
  const types = wf.steps.map(s => s.type);
  if (!wf.steps.length || STEP_CATALOG[types[0]].kind !== "trigger") out.push(["bad", "Workflow must start with a trigger"]);
  else out.push(["good", "Starts with a trigger"]);
  if (types.filter(t => STEP_CATALOG[t].kind === "trigger").length > 1) out.push(["warn", "More than one trigger — only the first is used"]);
  const prod = types.indexOf("deploy_prod");
  if (prod >= 0) {
    const gated = types.slice(0, prod).some(t => t === "approval");
    out.push(gated ? ["good", "Production deploy is behind a human approval"] : ["bad", "Production deploy has no human approval — blocked by policy prod-changes-need-human"]);
    if (!types.slice(0, prod).includes("run_tests")) out.push(["warn", "No tests run before production"]);
    if (!types.slice(prod).some(t => t === "verify_metrics" || t === "rollback")) out.push(["warn", "No metric check or rollback guard after production deploy"]);
  }
  return out;
}
