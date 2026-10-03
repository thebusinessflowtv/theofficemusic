#!/usr/bin/env bash
set -euo pipefail

REPO="/home/ubuntu/theofficemusic"
AGENT="$REPO/ovh-streaming/host_deploy_agent.py"
UNIT="/etc/systemd/system/mediaforge-deploy-agent.service"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer with sudo:"
  echo "  sudo bash $REPO/ovh-streaming/install-host-deploy-agent.sh"
  exit 1
fi

test -f "$AGENT" || { echo "Missing $AGENT"; exit 1; }
mkdir -p /var/lib/mediaforge-deploy-agent
chmod 700 /var/lib/mediaforge-deploy-agent
chmod 755 "$AGENT"

cat > "$UNIT" <<'UNIT'
[Unit]
Description=MediaForge OVH Remote Deploy Agent
After=network-online.target docker.service
Wants=network-online.target docker.service

[Service]
Type=simple
User=root
WorkingDirectory=/home/ubuntu/theofficemusic/ovh-streaming
Environment=MEDIAFORGE_API_URL=https://mediaforge-api.guilhermeodsgn.workers.dev
Environment=MEDIAFORGE_REPO=/home/ubuntu/theofficemusic
Environment=MEDIAFORGE_DEPLOY_POLL_SECONDS=5
ExecStart=/usr/bin/python3 /home/ubuntu/theofficemusic/ovh-streaming/host_deploy_agent.py
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=read-only
ReadWritePaths=/home/ubuntu/theofficemusic /var/lib/mediaforge-deploy-agent /var/run/docker.sock

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now mediaforge-deploy-agent.service
sleep 3
systemctl --no-pager --full status mediaforge-deploy-agent.service | head -30

echo
echo "MEDIAFORGE_REMOTE_DEPLOY_READY"
echo "Future deploys can be issued through MediaForge without SSH."
