"""Defaults seeded on first start + what each workflow step "does" in the scripted runner.

Mirrors dashboard/assets/workflows.js (the UI's step catalog). When Strands arrives, STEP_SIM is
replaced by real tools; the shape (tool, detail, integration) stays.
"""
import time
import uuid

# step type -> (tool name, detail, integration it needs)
STEP_SIM = {
    "investigate": ("debugger.investigate", "correlate metrics, deploys and edge data", "prometheus"),
    "propose_fix": ("developer.propose_fix", "code + Akamai property + HPA patch", "github"),
    "build_feature": ("developer.build", "+184 −12 across 4 files", "github"),
    "run_tests": ("tests.run", "unit 212 passed · e2e 27 passed · k6 p95 184 ms", None),
    "record_video": ("video.record", "playwright walkthrough, 0:42", None),
    "infra_change": ("infra.apply", "hpa/catalog-api, akamai property v58", "aws"),
    "approval": ("approval.wait", "approved", None),
    "policy_check": ("policy.check", "prod-changes-need-human, cost-guardrail ✓", None),
    "verify_metrics": ("prometheus.verify", "p99 < 300 ms for 5 min ✓", "prometheus"),
    "raise_pr": ("github.create_pull_request", "shoplite/catalog-api #133", "github"),
    "merge_pr": ("github.merge", "#133 (squash) → main", "github"),
    "deploy_preview": ("k8s.deploy_preview", "namespace pr-133 on shop-dev", "aws"),
    "deploy_staging": ("argo.rollout", "catalog-api → staging · Akamai v58 on STAGING", "aws"),
    "deploy_prod": ("argo.rollout", "prod canary 10% → 50% → 100% · Akamai v58 on PRODUCTION", "aws"),
    "rollback": ("argo.rollback_guard", "armed: 5xx > 1% → rollback", "aws"),
    "update_ticket": ("devrev.update_ticket", "TKT-1042 → Resolved, customer notified", "devrev"),
    "post_channel": ("slack.post_message", "summary → channel", "slack"),
}

STEP_DEFAULTS = {
    "trigger_alert": {"source": "PagerDuty", "severity": "SEV2"},
    "trigger_ticket": {"filter": "type = bug AND tag = checkout"},
    "trigger_mention": {"channels": "Slack, Teams, DevRev"},
    "trigger_schedule": {"cron": "0 2 * * *"},
    "investigate": {"window": "6h"},
    "propose_fix": {"scope": "Code + infra config"},
    "build_feature": {"model": "Claude Opus 5.5"},
    "run_tests": {"suites": "unit, e2e, load", "block": True},
    "infra_change": {"targets": "k8s, akamai"},
    "approval": {"approvers": "SRE or Eng Manager", "where": "Same thread", "timeout": "4h"},
    "verify_metrics": {"query": "p99 < 300 ms for 5 min", "onFail": "Rollback"},
    "raise_pr": {"draft": False, "autoMerge": False},
    "deploy_preview": {"cluster": "EKS shop-dev"},
    "deploy_prod": {"strategy": "Canary 10 → 50 → 100%", "akamai": True},
    "rollback": {"threshold": "5xx rate > 1% for 2 min"},
    "update_ticket": {"reply": True},
    "post_channel": {"channel": "#checkout-oncall"},
}

_n = 0


def mk_step(type_, **config):
    global _n
    _n += 1
    return {"uid": f"s{int(time.time() * 1000):x}{_n}", "type": type_, "config": {**STEP_DEFAULTS.get(type_, {}), **config}}


def default_workflows():
    return [
        {"id": "incident", "name": "Incident → fix", "description": "Alerts and bug tickets: find the cause, propose a fix, ship it.",
         "active": True, "mode": "Live", "version": 1,
         "steps": [mk_step("trigger_alert"), mk_step("investigate"), mk_step("propose_fix"), mk_step("approval"),
                   mk_step("raise_pr", autoMerge=True), mk_step("run_tests"), mk_step("deploy_staging"), mk_step("verify_metrics"),
                   mk_step("deploy_prod"), mk_step("update_ticket")]},
        {"id": "feature", "name": "Feature request → PR", "description": "Turn a DevRev feature request into a PR with a preview and demo video.",
         "active": True, "mode": "Live", "version": 1,
         "steps": [mk_step("trigger_ticket", filter="type = feature AND upvotes >= 10"), mk_step("build_feature"), mk_step("run_tests"),
                   mk_step("deploy_preview"), mk_step("record_video"), mk_step("raise_pr"), mk_step("approval", approvers="Any developer"),
                   mk_step("merge_pr"), mk_step("update_ticket")]},
        {"id": "cost", "name": "Nightly cost sweep", "description": "Find idle resources and propose savings.",
         "active": False, "mode": "Dry-run", "version": 1,
         "steps": [mk_step("trigger_schedule"), mk_step("infra_change", targets="ec2 idle, s3 lifecycle"), mk_step("approval"),
                   mk_step("post_channel", channel="#infra")]},
    ]


SKILLS = [
    ("debugger", "Debugger", "Root-causes incidents across metrics, logs, deploys and edge data.", ["prometheus", "cloudwatch", "akamai.reporting", "k8s.read", "github.read"], "auto"),
    ("developer", "Developer", "Builds features & fixes from tickets: forks, codes, deploys a preview, records a demo video, opens a PR.", ["github", "code.edit", "k8s.deploy_preview", "video.record"], "approval"),
    ("tester", "Tester", "Runs unit, e2e, load and a11y tests against preview environments.", ["k6", "playwright", "axe"], "auto"),
    ("infra", "Infra Ops", "Changes AWS, Kubernetes and Akamai config. Production changes always go through approval.", ["aws.*", "k8s.apply", "akamai.property"], "approval"),
    ("analyst", "Infra Q&A", "Answers questions about your infrastructure, cost and usage.", ["aws.resource_explorer", "cost_explorer"], "auto"),
]

POLICIES = [
    ("prod-changes-need-human", "Production changes need a human approver", "Only Owner / SRE users can approve changes that touch production.", True),
    ("main-needs-review", "Merges to main need one review", "Sidekick can open PRs freely; merging needs an approval step or auto-merge enabled in the workflow.", True),
    ("preview-ns-scope", "Preview environments only in pr-* namespaces", "Developer skill can create/delete namespaces matching pr-* only.", True),
    ("cost-guardrail", "Cost guardrail: block changes adding > $500/mo", "Estimated with the AWS Pricing API before any apply.", True),
    ("read-only", "Read-only mode", "Kill switch. Sidekick can investigate and suggest but not change anything.", False),
]

SETTINGS = {
    "agent_name": "Sidekick",
    "handle": "@sidekick",
    "model": "Claude Sonnet 5",
    "step_delay": [2, 3],
    "brain_mode": "scripted",   # scripted | llm — Settings / Model page toggle
}


def new_uid():
    return uuid.uuid4().hex[:8]
