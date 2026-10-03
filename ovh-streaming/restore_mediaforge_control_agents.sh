#!/usr/bin/env bash
set -euo pipefail

REPO="${MEDIAFORGE_REPO:-/home/ubuntu/theofficemusic}"
OVH="$REPO/ovh-streaming"
API="https://mediaforge-api.guilhermeodsgn.workers.dev"

echo "== Restore MediaForge control agents =="
sudo -u ubuntu git -C "$REPO" fetch origin main
sudo -u ubuntu git -C "$REPO" reset --hard origin/main

cd "$OVH"

# Persist the correct control-plane URL for future agent restarts.
if [ -f .env ]; then
  if grep -q '^MEDIAFORGE_API_URL=' .env; then
    sed -i.bak 's#^MEDIAFORGE_API_URL=.*#MEDIAFORGE_API_URL=https://mediaforge-api.guilhermeodsgn.workers.dev#' .env
  else
    printf '\nMEDIAFORGE_API_URL=%s\n' "$API" >> .env
  fi
else
  printf 'MEDIAFORGE_API_URL=%s\n' "$API" > .env
fi

echo "Building ovh-agent only..."
sudo docker compose build ovh-agent
echo "Recreating ovh-agent only..."
sudo env MEDIAFORGE_API_URL="$API" docker compose up -d --no-deps --force-recreate ovh-agent

# Ensure the host deploy agent also uses the same control plane.
sudo mkdir -p /etc/systemd/system/mediaforge-deploy-agent.service.d
printf '[Service]\nEnvironment="MEDIAFORGE_API_URL=%s"\n' "$API" | sudo tee /etc/systemd/system/mediaforge-deploy-agent.service.d/mediaforge-api.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl restart mediaforge-deploy-agent

sleep 8

echo
echo "=== CONTROL AGENT ==="
sudo docker compose ps ovh-agent
echo
echo "=== DEPLOY AGENT ==="
sudo systemctl is-active mediaforge-deploy-agent

echo
echo "=== OVH AGENT RECENT LOG ==="
sudo docker logs --tail 40 peter-lofi-ovh-agent 2>&1 | tail -40

echo
echo "MEDIAFORGE_CONTROL_AGENTS_OK"
echo "Streaming containers were not restarted."
