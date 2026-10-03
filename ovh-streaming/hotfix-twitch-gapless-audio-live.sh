#!/usr/bin/env bash
set -euo pipefail

REPO="/home/ubuntu/theofficemusic"
STREAM_DIR="$REPO/ovh-streaming"
CONTAINER="peter-lofi-twitch"
STATE="$STREAM_DIR/state/twitch"
FIFO="$STATE/audio.pcm"

if [ "$(id -u)" -ne 0 ]; then
  echo "Execute com sudo."
  exit 1
fi

for bin in docker python3; do
  command -v "$bin" >/dev/null 2>&1 || { echo "Dependência ausente: $bin"; exit 1; }
done

test -s "$STATE/health.json" || { echo "Health Twitch ausente"; exit 1; }
test -s "$STATE/playlist.json" || { echo "Playlist Twitch ausente"; exit 1; }
test -p "$FIFO" || { echo "FIFO de áudio ausente: $FIFO"; exit 1; }

echo "=== TWITCH GAPLESS AUDIO CLOCK HOTFIX ==="
echo "Nenhum container será recriado. Nenhum encoder RTMP será reiniciado."

python3 - "$STATE/playlist.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
print("playlist_track_count =",len(d.get("tracks") or []))
print("shuffle =",d.get("shuffle"))
print("repeat =",d.get("repeat"))
if len(d.get("tracks") or []) != 98:
    raise SystemExit("ERRO: playlist não tem 98 faixas")
if d.get("shuffle") is not True:
    raise SystemExit("ERRO: shuffle precisa estar ativo")
PY

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
OLD_AUDIO_PID="$(python3 - "$STATE/health.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1])).get("audio_pid",""))
PY
)"
echo "Encoder PID: $BEFORE_ENCODER"
echo "Restarts: $BEFORE_RESTARTS"
echo "Audio PID antigo: $OLD_AUDIO_PID"

echo "[1/5] Validando e copiando novo clock de áudio..."
python3 -m py_compile "$STREAM_DIR/app/audio_engine.py"
docker cp "$STREAM_DIR/app/audio_engine.py" "$CONTAINER:/app/audio_engine.py"

echo "[2/5] Armando guarda de silêncio ANTES de trocar o feeder..."
docker exec -d "$CONTAINER" python - "$OLD_AUDIO_PID" <<'PY'
import glob,os,sys,time
old=int(sys.argv[1]) if sys.argv[1].isdigit() else -1
fifo="/state/twitch/audio.pcm"

def engine_pids():
    out=[]
    me=os.getpid()
    for p in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            pid=int(p.split("/")[2])
            if pid==me or pid==old:
                continue
            raw=open(p,"rb").read()
            parts=[x.decode("utf-8","ignore") for x in raw.split(b"\x00") if x]
            joined=" ".join(parts)
            if "/app/audio_engine.py" in joined and "--platform twitch" in joined:
                out.append(pid)
        except Exception:
            pass
    return out

# Wait until the old feeder is actually gone, then keep the FIFO clock alive
# with exact 20 ms silence frames until StreamCore starts the replacement.
deadline=time.time()+15
while time.time()<deadline and old>0 and os.path.exists(f"/proc/{old}"):
    time.sleep(0.01)

fd=os.open(fifo,os.O_WRONLY)
frame=bytes(960*2*2)
next_tick=time.monotonic()
try:
    while time.time()<deadline:
        if engine_pids():
            break
        now=time.monotonic()
        if now<next_tick:
            time.sleep(next_tick-now)
        next_tick+=0.020
        os.write(fd,frame)
finally:
    os.close(fd)
PY

if [ -n "$OLD_AUDIO_PID" ]; then
  docker exec "$CONTAINER" python -c "import os,signal; os.kill(int('$OLD_AUDIO_PID'), signal.SIGTERM)" || true
fi

NEW_AUDIO_PID=""
for i in $(seq 1 150); do
  sleep 0.1
  NEW_AUDIO_PID="$(python3 - "$STATE/health.json" <<'PY'
import json,sys
try: print(json.load(open(sys.argv[1])).get("audio_pid",""))
except Exception: print("")
PY
)"
  if [ -n "$NEW_AUDIO_PID" ] && [ "$NEW_AUDIO_PID" != "$OLD_AUDIO_PID" ]; then
    break
  fi
done
[ -n "$NEW_AUDIO_PID" ] && [ "$NEW_AUDIO_PID" != "$OLD_AUDIO_PID" ] || {
  echo "ERRO: novo audio_engine não apareceu."
  docker logs --tail 100 "$CONTAINER" 2>&1 || true
  exit 2
}
echo "Audio PID novo: $NEW_AUDIO_PID"

sleep 1

echo "[3/5] Confirmando clock contínuo de 20 ms..."
python3 - "$STATE/audio-health.json" <<'PY'
import json,sys,time
d=json.load(open(sys.argv[1]))
print("audio_state =",d.get("state") or d.get("status"))
print("clock_frame_ms =",d.get("clock_frame_ms"))
print("track_id =",d.get("track_id"))
if int(d.get("clock_frame_ms") or 0) != 20:
    raise SystemExit("ERRO: novo clock de 20 ms ainda não está ativo")
PY

echo "[4/5] Fazendo 3 trocas aleatórias sem tocar no encoder..."
for N in 1 2 3; do
  BEFORE_TITLE="$(python3 - "$STATE/now-playing.json" <<'PY'
import json,sys
try: print(json.load(open(sys.argv[1])).get("title",""))
except Exception: print("")
PY
)"
  TEST_ID="gapless-$N-$(date +%s%N)"
  cat > "$STATE/command.json.tmp" <<EOF
{"id":"$TEST_ID","action":"skip","requested_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","source":"gapless-validation"}
EOF
  mv "$STATE/command.json.tmp" "$STATE/command.json"

  CHANGED=0
  AFTER_TITLE="$BEFORE_TITLE"
  for i in $(seq 1 120); do
    sleep 0.1
    AFTER_TITLE="$(python3 - "$STATE/now-playing.json" <<'PY'
import json,sys
try: print(json.load(open(sys.argv[1])).get("title",""))
except Exception: print("")
PY
)"
    if [ -n "$AFTER_TITLE" ] && [ "$AFTER_TITLE" != "$BEFORE_TITLE" ]; then
      CHANGED=1
      break
    fi
  done
  [ "$CHANGED" -eq 1 ] || { echo "ERRO: troca $N não mudou a faixa"; exit 3; }
  echo "  $N: $BEFORE_TITLE  ->  $AFTER_TITLE"
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
  CUR_STATUS="$(python3 - "$STATE/health.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1])).get("status",""))
PY
)"
  [ "$CUR_ENCODER" = "$BEFORE_ENCODER" ] || { echo "ERRO: encoder mudou na troca $N"; exit 4; }
  [ "$CUR_RESTARTS" = "$BEFORE_RESTARTS" ] || { echo "ERRO: restart detectado na troca $N"; exit 5; }
  [ "$CUR_STATUS" = "live" ] || { echo "ERRO: status=$CUR_STATUS na troca $N"; exit 6; }
done

echo "[5/5] Estado final..."
python3 - "$STATE/health.json" "$STATE/audio-health.json" <<'PY'
import json,sys
h=json.load(open(sys.argv[1])); a=json.load(open(sys.argv[2]))
print("status =",h.get("status"))
print("encoder_pid =",h.get("encoder_pid"))
print("restarts =",h.get("restarts"))
print("audio_pid =",h.get("audio_pid"))
print("audio_state =",a.get("state") or a.get("status"))
print("clock_frame_ms =",a.get("clock_frame_ms"))
print("crossfade_seconds =",a.get("crossfade_seconds"))
print("prebuffer_seconds =",a.get("prebuffer_seconds"))
print("audio_stalls =",h.get("audio_stalls"))
PY

echo
echo "TWITCH_GAPLESS_AUDIO_OK"
echo "Encoder preservado: $BEFORE_ENCODER"
echo "Restarts preservados: $BEFORE_RESTARTS"
echo "Shuffle: ON"
echo "PCM clock: 20 ms contínuo"
echo "Crossfade: 1.5 s"
echo "Nenhum container Twitch foi recriado."
