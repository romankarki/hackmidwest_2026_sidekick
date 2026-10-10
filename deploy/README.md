# Hosting on Akamai Cloud

Sidekick and ShopLite run on one Akamai Cloud (Linode) VM with Docker. Caddy in front provides HTTPS. Pushing to GitHub deploys automatically.

```
Internet ─► Cloud Firewall (22, 80, 443 only) ─► Caddy (HTTPS, Let's Encrypt) ─┬─► shop.<host>      ShopLite         :8080
                                                                                 ├─► sidekick.<host>  dashboard + API  :8090  (basic auth)
                                                                                 ├─► mcp.<host>       DevRev connector :8095  (bearer tokens)
                                                                                 └─► pr-142/143.<host> demo previews  :8142/:8143
GitHub Actions ─SSH (ci key, forced command)──► /opt/apps/gate.sh ci:        deploy shoplite | deploy sidekick | kpreview up/down
Sidekick API   ─SSH (dashboard key, from Docker)► /opt/apps/gate.sh dashboard: kpreview ls | down <pr> | down-all   (kill switch)
gate.sh ─kubectl─► Akamai LKE `sidekick-previews` ─► Traefik on a NodeBalancer ─► https://pr-<n>.<lke-host>  (namespace pr-<n>)
```

PR previews run on **Akamai LKE (Kubernetes)**. Each PR gets namespace `pr-<n>` with web, api and a throwaway db. GitHub Actions builds the images, tags them with the commit SHA, and pushes them to `ghcr.io/romankarki/shoplite-target/{db,api,web}`. The gate applies the PR branch's own `k8s/base` manifests plus an Ingress (Traefik, Let's Encrypt via cert-manager). The cluster credentials live only on the VM (`/opt/apps/kube/config`); neither GitHub nor Sidekick holds them.

`<host>` is `<ip-with-dashes>.sslip.io`. sslip.io is free wildcard DNS that resolves to the IP, so no domain is needed.

| | |
|---|---|
| **VM** | `sidekick-prod`: g6-standard-4 (4 vCPU / 8 GB, about $48/mo), Ubuntu 24.04, `us-sea` |
| **LKE** | `sidekick-previews`: 1× g6-standard-2 node (about $24/mo) + 1 NodeBalancer (about $10/mo) for all previews; Traefik + cert-manager |
| **Firewall** | `sidekick-prod-fw`: inbound TCP 22/80/443 and ping; everything else dropped |
| **Code** | `/opt/apps/shoplite` (shoplite-target) and `/opt/apps/sidekick` (this repo), owned by `deploy` |
| **Secrets** | the `.env` files and `teammates.json` are uploaded by `provision.sh`; never in git |
| **Local only** | `deploy/.keys/` holds the CI deploy key and the dashboard password |

## First deploy

```bash
linode-cli configure                                  # once: log in to Akamai Cloud
bash deploy/provision.sh                              # firewall + VM + everything on it (about 15 min)
bash deploy/lke-setup.sh <ip>                         # LKE cluster + ingress + certs for PR previews (about 10 min)
GITHUB_TOKEN=<token> python deploy/set-github-secrets.py <ip>   # enables deploy-on-push + PR previews
```

`provision.sh` is safe to re-run: it reuses the firewall and VM if they exist. It carries your local Sidekick database over (integrations, model settings, history), so the hosted Sidekick is configured exactly like your laptop.

## Day to day

- **ShopLite:**
  - Push or merge to `master` → the *Deploy ShopLite* workflow rebuilds https://shop.\<host\>.
  - Open a PR → the *PR preview* workflow deploys it to LKE at https://pr-\<n\>.\<lke-host\> and comments the link. Each push updates it; closing the PR deletes the namespace.
- **Sidekick:** push to `master` → the *Deploy Sidekick* workflow rebuilds the API, dashboard, Slack bot and DevRev connector.
- **Sidekick's own PRs:** "@sidekick change … and raise a PR" → Approve → PR → LKE preview link (about 3–4 min) → merge → live on shop.\<host\>.

The keys can't open a shell: each can only run its role's commands in `gate.sh` (ci: deploys and previews; dashboard: list and delete previews). The server copy of `gate.sh` is root-owned and installed by `bootstrap.sh`, so a push can't widen it. Re-run `provision.sh` to update the gate.

## Kill switch and flood guard

- **Flood guard:** at most **5** LKE previews run at once (`MAX_K8S_PREVIEWS`). Extra PRs get a PR comment saying the limit was hit, instead of a preview.
- **Kill switch:** Sidekick dashboard → **Previews** lists every running preview (PR, branch, commit, age, link). You can **Delete** one, or press **Delete all previews**. Each deletion is recorded in Runs.

The dashboard's key can only list and delete previews, and only from Docker's private network on the VM.

To stop everything on the cluster from your laptop instead:

```bash
KUBECONFIG=deploy/.keys/lke-kubeconfig.yaml kubectl delete ns -l sidekick.preview=true
```

## Operate

```bash
ssh root@<ip>                                              # your own key (~/.ssh/id_ed25519)
cd /opt/apps/sidekick/sidekick && docker compose ps        # or logs -f api slack-bot
cd /opt/apps/shoplite && docker compose ls                 # ShopLite + previews
cat /etc/caddy/Caddyfile; ls /etc/caddy/sites/             # routes
```

## Costs and cleanup

The VM bills hourly, and **a powered-off VM still bills**. The VM plus LKE cost about $82/mo together; the $300 HackMidwest credit expires 2027-01-01, so it covers them until then. To stop all charges:

```bash
linode-cli linodes list                        # find the id of sidekick-prod
linode-cli linodes delete <id>
linode-cli firewalls list && linode-cli firewalls delete <firewall-id>
linode-cli lke clusters-list && linode-cli lke cluster-delete <cluster-id>
linode-cli nodebalancers list               # the NodeBalancer goes with the cluster's Traefik service; delete it if it lingers
```

Then remove the `DEPLOY_*` secrets from both GitHub repos. Workflows skip deploys with a warning when they're missing.
