"""Sidekick × DevRev Computer — MCP connector (Streamable HTTP).

Workflow it solves on the Enterprise-Bench "Maple Payments" dataset: **escalation triage** — turn open customer
pain (support tickets) into engineering priorities with revenue and contract (SLA) context, then hand the worst
one to Sidekick to investigate and fix. Every answer carries evidence (record IDs, document:line) and the caller's
access scope; teammates authenticate with personal tokens and only see what their role allows.

Run:  python server.py            → http://localhost:8095/mcp   (Authorization: Bearer <teammate token>)
"""
import json
import os
import time
from datetime import datetime, timezone

import requests
import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from maple import REFERENCE_DATE, Maple

DATA_DIR = os.getenv("EB_DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
TEAMMATES_FILE = os.getenv("TEAMMATES_FILE", os.path.join(os.path.dirname(__file__), "teammates.json"))
SIDEKICK = os.getenv("SIDEKICK_API_URL", "http://localhost:8090").rstrip("/")
DASHBOARD = os.getenv("SIDEKICK_DASHBOARD_URL", "http://localhost:8090")
PORT = int(os.getenv("PORT", "8095"))

maple = Maple(DATA_DIR)
with open(TEAMMATES_FILE, encoding="utf-8") as f:
    TEAMMATES = {t["token"]: t for t in json.load(f)["teammates"]}

mcp = MCPServer(
    name="sidekick-escalations",
    title="Sidekick — Escalation triage (Maple Payments)",
    instructions=(
        "Tools for the escalation-triage workflow over Maple Payments data (Enterprise-Bench): open P0/P1 tickets with "
        "their product component, related engineering issue, account, ARR and contractual SLA status; ARR at risk by "
        "product area; account briefs; and hand-off to Sidekick to investigate/fix. Results are scoped to the caller's "
        "role — say so when rows are hidden. Always keep the Evidence section when you summarise a result."),
)


# ---------- identity ----------
def teammate(ctx: Context) -> dict:
    auth = (ctx.headers or {}).get("authorization", "")
    t = TEAMMATES.get(auth.removeprefix("Bearer ").strip())
    if not t:
        raise PermissionError("Unauthorized — use your personal Sidekick connector token.")
    return t


def scope_line(t, hidden=0):
    acc = "all accounts" if t["accounts"] == "all" else f"only accounts owned by {t['name']}"
    extra = [] if t.get("see_arr") else ["ARR/ACV hidden for this role"]
    if hidden:
        extra.append(f"{hidden} matching record(s) outside your scope not shown")
    return f"_Scope: {t['name']} ({t['role']}) — {acc}" + (" · " + " · ".join(extra) if extra else "") + "_"


def money(v):
    return "🔒 hidden" if v is None else f"${v:,.0f}"


def as_of_date(as_of: str):
    s = (as_of or REFERENCE_DATE).strip()
    return datetime.fromisoformat(s + ("T00:00:00+00:00" if len(s) == 10 else "")).astimezone(timezone.utc)


def audit(t, tool, args, summary, evidence):
    """Record the tool call in Sidekick's Runs page (source: DevRev) — best effort."""
    try:
        requests.post(f"{SIDEKICK}/api/connector/log", timeout=3, json={
            "tool": tool, "args": args, "summary": summary[:1500], "evidence": evidence[:40],
            "actor": {"source": "devrev", "id": t.get("email"), "name": t["name"]}})
    except requests.RequestException:
        pass


# ---------- tools ----------
@mcp.tool()
def whoami(ctx: Context) -> str:
    """Show which teammate you're authenticated as and what you're allowed to see and do."""
    t = teammate(ctx)
    s = maple.stats()
    return (f"You are *{t['name']}* ({t['role']}).\n{scope_line(t)}\n"
            f"Can approve Sidekick changes: {'yes' if t.get('can_approve') else 'no'}\n\n"
            f"Dataset: Maple Payments (Enterprise-Bench) — {s['accounts']} accounts, {s['tickets']:,} tickets ({s['open_tickets']} open), "
            f"{s['issues']:,} engineering issues ({s['open_issues']:,} open), {s['parts']} product parts. Default as-of date {REFERENCE_DATE}.")


@mcp.tool()
def escalation_triage(ctx: Context, priority: str = "p0,p1", as_of: str = "", limit: int = 15) -> str:
    """The core workflow: rank open high-priority customer tickets by SLA breach and revenue at risk, each with its
    product component, the most related open engineering issue, account, tier and contractual first-response SLA.

    Args:
        priority: Comma-separated ticket priorities to include, e.g. "p0,p1" or "p1"
        as_of: Date (YYYY-MM-DD) to measure ticket age against; defaults to the Enterprise-Bench reference date 2026-04-13
        limit: Max rows to show
    """
    t = teammate(ctx)
    res = maple.escalation_triage(t, tuple(p.strip().lower() for p in priority.split(",")), as_of_date(as_of))
    rows = res["rows"][: max(1, min(limit, 50))]
    lines = [f"*Escalation triage* — {len(res['rows'])} open {priority.upper()} ticket(s) as of {res['as_of'][:10]}\n",
             "| # | Ticket | Account (tier) | ARR | Component | Related eng issue | SLA first response |",
             "|---|---|---|---|---|---|---|"]
    ev = set()
    for n, r in enumerate(rows, 1):
        iss = r["related_issues"][0] if r["related_issues"] else None
        sla = r["sla"]
        sla_txt = (f"⛔ breached by {sla['hours_overdue'] / 24:,.1f} d (limit {sla['threshold']})" if sla and sla["breached"]
                   else f"✅ within {sla['threshold']}" if sla else "n/a")
        comp = ", ".join(f"{c['name']} ({c['part_id']})" for c in r["components"]) or "—"
        lines.append(f"| {n} | {r['ticket_id']} {r['priority'].upper()}: {r['subject'][:60]} | {r['account']} ({r['tier']}) | {money(r['arr'])} "
                     f"| {comp} | {(iss['issue_id'] + ' ' + iss['title'][:45] + ' · ' + iss['assignee']) if iss else 'none found'} | {sla_txt} |")
        ev |= {f"{r['ticket_id']} (tickets.json)", f"{r['account_id']} (accounts.json)"} | {f"{c['part_id']} (maple_parts.json)" for c in r["components"]}
        if iss:
            ev.add(f"{iss['issue_id']} (issues.json)")
        if sla:
            ev.add(f"SLA {r['tier']} {r['priority'].upper()} = {sla['threshold']} ({sla['source']})")
    breached = sum(1 for r in res["rows"] if r["sla"] and r["sla"]["breached"])
    lines += ["", f"*{breached} of {len(res['rows'])} breach their contractual first-response SLA.* "
                  f"Next: `escalate_to_sidekick` with the top ticket to have Sidekick investigate and propose a fix.",
              "", scope_line(t, res["hidden_by_scope"]), "", "*Evidence:* " + " · ".join(sorted(ev))]
    out = "\n".join(lines)
    audit(t, "escalation_triage", {"priority": priority, "as_of": as_of or REFERENCE_DATE}, out.split("\n")[0] + f" — top: {rows[0]['ticket_id'] if rows else 'none'}", sorted(ev))
    return out


@mcp.tool()
def arr_at_risk_by_component(ctx: Context, priority: str = "p0,p1") -> str:
    """Total ARR of accounts with at least one open ticket of the given priorities, broken down by product area (PART),
    ranked highest first — tells engineering which components to prioritise by revenue exposure.

    Args:
        priority: Comma-separated ticket priorities, e.g. "p0,p1"
    """
    t = teammate(ctx)
    rows = maple.arr_at_risk(t, tuple(p.strip().lower() for p in priority.split(",")))
    lines = [f"*ARR at risk by product area* (open {priority.upper()} tickets)\n", "| Rank | Part | Product area | ARR at risk | Accounts | Open tickets |", "|---|---|---|---|---|---|"]
    for n, r in enumerate(rows, 1):
        names = ", ".join(maple.accounts[a]["account_name"] for a in r["accounts"])
        lines.append(f"| {n} | {r['part_id']} | {r['name']} | {money(r['total_arr'])} | {names} | {r['open_tickets']} |")
    ev = sorted({f"{r['part_id']} (maple_parts.json)" for r in rows} | {f"{x} (tickets.json)" for r in rows for x in r["ticket_ids"]})
    out = "\n".join(lines + ["", scope_line(t), "", "*Evidence:* " + " · ".join(ev[:60])])
    audit(t, "arr_at_risk_by_component", {"priority": priority}, out.split("\n")[0] + (f" — #1 {rows[0]['name']}" if rows else ""), ev)
    return out


@mcp.tool()
def sla_breaches(ctx: Context, priority: str = "p1", as_of: str = "") -> str:
    """Every open ticket whose age exceeds the first-response SLA in its account's MSA tier (thresholds read from the
    tier contracts), with ticket age and hours overdue.

    Args:
        priority: Comma-separated priorities, e.g. "p1" or "p0,p1"
        as_of: Date (YYYY-MM-DD); defaults to the Enterprise-Bench reference date 2026-04-13
    """
    t = teammate(ctx)
    res = maple.escalation_triage(t, tuple(p.strip().lower() for p in priority.split(",")), as_of_date(as_of))
    rows = [r for r in res["rows"] if r["sla"] and r["sla"]["breached"]]
    rows.sort(key=lambda r: -r["sla"]["hours_overdue"])
    lines = [f"*SLA first-response breaches* — {len(rows)} ticket(s) as of {res['as_of'][:10]}\n",
             "| Ticket | Account | Tier | Ticket age (days) | SLA threshold (hours) | Hours overdue |", "|---|---|---|---|---|---|"]
    for r in rows:
        s = r["sla"]
        lines.append(f"| {r['ticket_id']} | {r['account']} | {r['tier']} | {s['age_hours'] / 24:,.1f} | {s['threshold_hours']:g} | {s['hours_overdue']:,.1f} |")
    sources = sorted({f"{r['tier']} {r['priority'].upper()} ({r['sla']['source']})" for r in rows})
    out = "\n".join(lines + ["", scope_line(t, res["hidden_by_scope"]), "", "*Evidence:* thresholds from " + " · ".join(sources)
                             + " · tickets: " + " · ".join(r["ticket_id"] for r in rows)])
    audit(t, "sla_breaches", {"priority": priority, "as_of": as_of or REFERENCE_DATE}, out.split("\n")[0], sources)
    return out


@mcp.tool()
def account_brief(ctx: Context, account: str, as_of: str = "") -> str:
    """A one-page brief on a customer account: tier, ARR, owner, open tickets with components / related engineering
    issues / SLA status, open opportunities and recent calls. Respects the caller's account scope.

    Args:
        account: Account name (or part of it) or ID, e.g. "Vantara" or "ACC-007"
        as_of: Date (YYYY-MM-DD) for SLA ages; defaults to 2026-04-13
    """
    t = teammate(ctx)
    found, hidden = maple.find_account(account, t)
    if not found:
        msg = f"No account matching {account!r} in your scope." + (f" ({hidden} match(es) exist but belong to other owners.)" if hidden else "")
        audit(t, "account_brief", {"account": account}, msg, [])
        return msg + "\n" + scope_line(t)
    b = maple.account_brief(found[0]["account_id"], t, as_of_date(as_of))
    a = b["account"]
    lines = [f"*{a['account_name']}* ({a['account_id']}) — {a['tier']} tier · {a['industry']}, {a['region']} · ARR {money(a['arr'])} · owner {a['owner']}", "",
             f"*Open tickets ({len(b['open_tickets'])})*"]
    for tk in b["open_tickets"]:
        s = tk["sla"]
        lines.append(f"• {tk['ticket_id']} {tk['priority'].upper()} — {tk['subject']} · {', '.join(tk['components'])}"
                     + (f" · ⛔ SLA breached ({s['threshold']})" if s and s["breached"] else "")
                     + (f"\n   ↳ related: {'; '.join(tk['related_issues'])}" if tk["related_issues"] else ""))
    lines += ["", f"*Open opportunities ({len(b['open_opportunities'])})*"]
    lines += [f"• {o['opportunity_id']} {o['name']} — {o['stage']} · ACV {money(o['acv'])} · close {o['close']} · next: {o['next_step']}" for o in b["open_opportunities"]] or ["• none"]
    if b["calls"]:
        lines += ["", "*Calls*"] + [f"• {c['transcript_id']} {c['title']} ({c['date']})" for c in b["calls"]]
    ev = [f"{a['account_id']} (accounts.json)"] + [f"{x['ticket_id']} (tickets.json)" for x in b["open_tickets"]] + \
         [f"{o['opportunity_id']} (opportunities.json)" for o in b["open_opportunities"]] + [f"{c['transcript_id']} (transcripts.json)" for c in b["calls"]]
    out = "\n".join(lines + ["", scope_line(t), "", "*Evidence:* " + " · ".join(ev)])
    audit(t, "account_brief", {"account": account}, lines[0], ev)
    return out


@mcp.tool()
def search_tickets(ctx: Context, query: str, status: str = "open", limit: int = 15) -> str:
    """Keyword search over support tickets (subject + description) within your scope.

    Args:
        query: Words to look for, e.g. "refund failed"
        status: "open" (default) or "all"
        limit: Max results
    """
    t = teammate(ctx)
    hits, total = maple.search_tickets(query, t, status, max(1, min(limit, 50)))
    lines = [f"*{total} ticket(s)* matching {query!r} ({status})"] + [f"• {h['ticket_id']} [{h['status']}, {h['priority'].upper()}] {h['subject']} — {h['account']}" for h in hits]
    out = "\n".join(lines + ["", scope_line(t)])
    audit(t, "search_tickets", {"query": query, "status": status}, lines[0], [h["ticket_id"] for h in hits])
    return out


# ---------- Sidekick hand-off ----------
def _sidekick_turn(t, text, thread):
    r = requests.post(f"{SIDEKICK}/api/agent/message", timeout=15, json={
        "source": "devrev", "channel": "devrev-computer", "thread_ts": thread, "text": text,
        "actor": {"source": "devrev", "id": t.get("email"), "name": t["name"]}}).json()
    parts = [r["reply"]["text"]] if r.get("reply") and r["reply"].get("text") else []
    run = None
    if r.get("run_id"):
        for _ in range(240):  # up to ~2 min
            run = requests.get(f"{SIDEKICK}/api/runs/{r['run_id']}", timeout=10).json()
            if run["status"] != "running":
                break
            time.sleep(0.5)
        steps = "\n".join(f"  {'✓' if s['status'] == 'done' else '✕' if s['status'] == 'error' else '…'} {s['tool']} — {s['detail']}" for s in run["steps"])
        parts += [f"_Sidekick run #{run['id']} · {run.get('model') or ''}_\n{steps}", (run.get("result") or {}).get("text", "")]
        if any(a.get("id") == "approve" for a in (run.get("result") or {}).get("actions", [])):
            parts.append(f"⚠️ A change is waiting for approval — an approver can use `sidekick_decide`, or open {DASHBOARD}/#runs")
    return "\n\n".join(p for p in parts if p)


@mcp.tool()
def escalate_to_sidekick(ctx: Context, ticket_id: str) -> str:
    """Hand an escalated ticket to Sidekick: it opens an incident thread with the ticket's evidence (account, ARR, component,
    related engineering issue, SLA) and Sidekick investigates and proposes a fix for human approval.

    Args:
        ticket_id: e.g. "TKT-119"
    """
    t = teammate(ctx)
    tk = next((x for x in maple.open_tickets if x["ticket_id"].lower() == ticket_id.strip().lower()), None)
    if not tk or tk["account_id"] not in maple.visible_accounts(t):
        return f"Open ticket {ticket_id} not found in your scope.\n" + scope_line(t)
    acc = maple.accounts[tk["account_id"]]
    iss = maple.related_issues(tk, 1)
    sla = maple.sla_status(tk, acc, as_of_date(""))
    text = (f"Customer incident escalated from DevRev: {tk['ticket_id']} ({tk['priority'].upper()}) \"{tk['subject']}\" — "
            f"{acc['account_name']}, {acc['tier']} tier, ARR ${acc.get('arr', 0):,}. Component: {', '.join(maple.part_name(c) for c in tk.get('components') or [])}. "
            + (f"Related engineering issue {iss[0]['issue_id']} \"{iss[0]['title']}\" ({iss[0]['status']}, {iss[0]['assignee']}). " if iss else "")
            + (f"SLA first response {sla['threshold']} breached by {sla['hours_overdue'] / 24:,.1f} days. " if sla and sla["breached"] else "")
            + "Investigate the incident and propose a fix.")
    out = _sidekick_turn(t, text, f"esc-{tk['ticket_id']}")
    return f"*Escalated {tk['ticket_id']} to Sidekick* (thread esc-{tk['ticket_id']}; also visible in {DASHBOARD}/#runs)\n\n{out}"


@mcp.tool()
def ask_sidekick(ctx: Context, question: str, thread: str = "") -> str:
    """Ask Sidekick (the ops/dev agent) anything — infra health, latency, cost, builds. Same brain as Slack.

    Args:
        question: What to ask, e.g. "is checkout healthy right now?"
        thread: Optional conversation key to continue a previous thread
    """
    t = teammate(ctx)
    return _sidekick_turn(t, question, thread or f"ask-{t['name'].split()[0].lower()}")


@mcp.tool()
def sidekick_pending_approvals(ctx: Context) -> str:
    """List Sidekick changes waiting for human approval."""
    teammate(ctx)
    rows = requests.get(f"{SIDEKICK}/api/approvals?status=pending", timeout=10).json()
    if not rows:
        return "No changes are waiting for approval."
    return "\n".join(f"• #{a['id']} {a['title']} — requested by {a.get('requested_by')} via {a['source']}\n  {a['summary'][:300]}" for a in rows)


@mcp.tool()
def sidekick_decide(ctx: Context, approval_id: int, decision: str) -> str:
    """Approve or reject a pending Sidekick change. Only teammates with approval rights can do this.

    Args:
        approval_id: The # from sidekick_pending_approvals
        decision: "approve" or "reject"
    """
    t = teammate(ctx)
    if decision not in ("approve", "reject"):
        return 'decision must be "approve" or "reject"'
    if not t.get("can_approve"):
        audit(t, "sidekick_decide", {"approval_id": approval_id, "decision": decision}, "⛔ denied — no approval rights", [])
        return f"⛔ {t['name']} ({t['role']}) can't approve production changes. Ask an Owner/SRE."
    r = requests.post(f"{SIDEKICK}/api/approvals/{approval_id}/decide", timeout=15,
                      json={"decision": decision, "actor": {"source": "devrev", "id": t.get("email"), "name": f"{t['name']} (DevRev)"}}).json()
    return (r.get("reply") or {}).get("text") or json.dumps(r)[:500]


# ---------- HTTP app with token auth ----------
def build_app():
    # DevRev Computer reaches us through a public tunnel hostname. Every /mcp request must carry a secret teammate token
    # (checked below), so Host-header checks are optional: set ALLOWED_HOSTS="abc.trycloudflare.com,localhost:8095" to enforce.
    allowed = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()]
    security = TransportSecuritySettings(enable_dns_rebinding_protection=bool(allowed), allowed_hosts=allowed,
                                         allowed_origins=[f"https://{h}" for h in allowed])
    inner = mcp.streamable_http_app(streamable_http_path="/mcp", host="0.0.0.0", transport_security=security)

    async def app(scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            headers = dict(scope.get("headers") or [])
            token = headers.get(b"authorization", b"").decode().removeprefix("Bearer ").strip()
            if not token:  # some UIs only take a URL: accept ?key=<token> and turn it into the header
                from urllib.parse import parse_qs
                token = (parse_qs(scope.get("query_string", b"").decode()).get("key") or [""])[0]
                if token:
                    scope = {**scope, "headers": [*scope["headers"], (b"authorization", f"Bearer {token}".encode())]}
            if token not in TEAMMATES:
                await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized - use your Sidekick connector token"}'})
                return
        if scope["type"] == "http" and scope["path"] == "/health":
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": json.dumps({"status": "ok", "teammates": len(TEAMMATES), **maple.stats()}).encode()})
            return
        await inner(scope, receive, send)
    return app


if __name__ == "__main__":
    print(f"⚡ Sidekick DevRev connector on http://0.0.0.0:{PORT}/mcp · {len(TEAMMATES)} teammates · data {DATA_DIR} · Sidekick {SIDEKICK}")
    uvicorn.run(build_app(), host="0.0.0.0", port=PORT, log_level="info")
