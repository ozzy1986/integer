#!/usr/bin/env bash
# Release the committed HEAD to the VPS (run from Git Bash / any shell with ssh):
#   deploy/deploy.sh             upload -> build -> switch `current` -> docker compose up -> healthcheck
#   deploy/deploy.sh --rollback  switch back to the previous release (database migrations are not reverted)
# Releases live in $ROOT/releases/<time>-<sha>; the 5 newest are kept. Secrets: $ROOT/shared/.env (server only).
set -euo pipefail
cd "$(dirname "$0")/.."

HOST=${DEPLOY_HOST:-root@162.35.161.95}
ROOT=/var/www/integer.ozzy1986.com
URL=https://integer.ozzy1986.com

healthcheck() {
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 5 "$URL/health" >/dev/null; then echo "healthy: $URL"; return 0; fi
    sleep 2
  done
  echo "UNHEALTHY"
  ssh "$HOST" "cd $ROOT/current && docker compose -p integer --env-file $ROOT/shared/.env ps; \
    docker compose -p integer --env-file $ROOT/shared/.env logs --tail 40 app"
  return 1
}

if [ "${1:-}" = "--rollback" ]; then
  ssh "$HOST" "bash $ROOT/current/deploy/remote.sh --rollback"
  healthcheck
  exit 0
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "commit your changes first: only HEAD is deployed" >&2
  exit 1
fi
RELEASE=$(date +%Y%m%d-%H%M%S)-$(git rev-parse --short HEAD)
echo "--- upload $RELEASE ---"
git archive --format=tar.gz HEAD | ssh "$HOST" "mkdir -p $ROOT/releases/$RELEASE && tar xzf - -C $ROOT/releases/$RELEASE"
echo "--- build and switch ---"
ssh "$HOST" "bash $ROOT/releases/$RELEASE/deploy/remote.sh $RELEASE"
healthcheck
