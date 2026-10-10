"""Sidekick Slack bot — Socket Mode thin client for the Sidekick backend.

Messages and button clicks go to the backend's brain (POST /api/agent/message|action). If the backend starts a
run, the bot posts one message in the thread and keeps updating it from GET /api/runs/{id} until it finishes.
A background loop sends a heartbeat (shown in Dashboard → Integrations → Slack) and delivers the backend's
outbox (e.g. "approved from the dashboard") into the right threads.
"""
import json
import os
import re
import threading
import time

import requests
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()
API = os.getenv("SIDEKICK_API_URL", "http://localhost:8090").rstrip("/")

app = App(token=os.environ["SLACK_BOT_TOKEN"])
ME = app.client.auth_test()
_names = {}


# ---------- backend ----------
def api(method, path, payload=None):
    r = requests.request(method, API + path, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def user_name(client, uid):
    if uid not in _names:
        try:
            u = client.users_info(user=uid)["user"]
            _names[uid] = u.get("real_name") or u.get("name") or uid
        except Exception:  # noqa: BLE001
            _names[uid] = uid
    return _names[uid]


def actor(client, uid):
    return {"source": "slack", "id": uid, "name": user_name(client, uid)}


def event_actor(client, event):
    """People, or another bot (e.g. DevRev handing work to Sidekick) — bot messages have no `user`."""
    if event.get("user"):
        return actor(client, event["user"])
    name = (event.get("bot_profile") or {}).get("name") or event.get("username") or "a bot"
    return {"source": "slack", "id": event.get("bot_id", "bot"), "name": f"{name} (bot)"}


def from_me(event):
    return event.get("bot_id") == ME.get("bot_id") or event.get("user") == ME.get("user_id")


def thread_context(client, channel, event, limit=12):
    """Earlier messages in this thread (people and other bots like DevRev, not Sidekick's own run cards), oldest first."""
    if not event.get("thread_ts"):
        return ""
    try:
        msgs = client.conversations_replies(channel=channel, ts=event["thread_ts"], limit=50)["messages"]
    except Exception:  # noqa: BLE001 — context is a bonus; never block the reply
        return ""
    lines = []
    for m in msgs:
        if m.get("ts") == event.get("ts") or from_me(m) or not (m.get("text") or "").strip():
            continue
        who = user_name(client, m["user"]) if m.get("user") else ((m.get("bot_profile") or {}).get("name") or "bot")
        lines.append(f"{who}: {m['text'].strip()[:700]}")
    return "\n".join(lines[-limit:])


# ---------- rendering ----------
def _sections(text):
    chunks, cur = [], ""
    for line in (text or "").split("\n"):  # Slack section text limit is 3000 chars
        if len(cur) + len(line) > 2800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    return [{"type": "section", "text": {"type": "mrkdwn", "text": c}} for c in chunks + [cur] if c.strip()]


def _actions(actions):
    if not actions:
        return []
    els = []
    for i, a in enumerate(actions):
        b = {"type": "button", "action_id": f"sk_{a['id']}_{i}", "text": {"type": "plain_text", "text": a["label"], "emoji": True},
             "value": json.dumps({"action": a["id"], "value": a.get("value", "")})}
        if a.get("style") in ("primary", "danger"):
            b["style"] = a["style"]
        if a.get("url"):
            b["url"] = a["url"]  # link button: Slack opens it in the browser
        els.append(b)
    return [{"type": "actions", "elements": els}]


def _step_line(s):
    tag = ""
    if s.get("integration"):
        tag = f"  _· {s['integration']} {'✓' if s.get('connected') else 'not connected (simulated)'}_"
    if s["status"] == "running":
        return f"⏳ `{s['tool']}` {s['detail']}…{tag}"
    if s["status"] == "error":
        return f"✕ `{s['tool']}` {s['detail']}  _({s.get('secs', 0)}s)_{tag}"
    return f"✅ `{s['tool']}` {s['detail']}  _({s.get('secs', 0)}s)_{tag}"


def progress_blocks(run):
    return [{"type": "context", "elements": [{"type": "mrkdwn", "text": f"*{run['skill'].title()}* · {run['title']} · working"}]},
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(_step_line(s) for s in run["steps"]) or "…"}}]


def result_blocks(run):
    res = run.get("result") or {}
    tools = " · ".join(f"`{s['tool']}`" for s in run["steps"])
    return ([{"type": "context", "elements": [{"type": "mrkdwn", "text": f"*{run['skill'].title()}* · {run['title']} · {len(run['steps'])} tool calls: {tools}"}]}]
            + _sections(res.get("text", "")) + _actions(res.get("actions")))


# ---------- delivery ----------
def post(client, channel, thread_ts, text, actions=None):
    client.chat_postMessage(channel=channel, thread_ts=thread_ts or None, text=text, blocks=_sections(text) + _actions(actions))


def follow(client, channel, thread_ts, run_id):
    """Post a progress message and keep it in sync with the run until it finishes (runs in its own thread)."""
    def loop():
        msg, last = None, None
        while True:
            try:
                run = api("GET", f"/api/runs/{run_id}")
            except Exception:  # noqa: BLE001
                time.sleep(2)
                continue
            done = run["status"] != "running"
            snapshot = json.dumps([run["status"], run["steps"]])
            if snapshot != last:
                blocks = result_blocks(run) if done else progress_blocks(run)
                text = (run.get("result") or {}).get("text") or f"Sidekick is working on: {run['title']}"
                if msg is None:
                    msg = client.chat_postMessage(channel=channel, thread_ts=thread_ts or None, text=text, blocks=blocks)
                else:
                    client.chat_update(channel=channel, ts=msg["ts"], text=text, blocks=blocks)
                last = snapshot
            if done:
                return
            time.sleep(1)
    threading.Thread(target=loop, daemon=True).start()


def deliver(client, channel, thread_ts, res):
    reply = res.get("reply")
    if reply and reply.get("text"):
        post(client, channel, thread_ts, reply["text"], reply.get("actions"))
    if res.get("run_id"):
        follow(client, channel, thread_ts, res["run_id"])


def safe(client, channel, thread_ts, fn):
    try:
        deliver(client, channel, thread_ts, fn())
    except requests.RequestException as e:
        post(client, channel, thread_ts, f"⚠️ Sidekick's backend isn't reachable at `{API}` ({e.__class__.__name__}). Is `docker compose up` running in `sidekick/`?")


def conv_thread(channel, message_thread_ts, message_ts):
    """Conversation key: the thread root in channels; the whole DM (thread "") unless inside a DM thread."""
    if channel.startswith("D"):
        return message_thread_ts or ""
    return message_thread_ts or message_ts


# ---------- events ----------
@app.event("app_mention")
def on_mention(event, client):
    if from_me(event):
        return  # never react to our own messages (no bot loops)
    ch, thread = event["channel"], conv_thread(event["channel"], event.get("thread_ts"), event["ts"])
    safe(client, ch, thread, lambda: api("POST", "/api/agent/message", {
        "source": "slack", "channel": ch, "thread_ts": thread, "text": event.get("text", ""), "actor": event_actor(client, event),
        "thread_context": thread_context(client, ch, event)}))


@app.event("message")
def on_dm(event, client):
    if event.get("channel_type") != "im" or event.get("bot_id") or event.get("subtype"):
        return  # only plain user messages in DMs
    ch, thread = event["channel"], conv_thread(event["channel"], event.get("thread_ts"), event["ts"])
    safe(client, ch, thread, lambda: api("POST", "/api/agent/message", {
        "source": "slack", "channel": ch, "thread_ts": thread, "text": event.get("text", ""), "actor": actor(client, event["user"])}))


@app.action(re.compile(r"^sk_"))
def on_button(ack, body, client):
    ack()
    payload = json.loads(body["actions"][0]["value"])
    if payload["action"] == "open":
        return  # link button — Slack already opened the URL; keep the buttons
    ch, m, uid = body["channel"]["id"], body["message"], body["user"]["id"]
    thread = conv_thread(ch, m.get("thread_ts"), m["ts"])
    # replace the buttons with who clicked what, so nobody double-clicks
    label = body["actions"][0]["text"]["text"]
    kept = [b for b in m.get("blocks", []) if b.get("type") != "actions"]
    client.chat_update(channel=ch, ts=m["ts"], text=m.get("text", ""),
                       blocks=kept + [{"type": "context", "elements": [{"type": "mrkdwn", "text": f"→ <@{uid}> chose *{label}*"}]}])
    safe(client, ch, thread, lambda: api("POST", "/api/agent/action", {
        "source": "slack", "channel": ch, "thread_ts": thread, "action": payload["action"], "value": payload.get("value", ""),
        "actor": actor(client, uid)}))


# ---------- background: heartbeat + outbox ----------
def background():
    last_hb = 0
    while True:
        try:
            if time.time() - last_hb > 30:
                api("POST", "/api/bot/heartbeat", {"team": ME.get("team"), "bot_user": ME.get("user"), "bot_user_id": ME.get("user_id")})
                last_hb = time.time()
            for item in api("GET", "/api/outbox/pending"):
                post(app.client, item["channel"], item["thread_ts"], item["text"])
                if item.get("run_id"):
                    follow(app.client, item["channel"], item["thread_ts"], item["run_id"])
                api("POST", f"/api/outbox/{item['id']}/delivered")
        except Exception as e:  # noqa: BLE001 — backend down: keep trying
            print(f"[background] {e.__class__.__name__}: {e}")
            last_hb = 0
        time.sleep(3)


if __name__ == "__main__":
    threading.Thread(target=background, daemon=True).start()
    print(f"⚡ Sidekick Slack bot running as @{ME.get('user')} in {ME.get('team')} → backend {API}")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
