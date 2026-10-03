#!/usr/bin/env bash
set -euo pipefail

REPO="/home/ubuntu/theofficemusic"
OVH="$REPO/ovh-streaming"
STATE="$OVH/state"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo."
  exit 1
fi

cd "$OVH"

echo "=== MediaForge OVH local-runtime migration ==="
echo "This operation does NOT recreate Kick or either YouTube container."
echo "It reloads the host watchdog, replaces the control agent, and reconnects only Twitch."

old_twitch_pid=""
if [ -f "$STATE/twitch/health.json" ]; then
  old_twitch_pid="$(python3 - <<'PY'
import json, pathlib
p=pathlib.Path("state/twitch/health.json")
try:
    print(json.loads(p.read_text()).get("encoder_pid") or "")
except Exception:
    print("")
PY
)"
fi

echo "[1/5] Reloading local host deploy agent/watchdog..."
systemctl daemon-reload
systemctl restart mediaforge-deploy-agent.service
sleep 4
systemctl is-active --quiet mediaforge-deploy-agent.service
echo "Host watchdog: active"

echo "[2/5] Building updated OVH control image..."
docker compose build ovh-agent twitch

echo "[3/5] Replacing only the non-streaming OVH control agent..."
docker compose up -d --no-deps --force-recreate ovh-agent
sleep 8
docker compose ps ovh-agent

echo "[4/5] Reconnecting only Twitch to Twitch's current official global ingest..."
docker compose up -d --no-deps --force-recreate twitch

echo "[5/5] Waiting for Twitch to become healthy..."
for I in $(seq 1 80); do
  if [ -f "$STATE/twitch/health.json" ]; then
    read -r STATUS HOT PID <<<"$(python3 - <<'PY'
import json, pathlib
try:
    d=json.loads(pathlib.Path("state/twitch/health.json").read_text())
    print(d.get("status",""), str(bool(d.get("hot_swap",False))).lower(), d.get("encoder_pid") or "")
except Exception:
    print("  ")
PY
)"
    echo "Twitch status=$STATUS hot_swap=$HOT encoder_pid=$PID"
    if [ "$STATUS" = "live" ] && [ "$HOT" = "true" ] && [ -n "$PID" ]; then
      break
    fi
  fi
  sleep 3
done

python3 - <<'PY'
import json, pathlib
root=pathlib.Path("state")
slots=("kick","twitch","youtube-deep-house","youtube-rainy")
print("\nFinal local health:")
bad=[]
for slot in slots:
    p=root/slot/"health.json"
    try:
        d=json.loads(p.read_text())
    except Exception as exc:
        print(f"- {slot}: unreadable ({exc})")
        bad.append(slot)
        continue
    print(
        f"- {slot}: status={d.get('status')} "
        f"encoder_pid={d.get('encoder_pid')} "
        f"restarts={d.get('restarts')} "
        f"hot_swap={d.get('hot_swap')}"
    )
    if d.get("status")!="live":
        bad.append(slot)
if bad:
    print("WARNING_NON_LIVE="+",".join(bad))
PY

new_twitch_pid="$(python3 - <<'PY'
import json, pathlib
try:
    print(json.loads(pathlib.Path("state/twitch/health.json").read_text()).get("encoder_pid") or "")
except Exception:
    print("")
PY
)"

echo
echo "TWITCH_OLD_ENCODER_PID=$old_twitch_pid"
echo "TWITCH_NEW_ENCODER_PID=$new_twitch_pid"
echo "OVH_LOCAL_RUNTIME_ACTIVE"
echo "GITHUB_LIVE_RUNTIME_POLLING_DISABLED"
echo "CLOUDFLARE_IS_CONTROL_PLANE_ONLY"
echo "KICK_AND_YOUTUBE_CONTAINERS_NOT_RECREATED"
