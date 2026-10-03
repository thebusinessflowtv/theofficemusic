#!/usr/bin/env bash
set -euo pipefail

REPO="${MEDIAFORGE_REPO:-/home/ubuntu/theofficemusic}"
OVH="$REPO/ovh-streaming"
PLAYLIST="$OVH/state/twitch/playlist.json"
NOWPLAY="$OVH/state/twitch/now-playing.json"
COMMAND="$OVH/state/twitch/command.json"

echo "=== Updating source ==="
sudo -u ubuntu git -C "$REPO" fetch origin main
sudo -u ubuntu git -C "$REPO" reset --hard origin/main

echo "=== Removing Love On My Mind from Twitch local playlist ==="
python3 - "$PLAYLIST" "$NOWPLAY" "$COMMAND" <<'PY'
import json,sys,os,tempfile,uuid,datetime
playlist_path,now_path,command_path=sys.argv[1:4]
with open(playlist_path,encoding="utf-8") as f:
    p=json.load(f)
tracks=p.get("tracks") or []
def target(t):
    title=str(t.get("title") or "").lower()
    artists=str(t.get("artists") or "").lower()
    return "love on my mind" in title and ("lucas" in artists or "steve" in artists)
removed=[t for t in tracks if target(t)]
p["tracks"]=[t for t in tracks if not target(t)]
p["updated_at"]=datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00","Z")
tmp=playlist_path+".tmp"
with open(tmp,"w",encoding="utf-8") as f:
    json.dump(p,f,ensure_ascii=False,indent=2); f.write("\n")
os.replace(tmp,playlist_path)
print("removed_count="+str(len(removed)))
print("playlist_track_count="+str(len(p["tracks"])))
try:
    now=json.load(open(now_path,encoding="utf-8"))
except Exception:
    now={}
if target(now):
    cmd={"id":str(uuid.uuid4()),"action":"skip","requested_at":datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00","Z"),"source":"remove-love-on-my-mind"}
    tmp=command_path+".tmp"
    with open(tmp,"w",encoding="utf-8") as f:
        json.dump(cmd,f,ensure_ascii=False,indent=2); f.write("\n")
    os.replace(tmp,command_path)
    print("current_track_was_removed=True; skipped=True")
else:
    print("current_track_was_removed=False")
PY

echo "=== Restoring MediaForge music control agent only ==="
cd "$OVH"
if sudo docker compose version >/dev/null 2>&1; then
  sudo docker compose up -d --no-deps --force-recreate ovh-agent
else
  sudo docker-compose up -d --no-deps --force-recreate ovh-agent
fi

echo "=== Agent status ==="
sudo docker ps --filter name=peter-lofi-ovh-agent --format 'name={{.Names}} status={{.Status}}'
echo "MUSIC_CONTROL_AGENT_RESTORED"
echo "No Twitch/Kick/YouTube publisher was restarted."
