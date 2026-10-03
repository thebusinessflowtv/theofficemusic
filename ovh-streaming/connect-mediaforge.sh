#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
echo "MediaForge ↔ OVH runtime migration"
echo "This keeps secrets local and rolls services one at a time."
echo

git pull --ff-only

echo "[1/4] Building current OVH image..."
sudo docker compose build

echo "[2/4] Starting control API and MediaForge OVH agent..."
sudo docker compose up -d --no-deps --force-recreate control-api ovh-agent

wait_live() {
  local slot="$1"
  local max="${2:-150}"
  local elapsed=0
  while [ "$elapsed" -lt "$max" ]; do
    status="$(python3 - "$slot" <<'PY'
import json,pathlib,sys
p=pathlib.Path("state")/sys.argv[1]/"health.json"
try:
    d=json.loads(p.read_text(encoding="utf-8"))
    print(d.get("status","unknown"))
except Exception:
    print("unknown")
PY
)"
    printf '  %-22s %s\n' "$slot" "$status"
    if [ "$status" = "live" ]; then
      return 0
    fi
    sleep 5
    elapsed=$((elapsed+5))
  done
  return 1
}

echo "[3/4] Rolling encoders to the MediaForge-aware runtime..."
for slot in kick twitch youtube-deep-house youtube-rainy; do
  echo
  echo "Recreating $slot..."
  sudo docker compose up -d --no-deps --force-recreate "$slot"
  if ! wait_live "$slot" 180; then
    echo "WARNING: $slot did not report live within 180 seconds."
    echo "Recent logs:"
    sudo docker compose logs --tail=50 "$slot" || true
  fi
done

echo
echo "[4/4] Final status"
sudo docker compose ps
echo
curl -fsS http://127.0.0.1:8787/health || true
echo
echo
echo "MEDIAFORGE_OVH_CONNECTED"
echo "You can close SSH. The ovh-agent and all encoders run under Docker restart policies."
