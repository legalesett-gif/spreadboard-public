#!/usr/bin/env bash
# Deploy to production without destroying an in-flight discovery scan.
#
# A collector restart kills the running api_discovery_worker, and a full scan
# takes 45-60 minutes. Nothing structural advances until one completes, so a
# deploy landed mid-scan costs the better part of an hour of board freshness.
# This check existed only as something the operator remembered to run; it was
# broken three times in one hour on 2026-08-31.
#
#   ./scripts/deploy_production.sh app collector      # refuses during a scan
#   ./scripts/deploy_production.sh --force app        # deliberate, scan is lost
#
set -euo pipefail

HOST="${SPREADBOARD_DEPLOY_HOST:-root@178.128.126.204}"
KEY="${SPREADBOARD_DEPLOY_KEY:-$HOME/.ssh/spreadboard_digitalocean}"
APP_DIR="${SPREADBOARD_DEPLOY_DIR:-/opt/spreadboard/app}"
COMPOSE="compose.production.yml"

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
echo "==> deploy OK"
