#!/usr/bin/env bash
set -euo pipefail

REPO="${MEDIAFORGE_REPO:-/home/ubuntu/theofficemusic}"
OVH="$REPO/ovh-streaming"
SLOT="twitch"
NAME="peter-lofi-twitch"
HEALTH="$OVH/state/$SLOT/health.json"
PLAYLIST="$OVH/state/$SLOT/playlist.json"
AUDIO_HEALTH="$OVH/state/$SLOT/audio-health.json"
NOW="$OVH/state/$SLOT/now-playing.json"

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

echo "== Twitch DJ balanced hotfix =="

sudo -u ubuntu git -C "$REPO" fetch origin main
sudo -u ubuntu git -C "$REPO" reset --hard origin/main

python3 - "$PLAYLIST" <<'PY'
import json,sys,os
p=sys.argv[1]
d=json.load(open(p,encoding="utf-8"))
tracks=d.get("tracks") or []
commercial=[]
original=[]
missing=[]
for t in tracks:
    tid=str(t.get("id") or "")
    src=str(t.get("source") or "")
    is_com=src=="twitch_dj_catalog_licensed_copy" or (tid.startswith("twitch-dj-") and not tid.startswith("twitch-dj-original-"))
    (commercial if is_com else original).append(t)
    u=str(t.get("url") or "")
    if is_com and u.startswith("file://"):
        path=u[7:]
        if not os.path.exists(path):
            marker="/ovh-streaming/state/"
            if marker in path:
                alt="/state/"+path.split(marker,1)[1]
                if not os.path.exists(alt):
                    missing.append(path)
            else:
                missing.append(path)
print(f"playlist_key={d.get('playlist_key')} total={len(tracks)} original={len(original)} commercial={len(commercial)} missing_commercial_files={len(missing)}")
if d.get("playlist_key")!="twitch-dj-mixed":
    raise SystemExit("ERROR: Twitch is not using twitch-dj-mixed")
if len(commercial)<1:
    raise SystemExit("ERROR: no commercial tracks in Twitch playlist")
if missing:
    raise SystemExit("ERROR: commercial MP3 files missing on OVH")
PY

before_encoder="$(json_field "$HEALTH" encoder_pid)"
before_audio="$(json_field "$HEALTH" audio_pid)"
echo "before encoder=$before_encoder audio=$before_audio"
test -n "$before_encoder"
test -n "$before_audio"

sudo docker cp "$OVH/app/audio_engine.py" "$NAME:/app/audio_engine.py"
sudo docker exec "$NAME" python -c "import os,signal; os.kill(int('$before_audio'), signal.SIGTERM)"

ok=0
for _ in $(seq 1 45); do
  sleep 1
  after_encoder="$(json_field "$HEALTH" encoder_pid)"
  after_audio="$(json_field "$HEALTH" audio_pid)"
  state="$(json_field "$AUDIO_HEALTH" state)"
  if [ -n "$after_encoder" ] && [ "$after_encoder" != "$before_encoder" ]; then
    echo "ERROR: Twitch encoder changed: $before_encoder -> $after_encoder" >&2
    exit 20
  fi
  if [ "$after_encoder" = "$before_encoder" ] && [ -n "$after_audio" ] && [ "$after_audio" != "$before_audio" ] && { [ "$state" = "playing" ] || [ "$state" = "track_finished" ]; }; then
    ok=1
    break
  fi
done

test "$ok" = "1"
echo "after encoder=$(json_field "$HEALTH" encoder_pid) audio=$(json_field "$HEALTH" audio_pid) state=$(json_field "$AUDIO_HEALTH" state)"
echo "now_playing=$(json_field "$NOW" title)"
echo "TWITCH_DJ_BALANCE_OK"
echo "RTMP encoder preserved."
