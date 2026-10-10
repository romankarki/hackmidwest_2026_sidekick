# Hosting on Akamai Cloud

Sidekick and ShopLite run on one Akamai Cloud (Linode) VM with Docker. Caddy in front provides HTTPS. Pushing to GitHub deploys automatically.

```
Internet ─► Cloud Firewall (22, 80, 443 only) ─► Caddy (HTTPS, Let's Encrypt) ─┬─► shop.<host>      ShopLite         :8080
                                                                                 ├─► sidekick.<host>  dashboard + API  :8090  (basic auth)
                                                                                 ├─► mcp.<host>       DevRev connector :8095  (bearer tokens)
                                                                                 ├─► pr-142/143.<host> demo previews  :8142/:8143
                                                                                 └─► pr-<n>.<host>    one stack per open PR (9000+n)
GitHub Actions ─SSH (deploy key, forced command)─► /opt/apps/gate.sh: deploy shoplite | deploy sidekick | preview up/down
```

`<host>` is `<ip-with-dashes>.sslip.io`. sslip.io is free wildcard DNS that resolves to the IP, so no domain is needed.

| | |
|---|---|
| **VM** | `sidekick-prod`: g6-standard-4 (4 vCPU / 8 GB, about $48/mo), Ubuntu 24.04, `us-sea` |
| **Firewall** | `sidekick-prod-fw`: inbound TCP 22/80/443 and ping; everything else dropped |
| **Code** | `/opt/apps/shoplite` (shoplite-target) and `/opt/apps/sidekick` (this repo), owned by `deploy` |
| **Secrets** | the `.env` files and `teammates.json` are uploaded by `provision.sh`; never in git |
| **Local only** | `deploy/.keys/` holds the CI deploy key and the dashboard password |

## First deploy

```bash
linode-cli configure                                  # once: log in to Akamai Cloud
bash deploy/provision.sh                              # firewall + VM + everything on it (about 15 min)
GITHUB_TOKEN=<token> python deploy/set-github-secrets.py <ip>   # enables deploy-on-push + PR previews
```

`provision.sh` is safe to re-run: it reuses the firewall and VM if they exist. It carries your local Sidekick database over (integrations, model settings, history), so the hosted Sidekick is configured exactly like your laptop.

## Day to day

- **ShopLite:**
  - Push or merge to `master` → the *Deploy ShopLite* workflow rebuilds https://shop.\<host\>.
  - Open a PR → the *PR preview* workflow builds https://pr-\<n\>.\<host\> and comments the link. Each push updates it; closing the PR removes it.
- **Sidekick:** push to `master` → the *Deploy Sidekick* workflow rebuilds the API, dashboard, Slack bot and DevRev connector.
- **Sidekick's own PRs:** "@sidekick change … and raise a PR" → Approve → PR → preview link (about 2–3 min) → merge → live on shop.\<host\>.

The deploy key can't open a shell. It can only run the four commands in `gate.sh`. The server copy of `gate.sh` is root-owned and installed by `bootstrap.sh`, so a push can't widen it. Re-run `provision.sh` to update the gate.

## Operate

```bash
ssh root@<ip>                                              # your own key (~/.ssh/id_ed25519)
cd /opt/apps/sidekick/sidekick && docker compose ps        # or logs -f api slack-bot
cd /opt/apps/shoplite && docker compose ls                 # ShopLite + previews
cat /etc/caddy/Caddyfile; ls /etc/caddy/sites/             # routes
```

## Costs and cleanup

The VM bills hourly, and **a powered-off VM still bills**. The $300 HackMidwest credit covers about 6 months of this VM; it expires 2027-01-01. To stop all charges:

```bash
linode-cli linodes list                        # find the id of sidekick-prod
linode-cli linodes delete <id>
linode-cli firewalls list && linode-cli firewalls delete <firewall-id>
```

Then remove the `DEPLOY_*` secrets from both GitHub repos. Workflows skip deploys with a warning when they're missing.
