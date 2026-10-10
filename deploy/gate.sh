#!/usr/bin/env bash
# Forced command for the restricted SSH keys (authorized_keys: command="/opt/apps/gate.sh <role>",restrict ...).
# Each key can run ONLY the actions of its role — no shell:
#
#   role "ci" (GitHub Actions)
#     deploy shoplite                      pull master of shoplite-target and rebuild ShopLite (https://shop.<host>)
#     deploy sidekick                      pull master of hackmidwest_2026_sidekick and rebuild Sidekick
#     preview up <pr> <branch>             build that branch on this VM (Docker) → https://pr-<pr>.<host>
#     preview down <pr>                    remove that VM preview
#     kpreview up <pr> <sha> <branch>      deploy the PR's images (ghcr.io, built by Actions) to LKE namespace pr-<pr>
#     kpreview down <pr>                   delete that LKE namespace
#
#   role "dashboard" (Sidekick's kill switch)
#     kpreview ls                          JSON list of LKE previews
#     kpreview down <pr> | kpreview down-all
set -euo pipefail
ROLE="${1:-ci}"
APPS=/opt/apps
HOST="$(cat "$APPS/host")"                  # VM routes, e.g. 172-232-189-143.sslip.io
K8S_HOST="$(cat "$APPS/k8s-host" 2>/dev/null || true)"   # LKE NodeBalancer, e.g. 172-238-58-56.sslip.io
MAX_K8S_PREVIEWS="${MAX_K8S_PREVIEWS:-5}"   # flood guard: more simultaneous LKE previews are refused
REGISTRY=ghcr.io/romankarki/shoplite-target
export KUBECONFIG="$APPS/kube/config"
read -r -a args <<< "${SSH_ORIGINAL_COMMAND:-}"
log() { echo "[gate] $*" >&2; }
deny() { echo "Not allowed for role '$ROLE': '${SSH_ORIGINAL_COMMAND:-}'"; exit 2; }

pull() {  # $1 = repo dir: hard-reset to the remote default branch (the server never has local edits)
  git -C "$1" fetch -q --prune origin
  git -C "$1" reset -q --hard "origin/$(git -C "$1" rev-parse --abbrev-ref origin/HEAD | sed 's#origin/##')"
  log "$(basename "$1") at $(git -C "$1" log -1 --format='%h %s')"
}

route() {  # $1 = site name, $2 = port → Caddy site file, reload
  printf '%s.%s {\n\treverse_proxy 127.0.0.1:%s\n}\n' "$1" "$HOST" "$2" > "/etc/caddy/sites/$1.caddy"
  sudo /usr/bin/systemctl reload caddy
}

valid_pr() { [[ "${1:-}" =~ ^[0-9]{1,5}$ ]] || { echo "invalid PR number"; exit 2; }; }

# ---------- Kubernetes (LKE) previews ----------
k_ls() {
  kubectl get ns -l sidekick.preview=true -o json | jq -c '[.items[] | {pr: (.metadata.labels["sidekick.pr"] | tonumber),
    namespace: .metadata.name, branch: .metadata.annotations["sidekick/branch"], sha: .metadata.annotations["sidekick/sha"],
    url: .metadata.annotations["sidekick/url"], created: .metadata.creationTimestamp, phase: .status.phase}] | sort_by(.pr)'
}

k_down() {  # $1 = pr
  kubectl delete ns "pr-$1" --ignore-not-found --wait=false >/dev/null
  [ -d "$APPS/k8s-previews/pr-$1" ] && git -C "$APPS/shoplite" worktree remove --force "$APPS/k8s-previews/pr-$1" || true
  log "🧹 LKE preview pr-$1 deleted"
}

k_up() {  # $1 = pr, $2 = full commit sha (images are tagged with it), $3 = branch
  local pr="$1" sha="$2" branch="$3" ns="pr-$1" dir="$APPS/k8s-previews/pr-$1" url="https://pr-$1.$K8S_HOST"
  [[ "$sha" =~ ^[0-9a-f]{40}$ && "$branch" =~ ^[A-Za-z0-9._/-]{1,120}$ ]] || { echo "usage: kpreview up <pr> <40-char sha> <branch>"; exit 2; }
  [ -n "$K8S_HOST" ] || { echo "LKE previews are not configured on this server"; exit 2; }
  local running; running="$(kubectl get ns -l sidekick.preview=true -o name | grep -vc "^namespace/$ns$" || true)"
  if [ "$running" -ge "$MAX_K8S_PREVIEWS" ]; then
    echo "⛔ Preview limit reached: $running of $MAX_K8S_PREVIEWS LKE previews are running. Delete some in Sidekick → Previews (kill switch), or close old PRs."
    exit 3
  fi
  # manifests come from the PR branch itself, at exactly the commit the images were built from
  git -C "$APPS/shoplite" fetch -q origin "+refs/heads/$branch:refs/remotes/origin/$branch"
  if [ -d "$dir" ]; then git -C "$dir" checkout -q --detach "$sha"; else git -C "$APPS/shoplite" worktree add -q --detach "$dir" "$sha"; fi
  mkdir -p "$dir/.k8s-preview"
  cat > "$dir/.k8s-preview/ingress.yaml" <<EOF
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: web
  annotations: { cert-manager.io/cluster-issuer: letsencrypt }
spec:
  ingressClassName: traefik
  tls: [{ hosts: [pr-$pr.$K8S_HOST], secretName: web-tls }]
  rules:
    - host: pr-$pr.$K8S_HOST
      http: { paths: [{ path: /, pathType: Prefix, backend: { service: { name: web, port: { number: 80 } } } }] }
EOF
  cat > "$dir/.k8s-preview/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: $ns
resources: [../k8s/base, ingress.yaml]
configMapGenerator:
  - name: shoplite-config
    behavior: merge
    literals: [APP_VERSION=${sha:0:7}, GIT_BRANCH=$branch, PREVIEW_LABEL=$ns]
replicas: [{ name: api, count: 1 }]   # previews don't need HA; keeps the node free for more PRs
images:
  - { name: shoplite-db,  newName: $REGISTRY/db,  newTag: "$sha" }
  - { name: shoplite-api, newName: $REGISTRY/api, newTag: "$sha" }
  - { name: shoplite-web, newName: $REGISTRY/web, newTag: "$sha" }
EOF
  kubectl create ns "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl label ns "$ns" --overwrite sidekick.preview=true "sidekick.pr=$pr" >/dev/null
  kubectl annotate ns "$ns" --overwrite "sidekick/branch=$branch" "sidekick/sha=$sha" "sidekick/url=$url" >/dev/null
  if [ -f "$APPS/kube/ghcr-pull" ]; then  # private images: "user:token" with read:packages
    IFS=: read -r u t < "$APPS/kube/ghcr-pull"
    kubectl -n "$ns" create secret docker-registry ghcr --docker-server=ghcr.io --docker-username="$u" --docker-password="$t" \
      --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    kubectl -n "$ns" patch serviceaccount default -p '{"imagePullSecrets":[{"name":"ghcr"}]}' >/dev/null
  fi
  kubectl apply -k "$dir/.k8s-preview" >/dev/null
  kubectl -n "$ns" rollout restart deploy/db deploy/api deploy/web >/dev/null   # fresh DB + new images on every push
  kubectl -n "$ns" rollout status deploy/db deploy/api deploy/web --timeout=300s >&2
  log "✅ $url"
}

# ---------- dispatch ----------
cmd="${args[0]:-} ${args[1]:-}"
if [ "$ROLE" = dashboard ]; then
  case "$cmd" in
    "kpreview ls") k_ls ;;
    "kpreview down") valid_pr "${args[2]:-}"; k_down "${args[2]}" ;;
    "kpreview down-all")
      for pr in $(kubectl get ns -l sidekick.preview=true -o jsonpath='{.items[*].metadata.labels.sidekick\.pr}'); do k_down "$pr"; done
      log "🧹 all LKE previews deleted" ;;
    *) deny ;;
  esac
  exit 0
fi

case "$cmd" in
  "deploy shoplite")
    pull "$APPS/shoplite"
    (cd "$APPS/shoplite" && APP_VERSION="$(git rev-parse --short HEAD)" GIT_BRANCH=master docker compose up -d --build --wait --remove-orphans)
    log "✅ https://shop.$HOST"
    ;;
  "deploy sidekick")
    pull "$APPS/sidekick"
    (cd "$APPS/sidekick/sidekick" && docker compose up -d --build --wait --remove-orphans)
    log "✅ https://sidekick.$HOST"
    ;;
  "preview up")
    pr="${args[2]:-}"; branch="${args[3]:-}"
    [[ "$pr" =~ ^[0-9]{1,5}$ && "$branch" =~ ^[A-Za-z0-9._/-]{1,120}$ ]] || { echo "usage: preview up <pr-number> <branch>"; exit 2; }
    dir="$APPS/previews/pr-$pr"; port=$((9000 + pr % 1000))
    git -C "$APPS/shoplite" fetch -q origin "+refs/heads/$branch:refs/remotes/origin/$branch"
    if [ -d "$dir" ]; then git -C "$dir" checkout -q --detach "origin/$branch"; else git -C "$APPS/shoplite" worktree add -q --detach "$dir" "origin/$branch"; fi
    (cd "$dir" && COMPOSE_PROJECT_NAME="shoplite-pr-$pr" WEB_PORT="127.0.0.1:$port" PREVIEW_LABEL="pr-$pr" GIT_BRANCH="$branch" \
      APP_VERSION="$(git rev-parse --short HEAD)" docker compose up -d --build --wait)
    route "pr-$pr" "$port"
    log "✅ https://pr-$pr.$HOST"
    ;;
  "preview down")
    pr="${args[2]:-}"; valid_pr "$pr"
    docker compose -p "shoplite-pr-$pr" down -v --remove-orphans || true
    [ -d "$APPS/previews/pr-$pr" ] && git -C "$APPS/shoplite" worktree remove --force "$APPS/previews/pr-$pr"
    rm -f "/etc/caddy/sites/pr-$pr.caddy" && sudo /usr/bin/systemctl reload caddy
    log "🧹 preview pr-$pr removed"
    ;;
  "kpreview up") valid_pr "${args[2]:-}"; k_up "${args[2]}" "${args[3]:-}" "${args[4]:-}" ;;
  "kpreview down") valid_pr "${args[2]:-}"; k_down "${args[2]}" ;;
  *) deny ;;
esac
