#!/usr/bin/env bash
# Deploy to production without destroying an in-flight discovery scan.
#
# A collector restart kills the running api_discovery_worker, and a full scan
# takes 45-60 minutes. Nothing structural advances until one completes, so a
# deploy landed mid-scan costs the better part of an hour of board freshness.
# This check existed only as something the operator remembered to run; it was
# broken three times in one hour on 2026-08-31.
#
# It also ships the working tree it is run from. The server build directory was
# kept in step by hand, so on 2026-09-03 this script reported "deploy OK" three
# times while the container went on running a market_history.py from five days
# earlier -- health was 200 and the restart counts were flat, and neither says
# anything about which code came back up. The source digest at the end does.
#
#   ./scripts/deploy_production.sh app collector      # refuses during a scan
#   ./scripts/deploy_production.sh --force app        # deliberate, scan is lost
#
set -euo pipefail

HOST="${SPREADBOARD_DEPLOY_HOST:-root@178.128.126.204}"
KEY="${SPREADBOARD_DEPLOY_KEY:-$HOME/.ssh/spreadboard_digitalocean}"
APP_DIR="${SPREADBOARD_DEPLOY_DIR:-/opt/spreadboard/app}"
COMPOSE="compose.production.yml"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_TREES=(spreadboard src scripts)

FORCE=0
if [[ "${1:-}" == "--force" ]]; then
  FORCE=1
  shift
fi

SERVICES=("$@")
if [[ ${#SERVICES[@]} -eq 0 ]]; then
  echo "usage: $0 [--force] <service> [service...]" >&2
  exit 2
fi

ssh_do() { ssh -i "$KEY" -o ConnectTimeout=60 "$HOST" "$@"; }

scan_running() {
  ssh_do 'docker top app-collector-1 2>/dev/null | grep -c api_discovery_worker || true' | tr -d '[:space:]'
}

restarts_before=$(ssh_do 'docker inspect app-app-1 app-collector-1 --format "{{.RestartCount}}" | paste -sd, -')

RUNNING=$(scan_running)
if [[ "$RUNNING" != "0" && "$RUNNING" != "" ]]; then
  if [[ $FORCE -eq 0 ]]; then
    cat >&2 <<MSG
REFUSING: a discovery scan is in flight ($RUNNING worker(s)).

Restarting the collector now destroys 45-60 minutes of scan work and nothing
structural advances until another completes. Wait for it to finish, or re-run
with --force if losing it is the intended trade.
MSG
    exit 1
  fi
  echo "WARNING: --force given; discarding an in-flight discovery scan." >&2
fi

echo "==> syncing source"
for tree in "${SOURCE_TREES[@]}"; do
  rsync -a --delete \
    --exclude '__pycache__/' --exclude '*.pyc' \
    -e "ssh -i $KEY -o ConnectTimeout=60" \
    "$REPO_ROOT/$tree/" "$HOST:$APP_DIR/$tree/"
done
rsync -a -e "ssh -i $KEY -o ConnectTimeout=60" \
  "$REPO_ROOT/pyproject.toml" "$REPO_ROOT/uv.lock" "$REPO_ROOT/Dockerfile" \
  "$REPO_ROOT/$COMPOSE" "$HOST:$APP_DIR/"

echo "==> building: ${SERVICES[*]}"
ssh_do "cd $APP_DIR && docker compose -f $COMPOSE build ${SERVICES[*]}"

echo "==> restarting: ${SERVICES[*]}"
ssh_do "cd $APP_DIR && docker compose -f $COMPOSE up -d --no-deps ${SERVICES[*]}"

echo "==> verifying"
sleep 20
code=$(curl -s -o /dev/null -w "%{http_code}" -m 45 https://spreadarbitrage.ink/api/health || echo 000)
echo "health=$code"
restarts_after=$(ssh_do 'docker inspect app-app-1 app-collector-1 --format "{{.RestartCount}}" | paste -sd, -')
echo "restart counts: before=$restarts_before after=$restarts_after"
ssh_do 'docker inspect app-app-1 app-collector-1 --format "{{.Name}} oom={{.State.OOMKilled}} status={{.State.Status}}"'

[[ "$code" == "200" ]] || { echo "health check failed" >&2; exit 1; }

# A 200 only says something is serving. This says it is serving THIS source.
want=$(cd "$REPO_ROOT" && python3 scripts/source_digest.py .)
for service in "${SERVICES[@]}"; do
  got=$(ssh_do "docker exec app-${service}-1 python /app/scripts/source_digest.py /app" 2>/dev/null | tr -d '[:space:]')
  echo "source $service: want=$want got=$got"
  [[ "$got" == "$want" ]] || {
    echo "REFUSING to report success: app-${service}-1 is running different source." >&2
    exit 1
  }
done
echo "==> deploy OK"
