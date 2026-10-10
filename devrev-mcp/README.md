# Sidekick × DevRev Computer: escalation-triage MCP connector

This is a remote **MCP server** (Streamable HTTP) that plugs Sidekick into **DevRev Computer**. It also works with any MCP client (Claude, Cursor, MCP Inspector). It solves one enterprise workflow over the **Enterprise-Bench "Maple Payments"** dataset.

> **Escalation triage.** Which open customer escalations should engineering fix first?
> - Each P0/P1 ticket is ranked by **contractual SLA breach** and **ARR at risk**.
> - Each is linked to its **product component** and the **open engineering issue** behind it.
> - The worst one is handed to **Sidekick**, which investigates and files a fix plan that waits for human approval.

**Trust model**

- **Evidence:** every result ends with an `Evidence:` line listing:
  - record IDs (`TKT-119`, `ACC-013`, `ISS-017`, `PART-010`);
  - contract citations such as `msa_standard_template.md:69` (the SLA row the threshold came from).
- **Authorized teammates:** each teammate has a personal token, and a teammate's role decides what they see and do:

| Teammate | Role | Sees | Can approve Sidekick changes |
|---|---|---|---|
| Roman Karki | Owner | all accounts, ARR | ✅ |
| Alex Morgan (USER-001) | Support lead | all accounts, ARR | ❌ |
| Marcus Webb (USER-032) | Account executive | **only accounts he owns** (12 of 31 P1s) | ❌ |
| Rahul Nair (USER-062) | Engineer | all accounts, **ARR hidden** | ❌ |

- **Audit:** every tool call is logged in the Sidekick dashboard (**Runs**, source *DevRev*) with its arguments and evidence.

## Tools

| Tool | What it does |
|---|---|
| `whoami` | Who the token belongs to, their scope, and dataset stats |
| `escalation_triage` | **The workflow.** Ranked open P0/P1 tickets with account, tier, ARR, component, related engineering issue + owner, SLA status |
| `arr_at_risk_by_component` | ARR of accounts with open P0/P1 tickets, grouped by product area (PART) |
| `sla_breaches` | Tickets past their tier's first-response SLA; thresholds are parsed from the MSA documents |
| `account_brief` | One account: open tickets, related issues, opportunities and next steps, calls |
| `search_tickets` | Keyword search over tickets in your scope |
| `escalate_to_sidekick` | Opens a Sidekick incident thread with the ticket's evidence. Sidekick investigates and proposes a fix → approval |
| `ask_sidekick` | Ask Sidekick anything; it uses the same brain as Slack |
| `sidekick_pending_approvals` / `sidekick_decide` | List and decide changes. Approvers only; others get ⛔ |

`SKILL.md` is the reusable skill (runbook) for the DevRev Computer agent: when to call which tool, and to keep the Evidence and Scope lines.

## 1. Run it

**Get the data.** The data is canary-marked: don't commit or redistribute it, and `data/` is gitignored.

```bash
cd devrev-mcp
./scripts/get-data.sh            # clones github.com/devrev/enterprise-bench, unzips artifacts/data.zip into ./data
```

**Create the teammate tokens.** Write `teammates.json` (gitignored) from the example, and put a long random string in each `token`:

```bash
cp teammates.example.json teammates.json
python -c "import secrets; print('sk_' + secrets.token_urlsafe(18))"   # one per teammate
```

**Start it with the rest of Sidekick:**

```bash
cd ../sidekick && docker compose up -d --build     # connector on http://localhost:8095/mcp
curl localhost:8095/health                          # dataset stats; /mcp without a token → 401
```

To run it standalone instead:

```bash
pip install -r requirements.txt && SIDEKICK_API_URL=http://localhost:8090 python server.py
```

## 2. Put it on a public URL

DevRev Computer runs in DevRev's cloud, so it can't reach `localhost`. A tunnel is the quickest way to give it a public URL:

```bash
cloudflared tunnel --url http://localhost:8095      # prints https://<random>.trycloudflare.com
# or: ngrok http 8095
```

Your MCP server URL is then `https://<random>.trycloudflare.com/mcp`.

**Optional hardening.** Restart with `MCP_ALLOWED_HOSTS=<random>.trycloudflare.com` in `sidekick/.env`. This turns on Host-header checks. Tokens are always required.

## 3. Connect it in DevRev Computer

These are the standard remote-MCP settings.

> **Confirm the UI.** DevRev hasn't published docs for adding a *custom* MCP server to Computer yet. Its public MCP docs cover the other direction: using DevRev's own MCP server from Claude or Cursor. Confirm the menu names with the DevRev mentors at the hackathon.

1. In DevRev, open **Computer** and find where agents get tools: **Settings → Integrations / Connectors / MCP servers**, or the agent's **Skills → Add MCP server**.
2. Add a **remote MCP server**:
   - **Name:** `Sidekick escalations`
   - **URL:** `https://<random>.trycloudflare.com/mcp`
   - **Transport:** Streamable HTTP (not SSE / stdio)
   - **Auth:** custom header `Authorization: Bearer <your teammate token>`

   If the form only takes a URL, put the token in the URL: `https://<random>.trycloudflare.com/mcp?key=<token>`.
3. Save. The 10 tools listed above should appear. If the UI has a skill or instructions field, paste the body of `SKILL.md` there.
4. Ask Computer: *"Use whoami, then run escalation triage for P1s and tell me what engineering should fix first, with evidence."*

**Another authorized teammate.** Each teammate adds the same URL with **their own** token, either in their own DevRev Computer or in any MCP client. Their results are scoped automatically.

**Fallback client: Claude Desktop/Code or Cursor.** Use this to prove it works from any MCP client, or if the DevRev UI isn't available:

```bash
claude mcp add --transport http sidekick-escalations https://<random>.trycloudflare.com/mcp --header "Authorization: Bearer <token>"
```

To debug interactively, run `npx @modelcontextprotocol/inspector` → Streamable HTTP → the URL, with the Authorization header.

## 4. Demo script (about 3 minutes)

1. **Alex (Support lead)** runs *"What should engineering fix first? P1s, with evidence."* → `escalation_triage`.
   - 31 open P1s; 29 breach SLA.
   - **#1 is TKT-119:** Vantara, $432k ARR, Subscription Lifecycle → ISS-017 "Seat quantity reset…" (owner Rahul Nair). The SLA is breached by 40 days, citing `msa_standard_template.md:69`.
2. **Alex** runs *"Where is revenue at risk?"* → `arr_at_risk_by_component`. Subscription Lifecycle and Hosted Checkout top the list.
3. **Alex** runs *"Escalate TKT-119 to Sidekick."* → `escalate_to_sidekick`.
   - Sidekick's run steps stream back: get ticket → get issue → search code → commits → reproduce.
   - The result is a fix plan tied to ISS-017, **waiting for approval**.
   - Alex runs `sidekick_decide` → ⛔ *Support lead can't approve*.
4. **Second authorized teammate: Marcus Webb (AE), with his own token.** He runs *"Brief me on Vantara before my renewal call."* → `account_brief`.
   - Open P1 linked to ISS-017; opportunity OPP-006 ($280k ACV) with next step *"Get engineering ETA on ISS-017"*; QBR call TRN-001.
   - Marcus runs triage → only **his 12** P1s, and the Scope line shows *"19 records outside your scope not shown"*.
5. **Rahul (Engineer)** runs `arr_at_risk_by_component` → the same ranking, with ARR shown as 🔒 hidden.
6. **Roman (Owner)** runs `sidekick_decide approve` → Sidekick runs the delivery workflow.
7. Open the Sidekick dashboard → **Runs**. Every call by every teammate is listed, with source *DevRev* and evidence.

## How it fits together

```
DevRev Computer ──MCP (Bearer token)──► devrev-mcp (server.py)
                                          ├─ maple.py   Enterprise-Bench data, workflow queries, scope enforcement
                                          └─ HTTP ──► Sidekick API (:8090) ── brain (scripted / Strands LLM) ── runs, approvals, audit
```

**Configuration** (environment variables):

| Variable | Default |
|---|---|
| `EB_DATA_DIR` | `./data` |
| `TEAMMATES_FILE` | `./teammates.json` |
| `SIDEKICK_API_URL` | `http://localhost:8090` |
| `PORT` | `8095` |
| `ALLOWED_HOSTS` | (unset) |
