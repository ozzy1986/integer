#!/usr/bin/env bash
# Runs on the server as root, called by deploy/deploy.sh:
#   remote.sh <release>    build images, switch `current`, start the stack, prune old releases
#   remote.sh --rollback   start the release before `current`
set -euo pipefail
ROOT=/var/www/integer.ozzy1986.com
ENV_FILE=$ROOT/shared/.env
compose() { docker compose -p integer --env-file "$ENV_FILE" "$@"; }

switch_to() {
  ln -sfn "$1" "$ROOT/current.tmp" && mv -Tf "$ROOT/current.tmp" "$ROOT/current"
  cd "$ROOT/current"
  compose up -d --build --wait --remove-orphans
}

if [ "${1:-}" = "--rollback" ]; then
  CURRENT=$(readlink -f "$ROOT/current")
  PREVIOUS=$(ls -1d "$ROOT"/releases/* | sort | awk -v cur="$CURRENT" '$0 < cur' | tail -1)
  [ -n "$PREVIOUS" ] || { echo "no release older than $CURRENT"; exit 1; }
  echo "rollback: $CURRENT -> $PREVIOUS"
  switch_to "$PREVIOUS"
  exit 0
fi

RELEASE_DIR=$ROOT/releases/$1
bash "$RELEASE_DIR/deploy/setup-server.sh"
# Build before switching, so a broken build never touches the running release.
(cd "$RELEASE_DIR" && compose build)
switch_to "$RELEASE_DIR"
ls -1d "$ROOT"/releases/* | sort | head -n -5 | xargs -r rm -rf
docker image prune -f >/dev/null
compose ps --format "table {{.Service}}\t{{.Status}}"
