#!/usr/bin/env bash
# Forced command for the GitHub Actions deploy key (authorized_keys: command="/opt/apps/gate.sh",restrict ...).
# The key can run ONLY these actions — no shell:
#
#   deploy shoplite            pull master of shoplite-target and rebuild ShopLite (https://shop.<host>)
#   deploy sidekick            pull master of hackmidwest_2026_sidekick and rebuild Sidekick
#   preview up <pr> <branch>   build that branch as its own stack → https://pr-<pr>.<host>
#   preview down <pr>          remove the preview, its database and its route
set -euo pipefail
APPS=/opt/apps
HOST="$(cat "$APPS/host")"               # e.g. 172-105-1-2.sslip.io (written by bootstrap.sh)
read -r -a args <<< "${SSH_ORIGINAL_COMMAND:-}"
log() { echo "[gate] $*"; }

pull() {  # $1 = repo dir: hard-reset to the remote default branch (the server never has local edits)
  git -C "$1" fetch -q --prune origin
  git -C "$1" reset -q --hard "origin/$(git -C "$1" rev-parse --abbrev-ref origin/HEAD | sed 's#origin/##')"
  log "$(basename "$1") at $(git -C "$1" log -1 --format='%h %s')"
}

route() {  # $1 = site name, $2 = port → Caddy site file, reload
  printf '%s.%s {\n\treverse_proxy 127.0.0.1:%s\n}\n' "$1" "$HOST" "$2" > "/etc/caddy/sites/$1.caddy"
  sudo /usr/bin/systemctl reload caddy
}

case "${args[0]:-} ${args[1]:-}" in
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
    pr="${args[2]:-}"
    [[ "$pr" =~ ^[0-9]{1,5}$ ]] || { echo "usage: preview down <pr-number>"; exit 2; }
    docker compose -p "shoplite-pr-$pr" down -v --remove-orphans || true
    [ -d "$APPS/previews/pr-$pr" ] && git -C "$APPS/shoplite" worktree remove --force "$APPS/previews/pr-$pr"
    rm -f "/etc/caddy/sites/pr-$pr.caddy" && sudo /usr/bin/systemctl reload caddy
    log "🧹 preview pr-$pr removed"
    ;;
  *)
    echo "Not allowed: '${SSH_ORIGINAL_COMMAND:-}'. Allowed: deploy shoplite | deploy sidekick | preview up <pr> <branch> | preview down <pr>"
    exit 2
    ;;
esac
