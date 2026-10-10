# Sidekick — Slack bot (Socket Mode)

Thin client for the Sidekick backend (`../sidekick`). It forwards @mentions, DMs and button clicks to the backend's
brain, renders each run as one threaded message that updates step by step, sends a heartbeat (shown in Dashboard →
Integrations → Slack) and delivers the backend's outbox (e.g. "approved from the dashboard").

App: **Sidekick** (`A0C86KMER4J`) — https://api.slack.com/apps/A0C86KMER4J, created from `manifest.json`.

## Run
Normally via the Sidekick stack: `cd ../sidekick && docker compose up -d` (service `slack-bot`, reads `.env` here).

Standalone (backend on the host at :8090):
```bash
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```
Run only one copy at a time, or Slack splits events between them.

## .env
```
SLACK_BOT_TOKEN=xoxb-…   # OAuth & Permissions → Bot User OAuth Token
SLACK_APP_TOKEN=xapp-…   # Basic Information → App-Level Tokens (connections:write)
SIDEKICK_API_URL=http://localhost:8090   # optional; compose sets http://api:8000
```

## Try
`/invite @sidekick` in a channel, then in one thread:
1. `@sidekick there was a latency spike at 7am, what's the reason?`
2. **Propose a fix** → **Approve** (or approve in Dashboard → Runs & approvals)
3. `@sidekick status` · `@sidekick start over`

Map your Slack member ID in Dashboard → Access control to enforce who can approve.
