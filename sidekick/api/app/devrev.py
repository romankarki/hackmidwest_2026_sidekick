"""DevRev as Sidekick's source of customer context (Integrations → DevRev).

• ask_agent(): consult an Agent Studio agent (e.g. "Customer Signals Hub", "Engineering Standards Guide") over the
  agents API (ai-agents.events.execute-sync, a server-sent event stream). One DevRev session per Slack thread keeps
  the agent's memory across follow-ups.
• sync_feedback_chat(): AirSync stores a Slack channel as a chat whose comments DevRev search (and therefore agents)
  can't see; copy each new feedback message into a ticket on the matching ShopLite part. Idempotent.
• comment() / resolve_tickets(): write-back — PR + preview links as comments on the tickets a change came from.
"""
import json
import re
import threading
import time

import requests

from . import integrations

API = "https://api.devrev.ai"
TIMEOUT = 20


class DevRevError(RuntimeError):
    pass


def cfg():
    c = integrations.creds("devrev")
    if not c.get("pat"):
        raise DevRevError("DevRev is not connected. Add a personal access token in Sidekick → Integrations → DevRev.")
    return c


def call(endpoint, body=None, timeout=TIMEOUT):
    r = requests.post(f"{API}/{endpoint}", headers={"Authorization": cfg()["pat"], "Content-Type": "application/json"},
                      json=body or {}, timeout=timeout)
    if r.status_code >= 400:
        raise DevRevError(f"DevRev {endpoint} → HTTP {r.status_code}: {r.text[:200]}")
    return r.json() if r.content else {}


# ---------- consult an Agent Studio agent ----------
def ask_agent(agent_id, message, session, timeout=150):
    """Returns {"answer", "skills", "seconds"}; raises DevRevError on failure or timeout."""
    t0 = time.time()
    body = {"agent": agent_id, "event": {"input_message": {"message": message}}, "session_object": session}
    try:
        r = requests.post(f"{API}/internal/ai-agents.events.execute-sync", json=body, stream=True, timeout=(10, timeout),
                          headers={"Authorization": cfg()["pat"], "Content-Type": "application/json"})
    except requests.RequestException as e:
        raise DevRevError(f"DevRev agent call failed: {e}") from e
    if r.status_code >= 400:
        raise DevRevError(f"DevRev agent → HTTP {r.status_code}: {r.text[:200]}")
    skills, answer, error = [], "", None
    for line in r.iter_lines(decode_unicode=True):
        if time.time() - t0 > timeout:
            raise DevRevError(f"DevRev agent didn't answer within {timeout}s")
        if not line or not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:])
        except ValueError:
            continue
        if ev.get("response") == "progress":
            st = (ev.get("progress") or {}).get("skill_triggered")
            if st:
                skills.append(st.get("skill_name"))
        elif ev.get("response") == "error" or ev.get("agent_response") == "error":
            error = json.dumps(ev.get("error") or ev)[:300]
        elif "message" in ev:
            answer = ev.get("message") or answer
    if not answer:
        raise DevRevError(error or "DevRev agent returned no answer")
    return {"answer": answer, "skills": skills, "seconds": round(time.time() - t0, 1)}


DON_RE = re.compile(r"<?(don:core:[\w:/.-]+?/(ticket|issue|article|account|conversation)/(\d+))>?")
PREFIX = {"ticket": "TKT", "issue": "ISS", "article": "ART", "account": "ACC", "conversation": "CONV"}


def readable(answer):
    """Turn <don:core:…/ticket/9> references into TKT-9 so Slack readers see familiar IDs."""
    return DON_RE.sub(lambda m: f"{PREFIX[m.group(2)]}-{m.group(3)}", answer)


# ---------- write-back ----------
def comment(object_id, body):
    return call("timeline-entries.create", {"type": "timeline_comment", "object": object_id, "body": body})


def resolve_tickets(display_ids):
    """['TKT-9', ...] → [{"display_id", "id", "title"}] for the ones that exist."""
    want = {d.strip().upper() for d in display_ids if d and d.strip()}
    out = []
    if not want:
        return out
    for w in call("works.list", {"type": ["ticket", "issue"], "limit": 100}).get("works", []):
        if w.get("display_id") in want:
            out.append({"display_id": w["display_id"], "id": w["id"], "title": w.get("title", "")})
    return out


# ---------- Slack chat → searchable tickets ----------
def _theme(text, parts):
    t = text.lower()
    if ("sort" in t or "price" in t) and "Catalog & sorting" in parts:
        return parts["Catalog & sorting"], "sorting"
    if ("wishlist" in t or "save for later" in t) and "Wishlist" in parts:
        return parts["Wishlist"], "wishlist"
    if "checkout" in t and "Checkout" in parts:
        return parts["Checkout"], "checkout"
    return parts.get("ShopLite") or next(iter(parts.values())), "other"


def sync_feedback_chat(chat=None):
    """Copy new messages from the synced Slack chat into tickets. Returns the display IDs created."""
    c = cfg()
    chat = chat or c.get("feedback_chat")
    if not chat:
        return []
    parts = {p["name"]: p["id"] for p in call("parts.list").get("parts", [])}
    existing = " ".join(w.get("body") or "" for w in call("works.list", {"type": ["ticket"], "limit": 100}).get("works", []))
    me = call("dev-users.self")["dev_user"]["id"]
    made = []
    for e in call("timeline-entries.list", {"object": chat, "limit": 100}).get("timeline_entries", []):
        text = (e.get("body") or "").strip()
        if not text or "has entered the channel" in text or e["id"] in existing or text[:120] in existing:
            continue
        who = (e.get("created_by") or {}).get("display_name") or "someone"
        first = re.split(r"(?<=[.!?])\s", re.sub(r"^\w[\w ]*:\s*", "", text), maxsplit=1)[0]
        part, theme = _theme(text, parts)
        body = (f"> {text}\n\nSource: Slack #customer-feedback (DevRev chat {chat}), posted by {who} on "
                f"{e.get('created_date', '')[:16].replace('T', ' ')} UTC. Theme: {theme}.\nslack-entry: {e['id']}")
        w = call("works.create", {"type": "ticket", "title": f"[Slack] {first[:90]}", "body": body, "applies_to_part": part,
                                  "owned_by": [me]}).get("work", {})
        if w.get("display_id"):
            made.append(w["display_id"])
    return made


def start_feedback_sync(interval=300):
    """Background loop: every 5 min copy new Slack feedback into tickets (no-op until configured)."""
    def loop():
        while True:
            try:
                if integrations.creds("devrev").get("feedback_chat"):
                    made = sync_feedback_chat()
                    if made:
                        print(f"[devrev] Slack feedback → tickets: {made}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[devrev] feedback sync: {e}", flush=True)
            time.sleep(interval)
    threading.Thread(target=loop, daemon=True, name="devrev-feedback-sync").start()
