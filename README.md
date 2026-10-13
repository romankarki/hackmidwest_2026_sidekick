# Sidekick: a sidekick for everyone

## 🏆 Hack Midwest 2026: competing in the DevRev + AWS + Akamai tracks

Sidekick is a pluggable ops/dev agent built for Hack Midwest 2026 (DevRev, Akamai and AWS tracks). Teams talk to it from Slack or DevRev Computer. It investigates incidents, answers infra questions and proposes fixes, and nothing ships until a human approves.

```
Slack ──────────► slack-bot (Socket Mode) ──┐
DevRev Computer ─► devrev-mcp (MCP, :8095) ──┼──► sidekick API (FastAPI) ── brain: scripted ⇄ Strands LLM agent ──► Postgres
Browser ────────► dashboard (:8090) ─────────┘          │
                                                        └─ integrations: AWS · Akamai · GitHub · Prometheus · DevRev …
```

## Live

**Apps**

| What | URL |
|---|---|
| **ShopLite** (the app Sidekick operates, production) | https://shop.172-234-233-51.sslip.io |
| **Sidekick dashboard** (integrations, model, runs and approvals, previews kill switch) | https://sidekick.172-234-233-51.sslip.io (password-protected; ask the team for access) |
| **DevRev MCP connector** (for DevRev Computer / Agent Studio) | https://mcp.172-234-233-51.sslip.io/mcp (needs a per-teammate token) |

**Previews**

| What | URL |
|---|---|
| PR previews, one per pull request on Akamai LKE (Kubernetes) | `https://pr-<PR number>.172-238-58-56.sslip.io` |
| Demo previews: wishlist / Tinder-style swipe | https://pr-142.172-234-233-51.sslip.io · https://pr-143.172-234-233-51.sslip.io |

**Code and monitoring**

| What | URL |
|---|---|
| Target app repo (Sidekick opens its PRs here) | https://github.com/romankarki/shoplite-target |
| CloudWatch dashboard (ShopLite metrics shipped from Akamai) | `ShopLite-on-Akamai` in AWS us-east-2 |

**Hosting**
- **Akamai Cloud:** VM `sidekick-prod` (Seattle) runs ShopLite and Sidekick. The LKE cluster `sidekick-previews` runs the PR previews.
- **AWS:** Amazon Bedrock runs the model; CloudWatch handles metrics and alarms.
- **DevRev:** customer signals, plus the Agent Studio agents.

Deploys run on every push through GitHub Actions; see [`deploy/README.md`](deploy/README.md).

## Repository

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
