#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/peter-lofi}"
PACKAGE="${PACKAGE:-/tmp/peter-lofi-ovh.tgz}"
ENV_FILE="${ENV_FILE:-/tmp/peter-lofi.env}"

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    echo "Root privileges are required (root or sudo)." >&2
    exit 1
  fi
}

if ! command -v docker >/dev/null 2>&1; then
  as_root apt-get update
  as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io ca-certificates curl
fi

if ! docker compose version >/dev/null 2>&1; then
  as_root apt-get update
  if ! as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-plugin; then
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose
  fi
fi

as_root mkdir -p "$APP_DIR"
as_root rm -rf "$APP_DIR/ovh-streaming"
as_root mkdir -p "$APP_DIR/ovh-streaming"
as_root tar -xzf "$PACKAGE" -C "$APP_DIR/ovh-streaming" --strip-components=1
as_root install -m 600 "$ENV_FILE" "$APP_DIR/ovh-streaming/.env"
as_root mkdir -p "$APP_DIR/ovh-streaming/state/kick" "$APP_DIR/ovh-streaming/state/twitch"

cd "$APP_DIR/ovh-streaming"

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
else
  COMPOSE=(docker-compose)
fi

as_root "${COMPOSE[@]}" up -d --build --remove-orphans
as_root "${COMPOSE[@]}" ps

echo
echo "Peter Lofi OVH streaming core deployed."
echo "Local health endpoint: http://127.0.0.1:8787/health"
