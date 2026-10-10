#!/usr/bin/env bash
# Create (or reuse) the Akamai Cloud VM + Cloud Firewall and install Sidekick + ShopLite on it. Run from the repo root:
#
#   bash deploy/provision.sh            (needs: linode-cli configured, ssh key ~/.ssh/id_ed25519, local Sidekick running)
#
# Billable: one g6-standard-4 Linode (8 GB, ~$48/mo). Delete with: linode-cli linodes delete <id>  (see deploy/README.md)
set -euo pipefail
cd "$(dirname "$0")/.."
LABEL="${LABEL:-sidekick-prod}"; REGION="${REGION:-us-sea}"; TYPE="${TYPE:-g6-standard-4}"; IMAGE="${IMAGE:-linode/ubuntu24.04}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
KEYS=deploy/.keys; mkdir -p "$KEYS"           # gitignored: CI deploy key + dashboard password
lc() { linode-cli --no-defaults --suppress-warnings "$@"; }

# ---- firewall: only SSH + HTTP(S) reach the VM; app ports (8080, 8090, 8095, previews) stay private ----
FW_ID="$(lc firewalls list --text --no-headers --format id,label | awk -v l="$LABEL-fw" '$2==l{print $1}')"
if [ -z "$FW_ID" ]; then
  FW_ID="$(lc firewalls create --label "$LABEL-fw" --rules.inbound_policy DROP --rules.outbound_policy ACCEPT \
    --rules.inbound '[{"label":"ssh-http-https","action":"ACCEPT","protocol":"TCP","ports":"22,80,443","addresses":{"ipv4":["0.0.0.0/0"],"ipv6":["::/0"]}},{"label":"ping","action":"ACCEPT","protocol":"ICMP","addresses":{"ipv4":["0.0.0.0/0"]}}]' \
    --text --no-headers --format id)"
  echo "✓ firewall $LABEL-fw ($FW_ID)"
fi

# ---- VM ----
ID="$(lc linodes list --label "$LABEL" --text --no-headers --format id)"
if [ -z "$ID" ]; then
  ID="$(lc linodes create --label "$LABEL" --region "$REGION" --type "$TYPE" --image "$IMAGE" \
    --root_pass "$(openssl rand -base64 30)" --authorized_keys "$(cat "$SSH_KEY.pub")" --firewall_id "$FW_ID" \
    --metadata.user_data "$(base64 -w0 deploy/cloud-init.yaml)" --tags sidekick --text --no-headers --format id)"
  echo "✓ created Linode $LABEL ($ID) in $REGION"
fi
until [ "$(lc linodes view "$ID" --text --no-headers --format status)" = running ]; do sleep 5; done
IP="$(lc linodes view "$ID" --text --no-headers --format ipv4 | awk '{print $1}')"
echo "✓ running at $IP"

SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 root@$IP"
until $SSH true 2>/dev/null; do sleep 5; done
echo "… waiting for first-boot setup (Docker, Caddy) to finish"
$SSH "cloud-init status --wait >/dev/null; docker --version && caddy version | head -1"

# ---- secrets + data to upload (never committed) ----
[ -f "$KEYS/ci_deploy_key" ] || ssh-keygen -q -t ed25519 -N "" -C "github-actions-deploy" -f "$KEYS/ci_deploy_key"
[ -f "$KEYS/dashboard_password" ] || openssl rand -base64 18 | tr -d '/+=' > "$KEYS/dashboard_password"
UP="$(mktemp -d)"
cp sidekick/.env "$UP/sidekick.env"; cp slack-bot/.env "$UP/slack-bot.env"; cp devrev-mcp/teammates.json "$UP/teammates.json"
cp "$KEYS/ci_deploy_key.pub" "$UP/"
for f in dashboard_gate_key dashboard_gate_key.pub lke-kubeconfig.yaml k8s-host; do if [ -f "$KEYS/$f" ]; then cp "$KEYS/$f" "$UP/"; fi; done  # LKE previews (deploy/lke-setup.sh)
if (cd sidekick && docker compose ps --status running db | grep -q db); then  # carry over integrations, model, history
  (cd sidekick && docker compose exec -T db pg_dump --clean --if-exists -U sidekick sidekick) > "$UP/sidekick.sql"
  echo "✓ dumped local Sidekick database ($(wc -c < "$UP/sidekick.sql") bytes)"
fi
$SSH "rm -rf /root/upload && mkdir -p /root/upload"
scp -q -i "$SSH_KEY" "$UP"/* deploy/bootstrap.sh "root@$IP:/root/upload/"
rm -rf "$UP"

$SSH "bash /root/upload/bootstrap.sh $IP '$(cat "$KEYS/dashboard_password")'"
ssh-keyscan -t ed25519 "$IP" 2>/dev/null > "$KEYS/known_hosts"
echo
echo "Dashboard login: admin / $(cat "$KEYS/dashboard_password")   (saved in deploy/.keys/dashboard_password)"
echo "Next: python deploy/set-github-secrets.py $IP   → enables deploy-on-push and PR previews"
