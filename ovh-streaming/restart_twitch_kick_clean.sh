#!/usr/bin/env bash
set -euo pipefail

REPO="${MEDIAFORGE_REPO:-/home/ubuntu/theofficemusic}"
OVH="$REPO/ovh-streaming"

json_field() {
  python3 - "$1" "$2" <<'PY'
import json,sys
try:
    d=json.load(open(sys.argv[1],encoding="utf-8"))
    v=d.get(sys.argv[2])
    print("" if v is None else v)
except Exception:
    print("")
PY
}

wait_live() {
  local slot="$1"
  local health="$OVH/state/$slot/health.json"
  echo "Waiting for $slot..."
  for _ in $(seq 1 120); do
    sleep 2
    local status visual audio enc
    status="$(json_field "$health" status)"
    visual="$(json_field "$health" visual_status)"
    audio="$(json_field "$health" audio_status)"
    enc="$(json_field "$health" encoder_pid)"
    echo "$slot status=$status visual=$visual audio=$audio encoder=$enc"
    if [ "$status" = "live" ] && [ "$visual" = "streaming" ] && [ -n "$enc" ]; then
      return 0
    fi
  done
  echo "ERROR: $slot failed to become healthy" >&2
  exit 20
}

echo "=== Updating MediaForge source ==="
sudo -u ubuntu git -C "$REPO" fetch origin main
sudo -u ubuntu git -C "$REPO" reset --hard origin/main

cd "$OVH"

echo
echo "=== TWITCH: build new image while current live is still running ==="
sudo docker compose build twitch

echo "=== TWITCH: end old session and recreate only Twitch ==="
sudo docker compose up -d --no-deps --force-recreate twitch
wait_live twitch

echo
echo "=== KICK: build new image while current live is still running ==="
sudo docker compose build kick

echo "=== KICK: end old session and recreate only Kick ==="
sudo docker compose up -d --no-deps --force-recreate kick
wait_live kick

echo
echo "=== FINAL STATUS ==="
for slot in twitch kick; do
  health="$OVH/state/$slot/health.json"
  echo "$slot status=$(json_field "$health" status) visual=$(json_field "$health" visual_status) audio=$(json_field "$health" audio_status) encoder=$(json_field "$health" encoder_pid)"
done

echo "TWITCH_KICK_CLEAN_RESTART_OK"
echo "YouTube containers were not touched."
