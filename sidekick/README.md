# Sidekick — control plane

Dashboard + API + Postgres + Slack bot, all in Docker Compose.

```
Slack ──► slack-bot (Socket Mode) ──┐
                                    ├──► api (FastAPI): brain · runs · integrations ──► Postgres
Browser ──► dashboard (served by api)┤
DevRev Computer ──MCP──► devrev-mcp ─┘   (:8095, per-teammate tokens — see ../devrev-mcp/README.md)
```

```bash
cd sidekick
docker compose up -d --build        # → http://localhost:8090   (bound to 127.0.0.1 — no login yet)
docker compose logs -f api slack-bot
```
`.env` holds `SIDEKICK_SECRET_KEY` (encrypts integration secrets — don't lose or change it), owner name/email and ports.
The Slack bot reads its tokens from `../slack-bot/.env`. Run only one copy of the bot at a time.
The `devrev-mcp` service needs `../devrev-mcp/data` (run `../devrev-mcp/scripts/get-data.sh`) and `../devrev-mcp/teammates.json`.

## What's real now
| Area | Backed by |
|---|---|
| **Ask Sidekick** console | same brain + conversation state as Slack; runs stream live |
| **Workflows** | `workflows` table — edits save automatically; the brain ships fixes using the steps after *Human approval* |
| **Runs & approvals** | `runs` / `approvals` tables; approving here posts into the original Slack thread (via `outbox`) |
| **Model** | `settings.model_config` — provider (Amazon Bedrock via `AnthropicBedrockMantle`, the Anthropic API, or OpenAI — OpenAI models load live from your account), model (default Claude Opus 5; Bedrock IDs get the `anthropic.` prefix), region, effort, max tokens, per-skill overrides, request preview and a real **Test model** call. `api/app/models.py: resolve(skill)` is what the Strands agent will read; every run stores the model label |
| **Replies** | `custom_replies` table — your own phrase → answer pairs (contains / exact / regex), checked before any built-in behaviour, in Slack and the console; placeholders `{agent_name}` `{user}` `{status}` `{dashboard_url}` |
| **Skills** | toggles + autonomy; a disabled skill makes Sidekick refuse that kind of request |
| **Integrations** | config + Fernet-encrypted secrets; **Test** makes a real read-only call (below) |
| **Access control** | users/roles + Slack member IDs (approver checks) and guardrail policies (`read-only`, `prod-changes-need-human`, …) |
| **Settings** | agent name/handle/model, simulated step duration |

### Integration tests
| Integration | Test call |
|---|---|
| AWS | `sts:GetCallerIdentity` (+ `eks:DescribeCluster` if a cluster is set). Empty keys → default credential chain |
| Anthropic API · OpenAI | list models with the API key (the Model page's **Test model** makes a real request to the chosen model; Bedrock uses the AWS credentials) |
| Akamai | EdgeGrid-signed `GET /papi/v1/groups` |
| GitHub | `GET /user` (+ `GET /repos/{repo}` permissions) |
| ShopLite | `GET {url}/api/health` (default `http://host.docker.internal:8080`) |
| Prometheus / Grafana | `/api/v1/status/buildinfo` · `/api/search` with the token |
| DevRev | `GET https://api.devrev.ai/dev-users.self` with the PAT |
| Slack | bot heartbeat (every 30 s) |

## Local demo for friends (≈5 min)
Everything runs on your laptop; Sidekick's answers are scripted, but the apps and links are real.

```bash
cd shoplite && docker compose up -d --build --wait     # ShopLite main        → http://localhost:8080
bash scripts/demo-previews.sh up                       # previews: pr-142 :8142 (wishlist) · pr-143 :8143 (Tinder-style)
cd ../sidekick && docker compose up -d --build --wait  # Sidekick dashboard   → http://localhost:8090  (+ Slack bot)
```
1. Show **ShopLite** at :8080 — a normal shop.
2. In Slack (or Dashboard → Ask Sidekick): `@sidekick build the wishlist feature from TKT-1039` → steps run → **Open pr-142 preview** → ♥ products, wishlist drawer.
3. Same ticket, different prompt: `@sidekick build TKT-1039 as a tinder style shopping experience` → **Open pr-143 preview** → swipe right = wishlist, left = skip, up = cart.
4. The incident story: `@sidekick there was a latency spike at 7am, what's the reason?` → **Propose a fix** → **Approve** (or approve in Runs & approvals).
5. Tour: Workflows (switch "after approval" to straight-to-prod and rerun 4), Model, Replies (`where are you from`), Integrations.

If a preview isn't running, Sidekick says so in its answer instead of posting a dead link. Stop the previews with `bash scripts/demo-previews.sh down`.

## The brain (v0: if/else on conversation status)
Lives in `api/app/brain.py`. One conversation per Slack thread / DM / dashboard console session:

```
new ─"spike/slow/why"─► investigating ─► root_caused ─"fix it"─► proposing_fix ─► awaiting_approval
awaiting_approval ─approve─► shipping ─► resolved (or pr_open if the workflow only raises a PR)
                  └─reject──► rejected ─"fix it"─► propose again
"build wishlist" / "tinder style" ─► building ─► pr_open    "test", "cost" ─► one-off run    "status", "start over"
```
Rules it enforces: no fix before an investigation · one thing at a time per conversation · approve only when something
is waiting · Owner/SRE only when `prod-changes-need-human` is on (unmapped Slack users are allowed until at least one
Slack ID is mapped) · `read-only` blocks shipping and building · disabled skills · paused Incident workflow.

Steps are **simulated** (2–3 s each, configurable) and labelled with the integration they'd use and whether it's connected.

## LLM mode (Strands agent) — `api/app/llm_agent.py`
**Configure → Model → Brain mode: Scripted demo ⇄ LLM agent.** In LLM mode, custom Replies, `status`, approve/reject,
read-only mode and approver checks still run first (same code as scripted); every other message goes to a **Strands agent**
built from the Model page:

| Provider | Strands model |
|---|---|
| Bedrock · Messages API (default) | `AnthropicModel` driving `anthropic.AsyncAnthropicBedrockMantle` (Claude in Amazon Bedrock) |
| Bedrock · Converse API | `BedrockModel` (may need an inference-profile ID → use *Custom model ID*) |
| Anthropic API | `AnthropicModel` |
| OpenAI | `OpenAIResponsesModel` |
| Akamai AI inference | `OpenAIModel` (Chat Completions) with `base_url` from Integrations → Akamai AI inference |

**Akamai AI inference (hackathon workshop endpoint).**
- **Setup:** Integrations → *Akamai AI inference* → base URL `https://ws.akamai-workshops.com/v1` + your team's `hk_…` key → Test. Then Model → provider *Akamai AI inference* → model `hackathon-chat` → **Test model**.
- **What it is:** an OpenAI-compatible vLLM server running Qwen 3.8 27B, with tool calling.
- **Limits:**
  - 32k-token context, so replies are capped at 4,096 tokens and thread history at the last 12 messages.
  - Chat Completions only; there is no Responses API.
  - The `hackathon-image` and `hackathon-embedding` models are hidden from the list, because they can't run the agent.

**Tools (real, read-only, use your saved integrations):** `shoplite_health`, `shoplite_route_latency` (live Prometheus metrics),
`shoplite_catalog_cache_headers`, `prometheus_query`, `cloudwatch_metric`, `eks_cluster_status`, `github_recent_commits`,
`github_commit_diff`, `akamai_list_properties`, `akamai_property_caching_rules` — plus `request_approval`, which creates a pending
approval; **Approve** then runs the configured workflow (delivery steps still simulated). Each tool call streams into the run
(Slack + dashboard) via Strands hooks; conversation history is kept per thread (last ~40 messages).

### Code changes → real pull requests (`api/app/github_ops.py`)

Example: `@sidekick rename the page title in web/public/index.html to "ShopLite Plus" and raise a PR`.

1. **Find and read.** The agent finds the code with `github_search_code`, `github_list_files` and `github_read_file`, which read the repo's base branch on GitHub.
2. **Propose.** It calls `propose_code_change` with exact find/replace edits. Sidekick checks that each `find` occurs exactly once in the current file and renders a unified diff. That diff is always appended to the reply, so the approver sees exactly what will land.
3. **Approve.** On **Approve**, Sidekick's own code runs the GitHub calls; the model is not involved:
   - re-check the edits against the latest base branch;
   - create `sidekick/<slug>-<n>`;
   - commit each file;
   - open a PR whose body contains the diff and who requested and approved it.

   The run's model column shows *GitHub API (no model)*, and Slack gets a **View PR** button.
4. **Hand-off issue.** Right after the PR, Sidekick files a GitHub issue for a developer (labels `sidekick` and `needs-review`, assigned via Integrations → GitHub → *Review issue assignee*). It contains:
   - the PR and live preview links;
   - what changed and why;
   - the agent's **assumptions**;
   - a review checklist;
   - suggested **follow-ups**;
   - the diff.

   The PR says `Closes #<issue>`, so merging closes it.
5. **Reject.** Nothing is created on GitHub. If a GitHub call fails, the approval goes back to pending, so you can fix the cause and approve again.

This needs **Integrations → GitHub**:
- a token with Contents and Pull requests read/write on the repo;
- the repo as `owner/name`;
- the correct base branch (`master` for shoplite-target).

The PR flow is only in LLM mode (Brain mode → LLM agent). Merging stays with a human on GitHub.

### Go-live checklist (configuration only)
1. **AWS credentials** → Integrations → AWS (see `docs/setup-aws.md`; enable Claude model access in Bedrock).
2. **Configure → Model** → provider/model/region → **Test model** ✓.
3. **Brain mode → LLM agent.** Ask in Slack: `@sidekick is checkout healthy? check latency and cache headers`.
4. More tools light up as you connect GitHub, Akamai, Prometheus, EKS. Switch back to *Scripted demo* any time (no model cost).

Next to make real: a preview environment per PR branch, and deploy hooks after merge.

## API
OpenAPI docs: http://localhost:8090/api/docs
