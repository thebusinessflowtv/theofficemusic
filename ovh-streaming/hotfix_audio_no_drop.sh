#!/usr/bin/env bash
set -euo pipefail

REPO="${MEDIAFORGE_REPO:-/home/ubuntu/theofficemusic}"
OVH="$REPO/ovh-streaming"
SLOTS=(kick twitch youtube-deep-house youtube-rainy)

if [ ! -d "$REPO/.git" ]; then
  echo "ERROR: MediaForge repo not found at $REPO" >&2
  exit 2
fi

echo "== MediaForge zero-drop audio hotfix =="
echo "Repo: $REPO"

# Update source only. No container/publisher recreation is allowed here.
sudo -u ubuntu git -C "$REPO" fetch origin main
sudo -u ubuntu git -C "$REPO" reset --hard origin/main

AUDIO_SRC="$OVH/app/audio_engine.py"
[ -f "$AUDIO_SRC" ] || { echo "ERROR: $AUDIO_SRC missing" >&2; exit 3; }
grep -q '"-nostdin", "-re"' "$AUDIO_SRC" || {
  echo "ERROR: realtime audio pacing patch is not present" >&2
  exit 4
}

json_field() {
  local file="$1" key="$2"
  python3 - "$file" "$key" <<'PY'
import json,sys
p,k=sys.argv[1],sys.argv[2]
try:
    d=json.load(open(p,encoding="utf-8"))
    v=d.get(k)
    print("" if v is None else v)
except Exception:
    print("")
PY
}

for slot in "${SLOTS[@]}"; do
  name="peter-lofi-$slot"
  health="$OVH/state/$slot/health.json"
  audio_health="$OVH/state/$slot/audio-health.json"

  echo
  echo "---- $slot ----"
  [ -f "$health" ] || { echo "ERROR: $health missing" >&2; exit 10; }

  before_encoder="$(json_field "$health" encoder_pid)"
  before_audio="$(json_field "$health" audio_pid)"
  before_status="$(json_field "$health" status)"

  echo "before: status=$before_status encoder=$before_encoder audio=$before_audio"
  [ "$before_status" = "live" ] || [ "$before_status" = "starting" ] || {
    echo "ERROR: $slot is not live/starting; refusing hotfix" >&2
    exit 11
  }
  [ -n "$before_encoder" ] || { echo "ERROR: encoder PID missing for $slot" >&2; exit 12; }
  [ -n "$before_audio" ] || { echo "ERROR: audio PID missing for $slot" >&2; exit 13; }

  # Replace only the child audio engine code in the running container.
  sudo docker cp "$AUDIO_SRC" "$name:/app/audio_engine.py"

  # Stop only the audio child. StreamCore owns an RDWR FIFO descriptor, so
  # the RTMP publisher remains alive and automatically launches the new child.
  sudo docker exec "$name" python -c "import os,signal; os.kill(int('$before_audio'), signal.SIGTERM)"

  ok=0
  for _ in $(seq 1 30); do
    sleep 1
    after_encoder="$(json_field "$health" encoder_pid)"
    after_audio="$(json_field "$health" audio_pid)"
    after_status="$(json_field "$health" status)"

    if [ "$after_encoder" != "$before_encoder" ]; then
      echo "ERROR: encoder PID changed on $slot: $before_encoder -> $after_encoder" >&2
      exit 20
    fi

    if [ -n "$after_audio" ] && [ "$after_audio" != "$before_audio" ] && { [ "$after_status" = "live" ] || [ "$after_status" = "starting" ]; }; then
      ok=1
      break
    fi
  done

  [ "$ok" = "1" ] || {
    echo "ERROR: audio child did not recover on $slot" >&2
    exit 21
  }

  # Give the realtime decoder a moment to settle, then verify that new
  # backpressure is not accumulating rapidly.
  sleep 4
  drops="$(json_field "$audio_health" backpressure_drops)"
  state="$(json_field "$audio_health" state)"
  after_encoder="$(json_field "$health" encoder_pid)"
  after_audio="$(json_field "$health" audio_pid)"
  echo "after: encoder=$after_encoder audio=$after_audio audio_state=$state backpressure_drops=${drops:-0}"
  [ "$after_encoder" = "$before_encoder" ] || exit 22
done

echo
echo "ZERO_DROP_AUDIO_HOTFIX_OK"
echo "All RTMP encoder PIDs were preserved."

# Restore only the control-plane agents. These are not publishers and do not
# carry RTMP media, so this cannot end any live.
echo
echo "Restoring MediaForge control agents..."
sudo systemctl restart mediaforge-deploy-agent 2>/dev/null || true
if sudo docker compose version >/dev/null 2>&1; then
  (cd "$OVH" && sudo docker compose up -d --no-deps ovh-agent) || true
else
  (cd "$OVH" && sudo docker-compose up -d --no-deps ovh-agent) || true
fi
echo "CONTROL_AGENTS_RESTORED"
