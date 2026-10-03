#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "Applying MediaForge live-control update (encoders stay online)..."
git pull --ff-only

echo "Rebuilding only the OVH command agent..."
sudo docker compose build ovh-agent
sudo docker compose up -d --no-deps --force-recreate ovh-agent

echo "Waiting for agent..."
sleep 8
sudo docker compose ps ovh-agent

echo
echo "MEDIAFORGE_LIVE_CONTROLS_UPDATED"
echo "Kick/Twitch/YouTube encoder containers were not restarted."
