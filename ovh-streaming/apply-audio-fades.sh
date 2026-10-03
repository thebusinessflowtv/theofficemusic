#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "Applying smooth audio fades to Peter Lofi live encoders..."
git pull --ff-only

echo "[1/2] Building updated streaming image..."
sudo docker compose build kick twitch youtube-deep-house youtube-rainy

wait_live() {
  local slot="$1"
  local max="${2:-180}"
  local elapsed=0
  while [ "$elapsed" -lt "$max" ]; do
    status="$(python3 - "$slot" <<'PY'
import json,pathlib,sys
p=pathlib.Path("state")/sys.argv[1]/"health.json"
try:
    print(json.loads(p.read_text(encoding="utf-8")).get("status","unknown"))
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

echo "[2/2] Rolling encoders one by one..."
for slot in twitch kick youtube-deep-house youtube-rainy; do
  echo
  echo "Updating $slot..."
  sudo docker compose up -d --no-deps --force-recreate "$slot"
  if ! wait_live "$slot" 180; then
    echo "WARNING: $slot did not report live within 180 seconds."
    sudo docker compose logs --tail=60 "$slot" || true
    exit 1
  fi
done

echo
echo "PETER_LOFI_AUDIO_FADES_ACTIVE"
echo "Fade-out: 1.5s"
echo "Fade-in: 1.5s"
echo "Track changes are now smoothed on Twitch, Kick and both YouTube slots."
