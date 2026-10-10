"""Enterprise-Bench "Maple Payments" data + the escalation-triage workflow queries.

Loads the dataset from EB_DATA_DIR (the unzipped artifacts/data.zip from github.com/devrev/enterprise-bench).
Every function returns plain dicts with the record IDs it used, so the MCP layer can show evidence.
Scope (which accounts a teammate may see, whether ARR is visible) is applied here, not in the prompt.
"""
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REFERENCE_DATE = "2026-04-13"  # Enterprise-Bench tasks measure ticket age "to 2026-04-13"; override per call
OPEN_TICKET = {"open", "in_progress", "pending", "new"}
OPEN_ISSUE = {"in_progress", "open", "todo", "backlog", "in_review"}
TIER_DOCS = {"Enterprise": "msa_enterprise_tier.md", "Growth": "msa_growth_tier.md", "Starter": "msa_standard_template.md"}
STOP = set("the a an of to and or in on for with is are was be not when after page from on our we our it this that".split())


def _load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _hours(text):
    """'30 minutes' → 0.5, '2 hours' → 2, '1 business day' → 24 (clock hours, conservative)."""
    t = text.lower()
    n = float(re.search(r"[\d.]+", t).group())
    if "min" in t:
        return n / 60
    if "day" in t:
        return n * 24
    return n


def _words(s):
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 2 and w not in STOP}


class Maple:
    def __init__(self, root):
        root = Path(root)
        if not (root / "crm_json_data" / "tickets.json").exists():
            raise SystemExit(f"Enterprise-Bench data not found in {root}. Run scripts/get-data.sh (see README).")
        self.accounts = {a["account_id"]: a for a in _load(root / "crm_json_data/accounts.json")}
        self.tickets = _load(root / "crm_json_data/tickets.json")
        self.opps = _load(root / "crm_json_data/opportunities.json")
        users = _load(root / "crm_json_data/users.json") + _load(root / "pm_json_data/users.json")
        self.users = {u["user_id"]: u for u in users}
        self.parts = {p["part_id"]: p for p in _load(root / "pm_json_data/maple_parts.json")}
        self.issues = _load(root / "pm_json_data/issues.json")
        self.transcripts = _load(root / "transcripts/transcripts.json")
        self.open_tickets = [t for t in self.tickets if t["status"] in OPEN_TICKET]
        self.issues_by_part = {}
        for i in self.issues:
            if i["status"] in OPEN_ISSUE:
                for c in i.get("components") or []:
                    self.issues_by_part.setdefault(c, []).append(i)
        self.sla = self._parse_sla(root / "internal_docs")

    # ---------- reference data ----------
    def _parse_sla(self, docs):
        """First-response thresholds per tier, parsed from the MSA tier documents (with file:line evidence)."""
        out = {}
        for tier, fname in TIER_DOCS.items():
            path = docs / fname
            if not path.exists():
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                m = re.match(r"^\|\s*\*\*(P\d)\*\*\s*\|\s*([^|]+)\|", line)
                if m:
                    first = re.sub(r"\*|\(std:[^)]*\)", "", m.group(2)).strip()
                    out.setdefault(tier, {})[m.group(1).lower()] = {"text": first, "hours": _hours(first), "source": f"{fname}:{n}"}
        return out

    def user_name(self, uid):
        u = self.users.get(uid) or {}
        return f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or uid

    def part_name(self, pid):
        return (self.parts.get(pid) or {}).get("title", pid)

    # ---------- scope ----------
    def visible_accounts(self, scope):
        if scope.get("accounts") == "owned":
            return {k for k, a in self.accounts.items() if a.get("owner_id") == scope.get("maple_user_id")}
        return set(self.accounts)

    def find_account(self, query, scope):
        q = (query or "").strip().lower()
        allowed = self.visible_accounts(scope)
        hits = [a for k, a in self.accounts.items() if q in (k.lower(), a["account_name"].lower()) or q in a["account_name"].lower()]
        visible = [a for a in hits if a["account_id"] in allowed]
        return visible, len(hits) - len(visible)

    # ---------- workflow: escalation triage ----------
    def related_issues(self, ticket, limit=3):
        """Open engineering issues on the ticket's components, ranked by word overlap with the ticket."""
        tw = _words(ticket["subject"] + " " + ticket.get("description", ""))
        cands = []
        for c in ticket.get("components") or []:
            for i in self.issues_by_part.get(c, []):
                score = len(tw & _words(i["title"])) * 2 + len(tw & _words(i.get("description", "")[:400]))
                if score:
                    cands.append((score, i))
        cands.sort(key=lambda x: (-x[0], x[1]["issue_id"]))
        if cands:  # keep only strong matches: within 60% of the best and not trivially weak
            floor = max(6, cands[0][0] * 0.6)
            cands = [c for c in cands if c[0] >= floor]
        seen, out = set(), []
        for score, i in cands:
            if i["issue_id"] not in seen:
                seen.add(i["issue_id"])
                out.append({"issue_id": i["issue_id"], "title": i["title"], "status": i["status"], "priority": i.get("priority"),
                            "assignee": self.user_name(i.get("assignee_id")), "match_score": score})
            if len(out) >= limit:
                break
        return out

    def sla_status(self, ticket, account, as_of):
        rule = (self.sla.get(account.get("tier")) or {}).get(ticket["priority"])
        if not rule:
            return None
        age_h = (as_of - _ts(ticket["created_at"])).total_seconds() / 3600
        return {"tier": account.get("tier"), "threshold": rule["text"], "threshold_hours": rule["hours"], "age_hours": round(age_h, 1),
                "breached": age_h > rule["hours"], "hours_overdue": round(max(0.0, age_h - rule["hours"]), 1), "source": rule["source"]}

    def escalation_triage(self, scope, priorities=("p0", "p1"), as_of=None):
        as_of = as_of or datetime.now(timezone.utc)
        allowed = self.visible_accounts(scope)
        rows, hidden = [], 0
        for t in self.open_tickets:
            if t["priority"] not in priorities:
                continue
            if t["account_id"] not in allowed:
                hidden += 1
                continue
            acc = self.accounts.get(t["account_id"], {})
            rows.append({"ticket_id": t["ticket_id"], "subject": t["subject"], "priority": t["priority"], "created_at": t["created_at"],
                         "account_id": t["account_id"], "account": acc.get("account_name"), "tier": acc.get("tier"),
                         "arr": acc.get("arr") if scope.get("see_arr") else None,
                         "components": [{"part_id": c, "name": self.part_name(c)} for c in t.get("components") or []],
                         "related_issues": self.related_issues(t), "sla": self.sla_status(t, acc, as_of),
                         "assignee": self.user_name(t.get("assignee_id"))})
        # breached first; then biggest revenue exposure (if the caller may see ARR); then most overdue
        rows.sort(key=lambda r: (not (r["sla"] or {}).get("breached"), -(r["arr"] or 0), -(r["sla"] or {}).get("hours_overdue", 0)))
        return {"as_of": as_of.isoformat(timespec="seconds"), "rows": rows, "hidden_by_scope": hidden}

    def arr_at_risk(self, scope, priorities=("p0", "p1")):
        allowed = self.visible_accounts(scope)
        by_part = {}
        for t in self.open_tickets:
            if t["priority"] in priorities and t["account_id"] in allowed:
                for c in t.get("components") or []:
                    e = by_part.setdefault(c, {"part_id": c, "name": self.part_name(c), "accounts": set(), "tickets": []})
                    e["accounts"].add(t["account_id"])
                    e["tickets"].append(t["ticket_id"])
        rows = []
        for e in by_part.values():
            arr = sum(self.accounts[a].get("arr") or 0 for a in e["accounts"])
            rows.append({"part_id": e["part_id"], "name": e["name"], "total_arr": arr if scope.get("see_arr") else None,
                         "accounts": sorted(e["accounts"]), "open_tickets": len(e["tickets"]), "ticket_ids": e["tickets"]})
        rows.sort(key=lambda r: (-(r["total_arr"] or 0), -r["open_tickets"]))
        return rows

    def account_brief(self, account_id, scope, as_of=None):
        as_of = as_of or datetime.now(timezone.utc)
        a = self.accounts[account_id]
        tickets = [t for t in self.open_tickets if t["account_id"] == account_id]
        opps = [o for o in self.opps if o["account_id"] == account_id and not o["stage"].startswith("closed")]
        calls = [t for t in self.transcripts if t.get("account_id") == account_id]
        return {
            "account": {k: a.get(k) for k in ("account_id", "account_name", "tier", "industry", "region", "country", "employee_count", "status")}
                       | {"arr": a.get("arr") if scope.get("see_arr") else None, "owner": self.user_name(a.get("owner_id"))},
            "open_tickets": [{"ticket_id": t["ticket_id"], "priority": t["priority"], "subject": t["subject"], "created_at": t["created_at"],
                              "components": [self.part_name(c) for c in t.get("components") or []],
                              "related_issues": [i["issue_id"] + " " + i["title"] for i in self.related_issues(t, 2)],
                              "sla": self.sla_status(t, a, as_of)} for t in tickets],
            "open_opportunities": [{"opportunity_id": o["opportunity_id"], "name": o["opportunity_name"], "stage": o["stage"],
                                    "acv": o.get("acv") if scope.get("see_arr") else None, "close": o.get("target_close_date"),
                                    "next_step": o.get("next_step")} for o in opps],
            "calls": [{"transcript_id": c["transcript_id"], "title": c["title"], "date": c.get("date")} for c in calls],
        }

    def search_tickets(self, query, scope, status="open", limit=20):
        allowed = self.visible_accounts(scope)
        words = _words(query)
        pool = self.open_tickets if status == "open" else self.tickets
        hits = []
        for t in pool:
            if t["account_id"] not in allowed:
                continue
            s = len(words & _words(t["subject"] + " " + t.get("description", "")))
            if s:
                hits.append((s, t))
        hits.sort(key=lambda x: (-x[0], x[1]["ticket_id"]))
        return [{"ticket_id": t["ticket_id"], "status": t["status"], "priority": t["priority"], "subject": t["subject"],
                 "account": self.accounts.get(t["account_id"], {}).get("account_name")} for _, t in hits[:limit]], len(hits)

    def stats(self):
        return {"accounts": len(self.accounts), "tickets": len(self.tickets), "open_tickets": len(self.open_tickets),
                "open_by_priority": dict(Counter(t["priority"] for t in self.open_tickets)), "issues": len(self.issues),
                "open_issues": sum(len(v) for v in self.issues_by_part.values()), "parts": len(self.parts),
                "sla_tiers": {k: {p: v["text"] for p, v in d.items()} for k, d in self.sla.items()}}
