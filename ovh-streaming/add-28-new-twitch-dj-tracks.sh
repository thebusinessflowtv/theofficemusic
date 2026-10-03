#!/usr/bin/env bash
set -euo pipefail

REPO="/home/ubuntu/theofficemusic"
STREAM_DIR="$REPO/ovh-streaming"
STATE_DIR="$STREAM_DIR/state"
TWITCH_DIR="$STATE_DIR/twitch"
MANIFEST="$REPO/control/twitch-dj-upload101-allowed62.json"
OLD_CMD="$REPO/control/ovh-commands/8c115531-4648-4394-b928-c82ab353d1a4.json"
ARCHIVE="/tmp/twitch-dj-upload101.zip"

if [ "$(id -u)" -ne 0 ]; then
  echo "Execute com sudo."
  exit 1
fi

for bin in python3 curl docker; do
  command -v "$bin" >/dev/null 2>&1 || { echo "Dependência ausente: $bin"; exit 1; }
done

test -s "$MANIFEST" || { echo "Manifesto ausente: $MANIFEST"; exit 1; }
test -s "$OLD_CMD" || { echo "Comando-base ausente: $OLD_CMD"; exit 1; }
test -s "$TWITCH_DIR/health.json" || { echo "Estado Twitch ausente"; exit 1; }

echo "=== TWITCH DJ: ADICIONAR 28 NOVAS ALLOWED ==="

BEFORE_PID="$(python3 - "$TWITCH_DIR/health.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1])).get("encoder_pid",""))
PY
)"
BEFORE_TITLE="$(python3 - "$TWITCH_DIR/now-playing.json" <<'PY'
import json,sys
try: print(json.load(open(sys.argv[1])).get("title",""))
except Exception: print("")
PY
)"
echo "Encoder PID antes: $BEFORE_PID"
echo "Tocando antes: $BEFORE_TITLE"

ARCHIVE_URL="$(python3 - "$OLD_CMD" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))["archive_url"])
PY
)"

echo "[1/5] Baixando ZIP enviado..."
curl -fL --retry 5 --retry-delay 2 "$ARCHIVE_URL" -o "$ARCHIVE"
test -s "$ARCHIVE"

echo "[2/5] Validando 101 MP3s e extraindo somente as 62 allowed..."
python3 - "$ARCHIVE" "$MANIFEST" "$OLD_CMD" "$STATE_DIR" <<'PY'
import hashlib,json,pathlib,shutil,sys,uuid,zipfile,datetime

archive=pathlib.Path(sys.argv[1])
manifest=json.load(open(sys.argv[2]))
old_cmd=json.load(open(sys.argv[3]))
state=pathlib.Path(sys.argv[4])
dj_dir=state/"twitch-dj-audio"
dj_dir.mkdir(parents=True,exist_ok=True)

expected={}
for row in manifest.get("tracks") or []:
    h=str(row.get("sha256") or "").lower()
    if len(h)==64:
        expected[h]=row
if len(expected)!=62:
    raise SystemExit(f"Esperava 62 hashes allowed, encontrei {len(expected)}")

real=0
found={}
with zipfile.ZipFile(archive) as zf:
    for info in zf.infolist():
        if info.is_dir() or not info.filename.lower().endswith(".mp3"):
            continue
        base=pathlib.PurePosixPath(info.filename).name
        if base.startswith("._") or "/__MACOSX/" in ("/"+info.filename):
            continue
        real+=1
        h=hashlib.sha256()
        with zf.open(info) as src:
            while True:
                chunk=src.read(1024*1024)
                if not chunk: break
                h.update(chunk)
        digest=h.hexdigest()
        meta=expected.get(digest)
        if not meta:
            continue
        target=dj_dir/(digest+".mp3")
        if not target.exists() or target.stat().st_size<1024:
            temp=target.with_suffix(".mp3.part")
            with zf.open(info) as src, open(temp,"wb") as dst:
                shutil.copyfileobj(src,dst,1024*1024)
            temp.replace(target)
        found[digest]={
            "id":"twitch-dj-"+digest[:12],
            "title":str(meta.get("title") or pathlib.PurePosixPath(info.filename).stem),
            "artists":str(meta.get("artists") or ""),
            "url":"file:///state/twitch-dj-audio/"+target.name,
            "duration_seconds":float(meta.get("duration_seconds") or 0),
            "source":"twitch_dj_catalog_licensed_copy",
            "sha256":digest,
        }

if real!=101:
    raise SystemExit(f"Esperava 101 MP3s reais, encontrei {real}")
if len(found)!=62:
    missing=sorted(set(expected)-set(found))
    raise SystemExit(f"Esperava 62 allowed no ZIP; encontrei {len(found)}. Missing={missing}")

base=[]
seen=set()
for i,t in enumerate(old_cmd.get("base_tracks") or []):
    url=str(t.get("url") or "")
    if not url or url in seen: continue
    seen.add(url)
    base.append({
        "id":str(t.get("id") or f"twitch-dj-original-{i+1:02d}"),
        "title":str(t.get("title") or "Peter Lofi"),
        "url":url,
        "duration_seconds":float(t.get("duration_seconds") or 0),
        "source":"peter_lofi_original",
    })
if len(base)!=36:
    raise SystemExit(f"Esperava 36 autorais, encontrei {len(base)}")

tracks=base+list(found.values())
if len(tracks)!=98:
    raise SystemExit(f"Playlist final deveria ter 98 faixas, tem {len(tracks)}")

now=datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00","Z")
twitch=state/"twitch"

def atomic(path,obj):
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+"\n")
    tmp.replace(path)

atomic(twitch/"playlist.json",{
    "station":"twitch",
    "playlist_key":"twitch-dj-mixed",
    "platform_lock":["twitch"],
    "shuffle":True,
    "repeat":True,
    "updated_at":now,
    "tracks":tracks,
})

desired={}
try: desired=json.load(open(twitch/"desired.json"))
except Exception: desired={}
desired.update({
    "runtime":"ovh",
    "runtime_slot":"twitch",
    "playlist_key":"twitch-dj-mixed",
    "updated_at":now,
})
atomic(twitch/"desired.json",desired)

atomic(twitch/"dj-import.json",{
    "status":"ready",
    "updated_at":now,
    "playlist_key":"twitch-dj-mixed",
    "original_tracks":36,
    "commercial_tracks":62,
    "track_count":98,
    "new_allowed_added":28,
    "duplicates_ignored":0,
    "missing_hashes":[],
    "rejected_files":[],
    "rtmp_restart":False,
})

atomic(twitch/"command.json",{
    "id":str(uuid.uuid4()),
    "action":"skip",
    "requested_at":now,
    "source":"mediaforge-twitch-dj-add-28-direct",
})

print("real_mp3s=101")
print("allowed_commercial=62")
print("new_allowed_added=28")
print("original_tracks=36")
print("final_playlist=98")
PY

echo "[3/5] Aguardando o audio engine recarregar a playlist..."
sleep 5

echo "[4/5] Validando playlist e troca sem restart..."
python3 - "$TWITCH_DIR/playlist.json" "$TWITCH_DIR/dj-import.json" <<'PY'
import json,sys
p=json.load(open(sys.argv[1]))
d=json.load(open(sys.argv[2]))
tracks=p.get("tracks") or []
print("playlist_track_count =",len(tracks))
print("original_tracks =",d.get("original_tracks"))
print("commercial_tracks =",d.get("commercial_tracks"))
print("new_allowed_added =",d.get("new_allowed_added"))
if len(tracks)!=98 or d.get("commercial_tracks")!=62:
    raise SystemExit("Validação da playlist falhou")
PY

AFTER_PID="$(python3 - "$TWITCH_DIR/health.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1])).get("encoder_pid",""))
PY
)"
AFTER_TITLE="$(python3 - "$TWITCH_DIR/now-playing.json" <<'PY'
import json,sys
try: print(json.load(open(sys.argv[1])).get("title",""))
except Exception: print("")
PY
)"

echo "[5/5] Segurança RTMP..."
echo "Encoder PID depois: $AFTER_PID"
echo "Tocando depois: $AFTER_TITLE"
if [ -n "$BEFORE_PID" ] && [ "$BEFORE_PID" != "$AFTER_PID" ]; then
  echo "ERRO: encoder PID mudou."
  exit 2
fi

rm -f "$ARCHIVE"

echo
echo "TWITCH_DJ_28_ADDED_OK"
echo "Playlist final: 36 autorais + 62 comerciais = 98 faixas"
echo "Encoder preservado: $AFTER_PID"
