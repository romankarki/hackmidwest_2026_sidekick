#!/usr/bin/env bash
# PR previews on Akamai LKE (Kubernetes). Run from the repo root after deploy/provision.sh:
#
#   bash deploy/lke-setup.sh <vm-ip>        (needs: linode-cli configured, kubectl, helm)
#
# Creates (or reuses) a 1-node LKE cluster, installs Traefik (→ one NodeBalancer for all previews) and cert-manager
# (Let's Encrypt), hands the kubeconfig to the VM's deploy gate, and wires Sidekick's kill switch.
# Billable: 1× g6-standard-2 node (~$24/mo) + 1 NodeBalancer (~$10/mo). Delete: linode-cli lke cluster-delete <id>.
set -euo pipefail
cd "$(dirname "$0")/.."
VM="$1"; LABEL="${LABEL:-sidekick-previews}"; REGION="${REGION:-us-sea}"; NODE="${NODE:-g6-standard-2}"
KEYS=deploy/.keys; export KUBECONFIG="$PWD/$KEYS/lke-kubeconfig.yaml"
lc() { linode-cli --no-defaults --suppress-warnings "$@"; }
SSH="ssh -i $HOME/.ssh/id_ed25519 root@$VM"

# ---- cluster ----
ID="$(lc lke clusters-list --text --no-headers --format id,label | awk -v l="$LABEL" '$2==l{print $1}')"
if [ -z "$ID" ]; then
  V="$(lc lke versions-list --text --no-headers --format id | sort -V | tail -1)"
  ID="$(lc lke cluster-create --label "$LABEL" --region "$REGION" --k8s_version "$V" --node_pools.type "$NODE" --node_pools.count 1 \
        --tags sidekick --text --no-headers --format id)"
  echo "✓ created LKE cluster $LABEL ($ID, Kubernetes $V)"
fi
until K="$(lc lke kubeconfig-view "$ID" --text --no-headers 2>/dev/null | head -1)" && [ "${#K}" -gt 100 ]; do sleep 10; done
echo "$K" | base64 -d > "$KEYS/lke-kubeconfig.yaml" && chmod 600 "$KEYS/lke-kubeconfig.yaml"
until kubectl get nodes --no-headers 2>/dev/null | grep -q " Ready"; do sleep 10; done
echo "✓ node ready"

# ---- ingress (Traefik; ingress-nginx was retired in 2026) + certificates ----
helm repo add traefik https://traefik.github.io/charts >/dev/null; helm repo add jetstack https://charts.jetstack.io >/dev/null; helm repo update >/dev/null
helm upgrade --install traefik traefik/traefik -n traefik --create-namespace --set service.type=LoadBalancer \
  --set ingressClass.isDefaultClass=true --set ingressClass.name=traefik --wait --timeout 10m >/dev/null
helm upgrade --install cert-manager jetstack/cert-manager -n cert-manager --create-namespace --set crds.enabled=true --wait --timeout 10m >/dev/null
kubectl apply -f - >/dev/null <<EOF
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata: { name: letsencrypt }
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    privateKeySecretRef: { name: letsencrypt-account-anon }
    solvers: [{ http01: { ingress: { ingressClassName: traefik } } }]
EOF
until NB="$(kubectl -n traefik get svc traefik -o jsonpath='{.status.loadBalancer.ingress[0].ip}')" && [ -n "$NB" ]; do sleep 10; done
echo "${NB//./-}.sslip.io" > "$KEYS/k8s-host"
echo "✓ Traefik + cert-manager · NodeBalancer $NB → previews at https://pr-<n>.$(cat "$KEYS/k8s-host")"

# ---- hand the cluster to the VM's gate + wire Sidekick's kill switch ----
[ -f "$KEYS/dashboard_gate_key" ] || ssh-keygen -q -t ed25519 -N "" -C "sidekick-dashboard-gate" -f "$KEYS/dashboard_gate_key"
scp -q -i "$HOME/.ssh/id_ed25519" "$KEYS/lke-kubeconfig.yaml" "$KEYS/k8s-host" "$KEYS/dashboard_gate_key" "$KEYS/dashboard_gate_key.pub" "root@$VM:/root/"
$SSH bash -s <<'EOF'
set -euo pipefail
APPS=/opt/apps; ENVF=$APPS/sidekick/sidekick/.env; AK=/home/deploy/.ssh/authorized_keys
if ! command -v kubectl >/dev/null; then
  curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.36/deb/Release.key | gpg --dearmor --batch --yes -o /etc/apt/keyrings/kubernetes.gpg
  echo "deb [signed-by=/etc/apt/keyrings/kubernetes.gpg] https://pkgs.k8s.io/core:/stable:/v1.36/deb/ /" > /etc/apt/sources.list.d/kubernetes.list
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq kubectl >/dev/null
fi
install -d -o deploy -g deploy -m 700 $APPS/kube $APPS/k8s-previews
install -o deploy -g deploy -m 600 /root/lke-kubeconfig.yaml $APPS/kube/config
cp /root/k8s-host $APPS/k8s-host
install -o root -g root -m 755 $APPS/sidekick/deploy/gate.sh $APPS/gate.sh
grep -v 'gate.sh dashboard' $AK > $AK.tmp || true
echo "command=\"$APPS/gate.sh dashboard\",from=\"172.16.0.0/12,192.168.0.0/16\",restrict $(cat /root/dashboard_gate_key.pub)" >> $AK.tmp
install -o deploy -g deploy -m 600 $AK.tmp $AK && rm -f $AK.tmp
sed -i '/^GATE_SSH_KEY=/d;/^GATE_HOST_KEY=/d' $ENVF
echo "GATE_SSH_KEY=$(base64 -w0 /root/dashboard_gate_key)" >> $ENVF
echo "GATE_HOST_KEY=$(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)" >> $ENVF
H="$(cat $APPS/k8s-host)"
cd $APPS/sidekick/sidekick && sudo -u deploy docker compose up -d --wait api >/dev/null
sudo -u deploy docker compose exec -T db psql -q -U sidekick -d sidekick -c \
  "UPDATE integrations SET config = config || jsonb_build_object('pr_preview_url_template', 'https://{label}.$H') WHERE key = 'shoplite'"
rm -f /root/lke-kubeconfig.yaml /root/k8s-host /root/dashboard_gate_key /root/dashboard_gate_key.pub
echo "✓ VM gate has the cluster; Sidekick → Previews kill switch is live"
EOF
echo "Next: set the shoplite-target secret PREVIEW_DOMAIN=$(cat "$KEYS/k8s-host")  (python deploy/set-github-secrets.py <vm-ip> does it)"
