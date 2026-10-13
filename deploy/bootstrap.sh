#!/usr/bin/env bash
# One-time server setup, run as root on the VM after cloud-init (deploy/provision.sh does this for you):
#
#   bash bootstrap.sh <public-ip> <dashboard-password>
#
# Expects (uploaded by provision.sh to /root/upload/): sidekick.env, slack-bot.env, teammates.json, ci_deploy_key.pub,
# and optionally sidekick.sql (pg_dump of your local Sidekick DB: integrations, model settings, history).
set -euo pipefail
IP="$1"; DASH_PASS="$2"
HOST="${IP//./-}.sslip.io"            # free wildcard DNS: anything.<ip-with-dashes>.sslip.io → <ip>
APPS=/opt/apps; UP=/root/upload
as_deploy() { sudo -u deploy -H bash -c "$*"; }

cloud-init status --wait >/dev/null || true
echo "$HOST" > "$APPS/host"

# ---- code ----
[ -d "$APPS/shoplite/.git" ] || as_deploy "git clone -q https://github.com/romankarki/shoplite-target.git $APPS/shoplite"
[ -d "$APPS/sidekick/.git" ] || as_deploy "git clone -q https://github.com/romankarki/hackmidwest_2026_sidekick.git $APPS/sidekick"
mkdir -p "$APPS/previews" && chown deploy:deploy "$APPS/previews"

# ---- secrets (never in git) ----
install -o deploy -g deploy -m 600 "$UP/sidekick.env" "$APPS/sidekick/sidekick/.env"
install -o deploy -g deploy -m 600 "$UP/slack-bot.env" "$APPS/sidekick/slack-bot/.env"
install -o deploy -g deploy -m 600 "$UP/teammates.json" "$APPS/sidekick/devrev-mcp/teammates.json"
cat >> "$APPS/sidekick/sidekick/.env" <<EOF
SIDEKICK_PUBLIC_URL=https://sidekick.$HOST
SIDEKICK_HOSTING=Akamai Cloud VM sidekick-prod (Seattle us-sea) · PR previews on Akamai LKE
MCP_ALLOWED_HOSTS=mcp.$HOST
EOF

# ---- Enterprise-Bench data for the DevRev connector (fetched from its source, not copied) ----
[ -f "$APPS/sidekick/devrev-mcp/data/crm_json_data/tickets.json" ] || as_deploy "bash $APPS/sidekick/devrev-mcp/scripts/get-data.sh"

# ---- deploy gate: the CI key may only run gate.sh (root-owned copy, so a git push can't change it) ----
install -o root -g root -m 755 "$APPS/sidekick/deploy/gate.sh" "$APPS/gate.sh"
install -d -o deploy -g deploy -m 700 /home/deploy/.ssh
echo "command=\"$APPS/gate.sh\",restrict $(cat "$UP/ci_deploy_key.pub")" > /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys && chmod 600 /home/deploy/.ssh/authorized_keys
echo "deploy ALL=(root) NOPASSWD: /usr/bin/systemctl reload caddy" > /etc/sudoers.d/deploy-caddy && chmod 440 /etc/sudoers.d/deploy-caddy
chown deploy:deploy /etc/caddy/sites

# ---- LKE previews (optional: present once deploy/lke-setup.sh has run) ----
if [ -f "$UP/dashboard_gate_key.pub" ]; then  # Sidekick's kill switch: dashboard role only, only from Docker networks
  echo "command=\"$APPS/gate.sh dashboard\",from=\"172.16.0.0/12,192.168.0.0/16\",restrict $(cat "$UP/dashboard_gate_key.pub")" >> /home/deploy/.ssh/authorized_keys
  cat >> "$APPS/sidekick/sidekick/.env" <<EOF
GATE_SSH_KEY=$(base64 -w0 "$UP/dashboard_gate_key")
GATE_HOST_KEY=$(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)
EOF
fi
if [ -f "$UP/lke-kubeconfig.yaml" ]; then
  if ! command -v kubectl >/dev/null; then
    curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.36/deb/Release.key | gpg --dearmor --batch --yes -o /etc/apt/keyrings/kubernetes.gpg
    echo "deb [signed-by=/etc/apt/keyrings/kubernetes.gpg] https://pkgs.k8s.io/core:/stable:/v1.36/deb/ /" > /etc/apt/sources.list.d/kubernetes.list
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq kubectl >/dev/null
  fi
  install -d -o deploy -g deploy -m 700 "$APPS/kube" "$APPS/k8s-previews"
  install -o deploy -g deploy -m 600 "$UP/lke-kubeconfig.yaml" "$APPS/kube/config"
  cp "$UP/k8s-host" "$APPS/k8s-host"
fi

# ---- HTTPS routes (Caddy gets Let's Encrypt certificates automatically) ----
HASH="$(caddy hash-password --plaintext "$DASH_PASS")"
cat > /etc/caddy/Caddyfile <<EOF
shop.$HOST {
	reverse_proxy 127.0.0.1:8080
}
sidekick.$HOST {
	basicauth {
		admin $HASH
	}
	reverse_proxy 127.0.0.1:8090
}
mcp.$HOST {
	reverse_proxy 127.0.0.1:8095
}
pr-142.$HOST {
	reverse_proxy 127.0.0.1:8142
}
pr-143.$HOST {
	reverse_proxy 127.0.0.1:8143
}
import sites/*.caddy
EOF
systemctl reload caddy

# ---- ShopLite + the two demo previews ----
as_deploy "cd $APPS/shoplite && APP_VERSION=\$(git rev-parse --short HEAD) GIT_BRANCH=master docker compose up -d --build --wait"
as_deploy "cd $APPS/shoplite && bash scripts/demo-previews.sh up"

# ---- Sidekick: restore the DB first (if a dump was uploaded), then the whole stack ----
cd "$APPS/sidekick/sidekick"
if [ -f "$UP/sidekick.sql" ] && [ ! -f "$APPS/.sidekick-db-restored" ]; then
  as_deploy "cd $APPS/sidekick/sidekick && docker compose up -d --wait db"
  as_deploy "cd $APPS/sidekick/sidekick && docker compose exec -T db psql -q -U sidekick -d sidekick" < "$UP/sidekick.sql" >/dev/null
  touch "$APPS/.sidekick-db-restored"
fi
as_deploy "cd $APPS/sidekick/sidekick && docker compose up -d --build --wait"

# point Sidekick's links at the public URLs (preview links in replies)
as_deploy "cd $APPS/sidekick/sidekick && docker compose exec -T db psql -q -U sidekick -d sidekick" <<SQL
UPDATE integrations SET config = config || '{"preview_url_template": "https://{label}.$HOST"}'::jsonb WHERE key = 'shoplite';
SQL

rm -rf "$UP"
echo
echo "✅ ShopLite        https://shop.$HOST"
echo "✅ Sidekick        https://sidekick.$HOST   (user: admin)"
echo "✅ DevRev MCP      https://mcp.$HOST/mcp"
echo "✅ Demo previews   https://pr-142.$HOST  https://pr-143.$HOST"
