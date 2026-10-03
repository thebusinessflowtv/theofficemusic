#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "Updating MediaForge OVH command transport..."
echo "Encoders will NOT be restarted."
git pull --ff-only

echo "[1/3] Rebuilding OVH agent only..."
sudo docker compose build ovh-agent
sudo docker compose up -d --no-deps --force-recreate ovh-agent

echo "[2/3] Waiting for D1 queue connection..."
sleep 10
sudo docker compose ps ovh-agent

echo
echo "[3/3] Recent agent activity..."
sudo docker compose logs --tail=80 ovh-agent || true

echo
echo "MEDIAFORGE_COMMAND_QUEUE_V2_ACTIVE"
echo "Twitch/Kick/YouTube encoders were not restarted."
echo "Next/Previous/Playlist commands now use Cloudflare D1 first, with GitHub only as fallback."
