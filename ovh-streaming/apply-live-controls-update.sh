#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "Applying MediaForge live-control update without restarting encoders..."
git pull --ff-only

echo "[1/4] Rebuilding only the OVH command agent..."
sudo docker compose build ovh-agent
sudo docker compose up -d --no-deps --force-recreate ovh-agent

echo "[2/4] Waiting for agent..."
sleep 8
sudo docker compose ps ovh-agent

echo "[3/4] Forcing Twitch onto the 36-track Gaming playlist without restarting the Twitch encoder..."
python3 - <<'PY'
import json, pathlib, shutil, time, uuid
root=pathlib.Path(".")
src=root/"stations"/"gaming.json"
dst=root/"state"/"twitch"/"playlist.json"
dst.parent.mkdir(parents=True,exist_ok=True)
data=json.loads(src.read_text(encoding="utf-8"))
tracks=data.get("tracks") or []
if len(tracks) < 2:
    raise SystemExit("Gaming playlist does not contain individual tracks.")
tmp=dst.with_suffix(".json.tmp")
tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
tmp.replace(dst)
cmd=root/"state"/"twitch"/"command.json"
payload={
  "id":"manual-cutover-"+str(uuid.uuid4()),
  "action":"skip",
  "requested_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
  "source":"apply-live-controls-update"
}
tmp=cmd.with_suffix(".json.tmp")
tmp.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
tmp.replace(cmd)
print(f"Twitch playlist ready: {len(tracks)} individual tracks")
print("Valid Twitch skip command written.")
PY

echo "[4/4] Waiting for Twitch now-playing to leave the 3-hour master..."
for N in $(seq 1 20); do
  if [ -f state/twitch/now-playing.json ]; then
    TITLE="$(python3 - <<'PY'
import json,pathlib
p=pathlib.Path("state/twitch/now-playing.json")
try:
    print(json.loads(p.read_text(encoding="utf-8")).get("title",""))
except Exception:
    print("")
PY
)"
    echo "Twitch now playing: $TITLE"
    case "$TITLE" in
      *"Current 3 Hour Mix"*) ;;
      "") ;;
      *) echo; echo "MEDIAFORGE_LIVE_CONTROLS_UPDATED"; echo "TWITCH_INDIVIDUAL_TRACKS_ACTIVE"; echo "No Twitch/Kick/YouTube encoder container was restarted."; exit 0 ;;
    esac
  fi
  sleep 2
done

echo
echo "MEDIAFORGE_LIVE_CONTROLS_UPDATED"
echo "Agent updated, but Twitch now-playing did not change within 40 seconds."
echo "Encoders were not restarted."
