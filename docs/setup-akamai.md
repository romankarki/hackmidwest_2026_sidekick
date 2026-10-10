# Connect Akamai to Sidekick

**Time:** ~20–40 minutes · **Cost:** free on a hackathon sandbox; pay-as-you-go on Akamai Cloud

Akamai shows up in Sidekick in two ways:

| | What it is | Who can get it | Used by Sidekick for |
|---|---|---|---|
| **A. Akamai CDN** (Property Manager) | The edge in front of ShopLite: cache rules, `Cache-Control` behaviour, activations | Needs an Akamai **contract** — usually provided by the hackathon's Akamai track (ask the mentors for a sandbox account) | Reading/changing cache rules — the heart of the "cache-header regression" demo |
| **B. Akamai Cloud** (formerly Linode) | VMs and managed Kubernetes | Self-serve sign-up | Hosting ShopLite publicly so the CDN has an origin (and showing Akamai compute) |

**Do Part A if the track gives you Control Center access. Do Part B if you need a public place to run ShopLite.** Most teams do both.

---

## Part A — Akamai CDN API credentials (Property Manager)

### A1. Log in
Go to https://control.akamai.com and sign in with the account the hackathon gave you.

### A2. Create an API client
1. Open the **☰ menu** → **Account Admin** → **Identity & access**.
2. Open the **API clients** tab → **Create API client**.
3. Choose **Advanced** (so you can limit what it can do) and give it a name: `sidekick-hackathon`.
4. Under **APIs**, add only what Sidekick needs, starting **read-only**:
   | API | Access |
   |---|---|
   | Property Manager (PAPI) | **READ-ONLY** to start (switch to READ-WRITE only when you're ready for Sidekick to propose edge changes) |
   | Reporting API | READ-ONLY (cache hit rates) |
5. Under **Groups**, pick only the group that contains your sandbox property.
6. **Create**, then **Download** the credentials. You get an `.edgerc` file like:
   ```ini
   [default]
   client_secret = abcdEfgh...=
   host = akab-xxxxxxxxxxxx-xxxxxxxxxxxx.luna.akamaiapis.net
   access_token = akab-xxxxxxxxxxxx-xxxxxxxxxxxx
   client_token = akab-xxxxxxxxxxxx-xxxxxxxxxxxx
   ```

> 🔒 These four values are full API credentials. Don't commit the `.edgerc` file or paste it into chats.

### A3. (Optional) Check the credentials from your terminal
```bash
pip install httpie-edgegrid
# put the .edgerc file at ~/.edgerc, then:
http --auth-type edgegrid -a default: :/papi/v1/groups
```
✅ You get JSON listing your contract's **groups**.

### A4. Add them to Sidekick
Dashboard → **Integrations → Akamai → Connect**:
| Field | From `.edgerc` |
|---|---|
| API host | `host` (without `https://`) |
| Client token | `client_token` |
| Client secret | `client_secret` |
| Access token | `access_token` |
| Property name | your sandbox property, e.g. `shoplite.example.com` (optional) |

**Save & test** → ✅ `Property Manager API OK · 1 group(s): …` (it makes the same signed `GET /papi/v1/groups` call).

| If you see | Fix |
|---|---|
| `HTTP 401` / `Invalid authorization` | A value was pasted wrong (check for trailing spaces), or your computer clock is off — EdgeGrid signatures are time-based |
| `HTTP 403` | The API client doesn't have Property Manager access, or not on that group (A2.4–A2.5) |
| `Missing host` | Fill all four credential fields |

### A5. Spending & safety guardrails for the CDN
CDN usage is billed on the **contract**, not per click, so the risk is *changing a live site*, not a surprise bill. Keep it safe:
- ✅ **Only activate property versions on the STAGING network.** Staging is Akamai's built-in test network and doesn't serve real users.
- ✅ Keep **Production network** activations for the demo moment, with the track mentors' OK.
- ✅ In Sidekick: keep the policy **"Production changes need a human approver"** on (Access control) — Sidekick then always asks before anything touches production.
- ✅ Start the API client **read-only**; upgrade to READ-WRITE only when you need it.
- ✅ Don't point real DNS at the property unless the mentors tell you to.

---

## Part B — Akamai Cloud (run ShopLite on a public VM)

ShopLite currently runs on your laptop (`localhost:8080`). A CDN needs a **public origin**, so put ShopLite on a small Akamai Cloud VM.

### B1. Sign up and set spending guardrails first
1. Sign up at https://www.linode.com (Akamai Cloud). New accounts usually get **trial credit** — check the current offer on the sign-up page and note when it expires.
2. Billing is **hourly, capped at the monthly price** of each resource. Important rules:
   - A **powered-off VM is still billed** — only **deleting** it stops charges.
   - Backups, extra volumes and NodeBalancers are billed separately — don't add them for the hackathon.
3. Pick the **smallest plan that works** (below) and put a reminder in your calendar to delete everything after the hackathon.
4. Check **Account → Billing** once a day during the hackathon to see your balance/credit.

### B2. Create the VM
1. **Create → Linode**.
2. Image: **Ubuntu 24.04 LTS** · Region: one near you · Plan: **Shared CPU → Linode 2 GB** (enough for ShopLite's 3 containers).
3. Label: `shoplite-origin` · set a strong root password **and** add your SSH key.
4. **Create Linode** → wait for *Running* → copy its **public IPv4**.

### B3. Add a firewall
**Firewalls → Create Firewall** → `shoplite-fw`:
- Inbound **default policy: Drop**
- Allow **TCP 22** (SSH) — ideally only from your own IP
- Allow **TCP 8080** (ShopLite)
- Assign it to `shoplite-origin`.

### B4. Run ShopLite on it
```bash
ssh root@<public-ip>

# install Docker
curl -fsSL https://get.docker.com | sh

# get ShopLite (your GitHub repo from setup-github.md)
git clone https://github.com/<you>/shoplite.git && cd shoplite
docker compose up -d --build --wait
```
✅ Open `http://<public-ip>:8080` — ShopLite loads.

> If the repo is private, clone over HTTPS with a token, or `scp` the folder up instead.

### B5. Tell Sidekick where ShopLite lives
Dashboard → **Integrations → ShopLite (target app) → Configure** → **App URL:** `http://<public-ip>:8080` → **Save & test** → ✅ `ShopLite … on branch main`.

### B6. Put the CDN in front (needs Part A)
In Control Center → **Property Manager**, in your sandbox property set the **Origin server hostname** to the VM's public IP / hostname (port 8080), save a new version and **activate on STAGING**. Then test through the edge using Akamai's staging hostname (the mentors will show you how to point your machine at staging). This is the path the cache-header demo bug travels: *edge → origin `/api/products`*.

---

## End-of-hackathon checklist
- [ ] Akamai Cloud: **delete** `shoplite-origin` (and any volumes/NodeBalancers) — powered-off still bills
- [ ] Control Center → Identity & access → API clients → **deactivate** `sidekick-hackathon`
- [ ] Revert any property activations the mentors asked you to undo
- [ ] Dashboard → Integrations → Akamai → **Disconnect**
