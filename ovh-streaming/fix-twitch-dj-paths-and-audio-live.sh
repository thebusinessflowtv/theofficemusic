#!/usr/bin/env bash
set -euo pipefail

REPO="/home/ubuntu/theofficemusic"
STREAM_DIR="$REPO/ovh-streaming"
CONTAINER="peter-lofi-twitch"
STATE="$STREAM_DIR/state/twitch"
PLAYLIST="$STATE/playlist.json"

if [ "$(id -u)" -ne 0 ]; then
  echo "Execute com sudo."
  exit 1
fi

for bin in docker python3; do
  command -v "$bin" >/dev/null 2>&1 || { echo "Dependência ausente: $bin"; exit 1; }
done

test -s "$PLAYLIST" || { echo "Playlist Twitch ausente"; exit 1; }
test -s "$STATE/health.json" || { echo "Health Twitch ausente"; exit 1; }

echo "=== FIX DEFINITIVO DOS CAMINHOS DJ + AUDIO ENGINE ==="
echo "Nenhum container Twitch será recriado. O encoder RTMP não será reiniciado."

BEFORE_ENCODER="$(python3 - "$STATE/health.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1])).get("encoder_pid",""))
PY
)"
BEFORE_RESTARTS="$(python3 - "$STATE/health.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1])).get("restarts",""))
PY
)"
echo "Encoder PID antes: $BEFORE_ENCODER"
echo "Restarts antes: $BEFORE_RESTARTS"

echo "[1/6] Corrigindo URLs file:// da playlist para caminhos internos do container..."
cp "$PLAYLIST" "$PLAYLIST.bak.$(date +%s)"
python3 - "$PLAYLIST" <<'PY'
import json,pathlib,sys
p=pathlib.Path(sys.argv[1])
d=json.loads(p.read_text())
tracks=d.get("tracks") or []
fixed=0
local=0
for t in tracks:
    url=str(t.get("url") or "")
    if not url.startswith("file://"):
        continue
    local+=1
    raw=url[7:]
    marker="/ovh-streaming/state/"
    if marker in raw:
        suffix=raw.split(marker,1)[1]
        t["url"]="file:///state/"+suffix.lstrip("/")
        fixed+=1
d["shuffle"]=True
d["repeat"]=True
tmp=p.with_suffix(".json.tmp")
tmp.write_text(json.dumps(d,ensure_ascii=False,indent=2)+"\n")
tmp.replace(p)
print("tracks_total =",len(tracks))
print("local_tracks =",local)
print("urls_corrigidas =",fixed)
print("shuffle =",d.get("shuffle"))
print("repeat =",d.get("repeat"))
if len(tracks)!=98:
    raise SystemExit(f"ERRO: esperava 98 faixas, encontrei {len(tracks)}")
if local!=62:
    raise SystemExit(f"ERRO: esperava 62 faixas locais, encontrei {local}")
PY

echo "[2/6] Confirmando que TODAS as 62 comerciais existem dentro do container..."
docker exec "$CONTAINER" python - <<'PY'
import json,pathlib,urllib.parse
d=json.load(open("/state/twitch/playlist.json"))
local=[]
missing=[]
for t in d.get("tracks") or []:
    url=str(t.get("url") or "")
    if not url.startswith("file://"):
        continue
    path=pathlib.Path(urllib.parse.unquote(urllib.parse.urlparse(url).path))
    local.append((t.get("title"),str(path)))
    if not path.exists() or path.stat().st_size < 4096:
        missing.append((t.get("title"),str(path)))
print("local_tracks_checked =",len(local))
print("missing_local_tracks =",len(missing))
if missing:
    for row in missing[:20]:
        print("MISSING:",row[0],"->",row[1])
    raise SystemExit(2)
if len(local)!=62:
    raise SystemExit(f"ERRO: esperava 62 locais, encontrei {len(local)}")
PY

echo "[3/6] Instalando motor de áudio tolerante a faixa inválida..."
python3 -m py_compile "$STREAM_DIR/app/audio_engine.py"
docker cp "$STREAM_DIR/app/audio_engine.py" "$CONTAINER:/app/audio_engine.py"

container_audio_pids() {
  docker exec "$CONTAINER" python - <<'PY'
import glob,os
hits=[]
me=os.getpid()
for f in glob.glob("/proc/[0-9]*/cmdline"):
    try:
        pid=int(f.split("/")[2])
        if pid==me:
            continue
        raw=open(f,"rb").read()
        parts=[x.decode("utf-8","ignore") for x in raw.split(b"\x00") if x]
        cmd=" ".join(parts)
        if "/app/audio_engine.py" in cmd and "--platform twitch" in cmd:
            hits.append(pid)
    except Exception:
        pass
print(" ".join(str(x) for x in sorted(hits)))
PY
}

OLD_AUDIO_PIDS="$(container_audio_pids)"
echo "Audio PID(s) atual(is): ${OLD_AUDIO_PIDS:-nenhum}"

echo "[4/6] Trocando SOMENTE o feeder de áudio com PCM de guarda..."
# Start a watchdog that waits for the old feeder to disappear and then supplies
# exact 20 ms silence frames until the replacement feeder is alive.
docker exec -d "$CONTAINER" python - <<'PY'
import glob,os,time
fifo="/state/twitch/audio.pcm"
start=time.time()

def audio_pids():
    out=[]
    me=os.getpid()
    for f in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            pid=int(f.split("/")[2])
            if pid==me:
                continue
            raw=open(f,"rb").read()
            parts=[x.decode("utf-8","ignore") for x in raw.split(b"\x00") if x]
            cmd=" ".join(parts)
            if "/app/audio_engine.py" in cmd and "--platform twitch" in cmd:
                out.append(pid)
        except Exception:
            pass
    return out

initial=set(audio_pids())
while time.time()-start < 8 and any(os.path.exists(f"/proc/{p}") for p in initial):
    time.sleep(0.01)

fd=os.open(fifo,os.O_WRONLY)
frame=bytes(960*2*2)
next_tick=time.monotonic()
try:
    while time.time()-start < 15:
        current=set(audio_pids())
        if current and not current.intersection(initial):
            break
        now=time.monotonic()
        if now < next_tick:
            time.sleep(next_tick-now)
        else:
            next_tick=now
        next_tick += 0.020
        os.write(fd,frame)
finally:
    os.close(fd)
PY

for PID in $OLD_AUDIO_PIDS; do
  docker exec "$CONTAINER" python -c "import os,signal; os.kill(int('$PID'), signal.SIGTERM)" 2>/dev/null || true
done

NEW_AUDIO_PIDS=""
for i in $(seq 1 150); do
  sleep 0.1
  NEW_AUDIO_PIDS="$(container_audio_pids)"
  if [ -n "$NEW_AUDIO_PIDS" ] && [ "$NEW_AUDIO_PIDS" != "$OLD_AUDIO_PIDS" ]; then
    break
  fi
done
[ -n "$NEW_AUDIO_PIDS" ] || {
  echo "ERRO: novo audio_engine não iniciou."
  docker logs --tail 120 "$CONTAINER" 2>&1 || true
  exit 3
}
echo "Novo(s) Audio PID(s): $NEW_AUDIO_PIDS"

sleep 2

python3 - "$STATE/audio-health.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
print("audio_state =",d.get("state") or d.get("status"))
print("clock_frame_ms =",d.get("clock_frame_ms"))
print("track_id =",d.get("track_id"))
state=d.get("state") or d.get("status")
if state in ("stopped",None):
    raise SystemExit("ERRO: audio engine está parado")
if int(d.get("clock_frame_ms") or 0)!=20:
    raise SystemExit("ERRO: clock contínuo de 20 ms não ficou ativo")
PY

echo "[5/6] Testando 5 skips aleatórios sem restart do encoder..."
for N in 1 2 3 4 5; do
  TITLE_BEFORE="$(python3 - "$STATE/now-playing.json" <<'PY'
import json,sys
try: print(json.load(open(sys.argv[1])).get("title",""))
except Exception: print("")
PY
)"
  ID="dj-path-test-$N-$(date +%s%N)"
  cat > "$STATE/command.json.tmp" <<EOF
{"id":"$ID","action":"skip","requested_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","source":"dj-path-stability-test"}
EOF
  mv "$STATE/command.json.tmp" "$STATE/command.json"

  CHANGED=0
  TITLE_AFTER="$TITLE_BEFORE"
  for i in $(seq 1 150); do
    sleep 0.1
    TITLE_AFTER="$(python3 - "$STATE/now-playing.json" <<'PY'
import json,sys
try: print(json.load(open(sys.argv[1])).get("title",""))
except Exception: print("")
PY
)"
    if [ -n "$TITLE_AFTER" ] && [ "$TITLE_AFTER" != "$TITLE_BEFORE" ]; then
      CHANGED=1
      break
    fi
  done
  [ "$CHANGED" -eq 1 ] || { echo "ERRO: skip $N não mudou a faixa"; exit 4; }

  sleep 2
  CUR_ENCODER="$(python3 - "$STATE/health.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1])).get("encoder_pid",""))
PY
)"
  CUR_RESTARTS="$(python3 - "$STATE/health.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1])).get("restarts",""))
PY
)"
  AUDIO_STATE="$(python3 - "$STATE/audio-health.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
print(d.get("state") or d.get("status") or "")
PY
)"
  echo "  $N: $TITLE_BEFORE -> $TITLE_AFTER | encoder=$CUR_ENCODER restarts=$CUR_RESTARTS audio=$AUDIO_STATE"

  [ "$CUR_ENCODER" = "$BEFORE_ENCODER" ] || { echo "ERRO: encoder mudou no skip $N"; exit 5; }
  [ "$CUR_RESTARTS" = "$BEFORE_RESTARTS" ] || { echo "ERRO: restart no skip $N"; exit 6; }
  [ "$AUDIO_STATE" != "stopped" ] || { echo "ERRO: audio morreu no skip $N"; exit 7; }
done

echo "[6/6] Resultado final..."
python3 - "$STATE/health.json" "$STATE/audio-health.json" "$PLAYLIST" <<'PY'
import json,sys
h=json.load(open(sys.argv[1]))
a=json.load(open(sys.argv[2]))
p=json.load(open(sys.argv[3]))
print("status =",h.get("status"))
print("encoder_pid =",h.get("encoder_pid"))
print("restarts =",h.get("restarts"))
print("audio_pid =",h.get("audio_pid"))
print("audio_state =",a.get("state") or a.get("status"))
print("clock_frame_ms =",a.get("clock_frame_ms"))
print("shuffle =",p.get("shuffle"))
print("playlist_track_count =",len(p.get("tracks") or []))
PY

echo
echo "TWITCH_DJ_PATH_AND_AUDIO_FIXED"
echo "98 faixas ativas"
echo "62 comerciais acessíveis dentro do container"
echo "Shuffle aleatório: ON"
echo "Encoder preservado: $BEFORE_ENCODER"
echo "Restarts preservados: $BEFORE_RESTARTS"
echo "Nenhum container Twitch foi recriado."
