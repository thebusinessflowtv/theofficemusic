#!/usr/bin/env bash
set -euo pipefail

REPO="/home/ubuntu/theofficemusic"
STREAM_DIR="$REPO/ovh-streaming"
CONTAINER="peter-lofi-twitch"
STATE="$STREAM_DIR/state/twitch"

if [ "$(id -u)" -ne 0 ]; then
  echo "Execute com sudo."
  exit 1
fi

for bin in docker python3; do
  command -v "$bin" >/dev/null 2>&1 || { echo "Dependência ausente: $bin"; exit 1; }
done

test -f "$REPO/ovh-streaming/app/audio_engine.py" || { echo "audio_engine.py ausente"; exit 1; }
test -s "$STATE/health.json" || { echo "health Twitch ausente"; exit 1; }

echo "=== HOTFIX TWITCH AUDIO CROSSFADE — SEM RESTART DO ENCODER ==="

BEFORE_PID="$(python3 - "$STATE/health.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1])).get("encoder_pid",""))
PY
)"
BEFORE_RESTARTS="$(python3 - "$STATE/health.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1])).get("restarts",""))
PY
)"
BEFORE_TITLE="$(python3 - "$STATE/now-playing.json" <<'PY'
import json,sys
try: print(json.load(open(sys.argv[1])).get("title",""))
except Exception: print("")
PY
)"
echo "Encoder PID antes: $BEFORE_PID"
echo "Restarts antes: $BEFORE_RESTARTS"
echo "Faixa antes: $BEFORE_TITLE"

echo "[1/6] Validando Python..."
python3 -m py_compile "$REPO/ovh-streaming/app/audio_engine.py"

echo "[2/6] Construindo imagem atualizada para persistir após futuros restarts..."
cd "$STREAM_DIR"
docker compose build twitch >/tmp/mediaforge-build-twitch.log 2>&1 || {
  tail -100 /tmp/mediaforge-build-twitch.log
  exit 1
}

echo "[3/6] Copiando engine novo para o container ATUAL sem tocar no encoder..."
docker cp "$REPO/ovh-streaming/app/audio_engine.py" "$CONTAINER:/app/audio_engine.py"

OLD_AUDIO_PID="$(docker exec "$CONTAINER" sh -lc "pgrep -f '/app/audio_engine.py' | head -1 || true")"
echo "Audio PID antigo: ${OLD_AUDIO_PID:-não encontrado}"

echo "[4/6] Reiniciando SOMENTE o feeder de áudio com guarda de silêncio..."
if [ -n "${OLD_AUDIO_PID:-}" ]; then
  docker exec "$CONTAINER" sh -lc "kill -TERM '$OLD_AUDIO_PID' || true"
fi

# Guard: keep PCM flowing while StreamCore notices the old feeder exited and
# starts the new one. This prevents the persistent RTMP muxer from starving.
docker exec -d "$CONTAINER" python -c '
import glob,os,time
fifo="/state/twitch/audio.pcm"
def audio_pids():
    out=[]
    for p in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            cmd=open(p,"rb").read().replace(b"\x00",b" ").decode("utf-8","ignore")
            if "/app/audio_engine.py" in cmd:
                out.append(int(p.split("/")[2]))
        except Exception:
            pass
    return out
fd=os.open(fifo,os.O_WRONLY)
chunk=bytes(int(48000*0.02)*2*2)
deadline=time.time()+8
while time.time()<deadline:
    if audio_pids():
        break
    try: os.write(fd,chunk)
    except BrokenPipeError: break
os.close(fd)
'

NEW_AUDIO_PID=""
for i in $(seq 1 50); do
  NEW_AUDIO_PID="$(docker exec "$CONTAINER" sh -lc "pgrep -f '/app/audio_engine.py' | head -1 || true")"
  if [ -n "$NEW_AUDIO_PID" ] && [ "$NEW_AUDIO_PID" != "${OLD_AUDIO_PID:-}" ]; then
    break
  fi
  sleep 0.1
done
[ -n "$NEW_AUDIO_PID" ] || { echo "Novo audio_engine não iniciou"; exit 2; }
echo "Audio PID novo: $NEW_AUDIO_PID"

sleep 2

echo "[5/6] Testando UMA troca com crossfade..."
TEST_ID="crossfade-test-$(date +%s)"
cat > "$STATE/command.json.tmp" <<EOF
{
  "id": "$TEST_ID",
  "action": "skip",
  "requested_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "source": "mediaforge-crossfade-hotfix-test"
}
EOF
mv "$STATE/command.json.tmp" "$STATE/command.json"

CHANGED=0
AFTER_TITLE="$BEFORE_TITLE"
for i in $(seq 1 60); do
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
[ "$CHANGED" -eq 1 ] || { echo "A faixa não mudou no teste"; exit 3; }
echo "Faixa depois: $AFTER_TITLE"

echo "[6/6] Confirmando que o encoder RTMP ficou contínuo..."
AFTER_PID="$(python3 - "$STATE/health.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1])).get("encoder_pid",""))
PY
)"
AFTER_RESTARTS="$(python3 - "$STATE/health.json" <<'PY'
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
echo "Encoder PID depois: $AFTER_PID"
echo "Restarts depois: $AFTER_RESTARTS"
echo "Audio state: $AUDIO_STATE"

if [ -n "$BEFORE_PID" ] && [ "$AFTER_PID" != "$BEFORE_PID" ]; then
  echo "ERRO: encoder RTMP mudou de PID."
  exit 4
fi
if [ -n "$BEFORE_RESTARTS" ] && [ "$AFTER_RESTARTS" != "$BEFORE_RESTARTS" ]; then
  echo "ERRO: contador de restarts mudou."
  exit 5
fi

echo
echo "TWITCH_CROSSFADE_HOTFIX_OK"
echo "Encoder preservado: $AFTER_PID"
echo "Crossfade: 1.5s"
echo "Próxima/Anterior agora pré-carregam a faixa seguinte antes de soltar a atual."
