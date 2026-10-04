#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${MEDIAFORGE_REPO:-/home/ubuntu/theofficemusic}"
OVH_DIR="$REPO_DIR/ovh-streaming"
STATE="$OVH_DIR/state"
TWITCH="$STATE/twitch"
KICK="$STATE/kick"
BACKUPS="$KICK/playlist-backups"
KICK_DJ_AUDIO="$STATE/kick-dj-audio"

mkdir -p "$BACKUPS" "$KICK_DJ_AUDIO"

python3 - "$TWITCH" "$KICK" "$BACKUPS" "$KICK_DJ_AUDIO" <<'PY'
import copy, json, os, pathlib, shutil, sys, time, uuid
from datetime import datetime, timezone

twitch=pathlib.Path(sys.argv[1])
kick=pathlib.Path(sys.argv[2])
backups=pathlib.Path(sys.argv[3])
kick_audio=pathlib.Path(sys.argv[4])

def read(path):
    return json.loads(path.read_text(encoding="utf-8"))

def atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    tmp.replace(path)

def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")

twitch_playlist=read(twitch/"playlist.json")
kick_playlist=read(kick/"playlist.json")
kick_desired=read(kick/"desired.json")

tracks=list(twitch_playlist.get("tracks") or [])
if not tracks:
    raise SystemExit("ERRO: playlist da Twitch está vazia.")
if str(twitch_playlist.get("playlist_key") or "")!="twitch-dj-mixed":
    raise SystemExit(
        "ERRO: playlist atual da Twitch não é twitch-dj-mixed: "
        +str(twitch_playlist.get("playlist_key"))
    )

stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
backup_path=backups/f"{stamp}-kick-before-twitch-dj-copy.json"
backup={
    "backup_type":"kick_playlist_safety_backup",
    "created_at":now(),
    "playlist":kick_playlist,
    "desired_playlist_key":kick_desired.get("playlist_key"),
}
atomic(backup_path,backup)
atomic(backups/"LATEST.json",{
    "created_at":now(),
    "backup_path":str(backup_path),
    "playlist_key":kick_playlist.get("playlist_key"),
    "track_count":len(kick_playlist.get("tracks") or []),
})

new_tracks=[]
linked=0
copied=0
for i,row in enumerate(tracks,1):
    t=copy.deepcopy(row)
    old_id=str(t.get("id") or f"track-{i:03d}")
    t["id"]="kick-copy-"+old_id
    t["position"]=i

    url=str(t.get("url") or "")
    prefix="file:///state/twitch-dj-audio/"
    if url.startswith(prefix):
        filename=url[len(prefix):]
        src=pathlib.Path("/state/twitch-dj-audio")/filename
        dst=pathlib.Path("/state/kick-dj-audio")/filename
        if not src.exists():
            raise SystemExit(f"ERRO: arquivo comercial ausente: {src}")
        if not dst.exists():
            try:
                os.link(src,dst)
                linked+=1
            except OSError:
                shutil.copy2(src,dst)
                copied+=1
        t["url"]="file:///state/kick-dj-audio/"+filename
        t["source"]="kick_independent_commercial_copy"
    elif str(t.get("source") or "").startswith("twitch"):
        t["source"]="kick_independent_catalog_copy"

    new_tracks.append(t)

new_playlist={
    "station":"kick",
    "playlist_key":"kick-dj-mixed-independent",
    "shuffle":True,
    "repeat":True,
    "updated_at":now(),
    "copied_from":"twitch-dj-mixed",
    "independent_player":True,
    "tracks":new_tracks,
}
atomic(kick/"playlist.json",new_playlist)

# Update only playlist metadata. DO NOT touch generation/desired live state.
kick_desired["runtime"]="ovh"
kick_desired["runtime_slot"]="kick"
kick_desired["playlist_key"]="kick-dj-mixed-independent"
kick_desired["updated_at"]=now()
atomic(kick/"desired.json",kick_desired)

# Ask only the Kick audio engine to transition with its normal fade.
cid="kick-independent-dj-"+uuid.uuid4().hex
payload={
    "id":cid,
    "action":"skip",
    "requested_at":now(),
    "source":"kick-independent-playlist-copy",
}
qdir=kick/"audio-commands"
qdir.mkdir(parents=True,exist_ok=True)
atomic(qdir/f"{time.time_ns():020d}-{cid}.json",payload)
atomic(kick/"command.json",payload)

result={
    "ok":True,
    "applied_at":now(),
    "playlist_key":"kick-dj-mixed-independent",
    "track_count":len(new_tracks),
    "backup_path":str(backup_path),
    "hardlinked_commercial_files":linked,
    "copied_commercial_files":copied,
    "generation_preserved":kick_desired.get("generation"),
    "rtmp_restart":False,
    "container_restart":False,
}
atomic(kick/"kick-dj-copy-result.json",result)
print(json.dumps(result,ensure_ascii=False,indent=2))
PY

echo
echo "=== KICK LIVE HEALTH (encoder must stay alive) ==="
cat "$KICK/health.json" 2>/dev/null || true
echo
echo "=== KICK NOW PLAYING ==="
cat "$KICK/now-playing.json" 2>/dev/null || true
echo
echo "=== BACKUP ==="
cat "$BACKUPS/LATEST.json"
