# Sidekick: a sidekick for everyone

Sidekick is a pluggable ops/dev agent built for Hack Midwest 2026 (DevRev, Akamai and AWS tracks). Teams talk to it from Slack or DevRev Computer. It investigates incidents, answers infra questions and proposes fixes, and nothing ships until a human approves.

```
Slack ──────────► slack-bot (Socket Mode) ──┐
DevRev Computer ─► devrev-mcp (MCP, :8095) ──┼──► sidekick API (FastAPI) ── brain: scripted ⇄ Strands LLM agent ──► Postgres
Browser ────────► dashboard (:8090) ─────────┘          │
                                                        └─ integrations: AWS · Akamai · GitHub · Prometheus · DevRev …
```

| Folder | What it is |
|---|---|
| [`sidekick/`](sidekick/README.md) | Control plane: API, brain (scripted demo or Strands agent), dashboard, Postgres, Docker Compose |
| [`slack-bot/`](slack-bot/README.md) | Slack app (Socket Mode): a thin client of the Sidekick API |
| [`devrev-mcp/`](devrev-mcp/README.md) | MCP connector for DevRev Computer: escalation triage over Enterprise-Bench, with per-teammate scope and evidence |
| [`docs/`](docs/) | Setup guides for the AWS, Akamai and GitHub integrations, with spending limits |

The demo app Sidekick operates on (ShopLite) lives in its own repository.

## Run it

```bash
cp sidekick/.env.example sidekick/.env               # set SIDEKICK_SECRET_KEY (command inside)
cp slack-bot/.env.example slack-bot/.env             # Slack bot + app tokens
cp devrev-mcp/teammates.example.json devrev-mcp/teammates.json   # one random token per teammate
./devrev-mcp/scripts/get-data.sh                     # Enterprise-Bench dataset (not committed)
cd sidekick && docker compose up -d --build          # dashboard → http://localhost:8090
```

The model and brain mode are set in the dashboard under **Configure → Model**. Providers are Claude on Bedrock, the Anthropic API, OpenAI, and the Akamai AI inference endpoint.
