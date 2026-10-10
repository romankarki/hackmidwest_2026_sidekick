---
name: escalation-triage
description: Turn open customer escalations into ranked, evidence-backed engineering priorities (SLA breach + revenue at risk + linked engineering issue) and hand the worst one to Sidekick to fix. Uses the sidekick-escalations MCP connector over Maple Payments (Enterprise-Bench) data.
---

# Escalation triage (Sidekick connector)

Use this skill when someone asks things like:
- "What should engineering fix first?" / "Which escalations are burning?"
- "Which P1s breach SLA?" / "How much ARR is at risk, and where?"
- "Brief me on <account> before my call"
- "Get someone on TKT-119" / "Have Sidekick fix it"

## Procedure

1. **Know who's asking.** Call `whoami` once per conversation. Results are scoped to the caller's token:
   - account executives see only accounts they own;
   - engineers don't see ARR;
   - only approvers can approve changes.

   Never try to work around the scope. If rows are hidden, say how many.
2. **Triage.** Call `escalation_triage` (default `priority="p0,p1"`, as-of date 2026-04-13). For the top 3–5 rows, report:
   - ticket, account and tier;
   - ARR (if visible);
   - component;
   - related engineering issue and its owner;
   - SLA status.
3. **Quantify** (if asked "where" or "how much"): call `arr_at_risk_by_component` to rank product areas, and `sla_breaches` for the contractual view.
4. **Account context** (if one account is in focus): call `account_brief`. It covers:
   - open tickets;
   - open opportunities and their next steps (e.g. "get engineering ETA on ISS-017");
   - recent calls.
5. **Act.** On request, call `escalate_to_sidekick` with the ticket ID. Sidekick investigates and files a fix plan that **waits for human approval**. Tell the user who can approve. Approvers use `sidekick_pending_approvals` → `sidekick_decide`.
6. **Ask ops questions**, such as infra health, latency or cost: call `ask_sidekick`.

## Output rules

- Lead with the answer: the 1–3 tickets to act on and why. Revenue, SLA breach and a known engineering issue are the reasons.
- **Always keep the `Evidence:` line** from tool results. It lists record IDs (`TKT-…`, `ACC-…`, `ISS-…`, `PART-…`) and contract citations (`msa_*.md:line`). Don't invent IDs or numbers that a tool didn't return.
- Keep the `Scope:` line so a reader knows what the answer could see.
- State the as-of date used for SLA ages.
