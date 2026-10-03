#!/usr/bin/env bash
set -euo pipefail

REPO="${MEDIAFORGE_REPO:-/home/ubuntu/theofficemusic}"
OVH="$REPO/ovh-streaming"
SLOTS=(kick twitch youtube-deep-house youtube-rainy)

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

echo "== MediaForge zero-drop AV recovery =="

sudo -u ubuntu git -C "$REPO" fetch origin main
sudo -u ubuntu git -C "$REPO" reset --hard origin/main

for slot in "${SLOTS[@]}"; do
  name="peter-lofi-$slot"
  health="$OVH/state/$slot/health.json"
  visual_health="$OVH/state/$slot/visual-health.json"
  audio_health="$OVH/state/$slot/audio-health.json"

  echo
  echo "---- $slot ----"

  before_encoder="$(json_field "$health" encoder_pid)"
  before_audio="$(json_field "$health" audio_pid)"
  before_visual="$(json_field "$health" visual_pid)"
  before_status="$(json_field "$health" status)"

  echo "before: status=$before_status encoder=$before_encoder audio=$before_audio visual=$before_visual"

  [ -n "$before_encoder" ] || { echo "ERROR: missing encoder PID on $slot" >&2; exit 10; }
  [ -n "$before_audio" ] || { echo "ERROR: missing audio PID on $slot" >&2; exit 11; }
  [ -n "$before_visual" ] || { echo "ERROR: missing visual PID on $slot" >&2; exit 12; }

  sudo docker cp "$OVH/app/audio_engine.py" "$name:/app/audio_engine.py"
  sudo docker cp "$OVH/app/visual_engine.py" "$name:/app/visual_engine.py"

  # Kill only child feeders; never touch the encoder/publisher.
  sudo docker exec "$name" python -c "import os,signal; os.kill(int('$before_audio'), signal.SIGTERM)"
  sudo docker exec "$name" python -c "import os,signal; os.kill(int('$before_visual'), signal.SIGTERM)"

  ok=0
  for _ in $(seq 1 45); do
    sleep 1
    after_encoder="$(json_field "$health" encoder_pid)"
    after_audio="$(json_field "$health" audio_pid)"
    after_visual="$(json_field "$health" visual_pid)"
    visual_state="$(json_field "$visual_health" status)"
    audio_state="$(json_field "$audio_health" state)"

    if [ -n "$after_encoder" ] && [ "$after_encoder" != "$before_encoder" ]; then
      echo "ERROR: encoder PID changed on $slot: $before_encoder -> $after_encoder" >&2
      exit 20
    fi

    if [ "$after_encoder" = "$before_encoder" ] &&
       [ -n "$after_audio" ] && [ "$after_audio" != "$before_audio" ] &&
       [ -n "$after_visual" ] && [ "$after_visual" != "$before_visual" ] &&
       [ "$visual_state" = "streaming" ] &&
       { [ "$audio_state" = "playing" ] || [ "$audio_state" = "track_finished" ]; }; then
      ok=1
      break
    fi
  done

  [ "$ok" = "1" ] || {
    echo "ERROR: AV feeders failed to recover on $slot" >&2
    echo "encoder=$(json_field "$health" encoder_pid) audio=$(json_field "$health" audio_pid) visual=$(json_field "$health" visual_pid) visual_state=$(json_field "$visual_health" status) audio_state=$(json_field "$audio_health" state)" >&2
    exit 21
  }

  sleep 2
  after_encoder="$(json_field "$health" encoder_pid)"
  after_audio="$(json_field "$health" audio_pid)"
  after_visual="$(json_field "$health" visual_pid)"
  visual_state="$(json_field "$visual_health" status)"
  audio_state="$(json_field "$audio_health" state)"
  drops="$(json_field "$audio_health" backpressure_drops)"

  echo "after: encoder=$after_encoder audio=$after_audio visual=$after_visual visual_state=$visual_state audio_state=$audio_state backpressure_drops=${drops:-0}"
  echo "encoder_preserved=True"
done

echo
echo "ZERO_DROP_AV_RECOVERY_OK"
echo "All RTMP encoder PIDs were preserved."
