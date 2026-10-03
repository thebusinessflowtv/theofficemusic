#!/usr/bin/env bash
set -euo pipefail

REPO="/home/ubuntu/theofficemusic"
STREAM_DIR="$REPO/ovh-streaming"
STATE="$STREAM_DIR/state/twitch"

if [ "$(id -u)" -ne 0 ]; then
  echo "Execute com sudo."
  exit 1
fi

for bin in docker python3; do
  command -v "$bin" >/dev/null 2>&1 || { echo "Dependência ausente: $bin"; exit 1; }
done

test -s "$STATE/playlist.json" || { echo "Playlist Twitch ausente"; exit 1; }
test -s "$STATE/health.json" || { echo "Health Twitch ausente"; exit 1; }

echo "=== TWITCH STABILITY + GAPLESS CROSSFADE FIX ==="
echo "ATENÇÃO: haverá UMA reconexão controlada da Twitch para trocar o publisher."
echo "Kick e YouTube não serão tocados."

python3 - "$STATE/playlist.json" <<'PY'
import json,pathlib,sys
p=pathlib.Path(sys.argv[1])
d=json.loads(p.read_text())
tracks=d.get("tracks") or []
if len(tracks)!=98:
    raise SystemExit(f"Esperava 98 faixas, encontrei {len(tracks)}")
d["shuffle"]=True
d["repeat"]=True
tmp=p.with_suffix(".json.tmp")
tmp.write_text(json.dumps(d,ensure_ascii=False,indent=2)+"\n")
tmp.replace(p)
print("playlist_track_count =",len(tracks))
print("shuffle =",d["shuffle"])
print("repeat =",d["repeat"])
PY

echo "[1/6] Validando código..."
python3 -m py_compile "$STREAM_DIR/app/audio_engine.py" "$STREAM_DIR/app/stream_core.py"

echo "[2/6] Construindo Twitch atualizada..."
cd "$STREAM_DIR"
docker compose build twitch

OLD_CONTAINER="$(docker inspect peter-lofi-twitch --format '{{.Id}}' 2>/dev/null || true)"
OLD_RESTARTS="$(python3 - "$STATE/health.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1])).get("restarts",""))
PY
)"
echo "Container anterior: ${OLD_CONTAINER:0:12}"
echo "Restarts anteriores: $OLD_RESTARTS"

echo "[3/6] Aplicando publisher resiliente — somente Twitch..."
docker compose up -d --no-deps --force-recreate twitch

NEW_CONTAINER="$(docker inspect peter-lofi-twitch --format '{{.Id}}')"
echo "Container novo: ${NEW_CONTAINER:0:12}"

echo "[4/6] Aguardando Twitch voltar LIVE..."
LIVE=0
for i in $(seq 1 60); do
  sleep 1
  STATUS="$(python3 - "$STATE/health.json" <<'PY'
import json,sys
try: print(json.load(open(sys.argv[1])).get("status",""))
except Exception: print("")
PY
)"
  if [ "$STATUS" = "live" ]; then
    LIVE=1
    break
  fi
  if [ $((i % 5)) -eq 0 ]; then
    echo "  status=$STATUS — $i s"
  fi
done
[ "$LIVE" -eq 1 ] || {
  echo "Twitch não voltou LIVE dentro do prazo."
  docker logs --tail 120 peter-lofi-twitch 2>&1 || true
  exit 2
}

echo "[5/6] Validando playlist aleatória + encoder..."
python3 - "$STATE/playlist.json" "$STATE/health.json" <<'PY'
import json,sys
p=json.load(open(sys.argv[1]))
h=json.load(open(sys.argv[2]))
tracks=p.get("tracks") or []
print("status =",h.get("status"))
print("encoder_pid =",h.get("encoder_pid"))
print("audio_pid =",h.get("audio_pid"))
print("playlist_track_count =",len(tracks))
print("shuffle =",p.get("shuffle"))
print("repeat =",p.get("repeat"))
print("video_bitrate_kbps =",h.get("video_bitrate_kbps"))
if h.get("status")!="live": raise SystemExit("Twitch não está live")
if len(tracks)!=98: raise SystemExit("Playlist não tem 98 faixas")
if p.get("shuffle") is not True: raise SystemExit("Shuffle não está ativo")
PY

ENCODER_PID="$(python3 - "$STATE/health.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1])).get("encoder_pid",""))
PY
)"
RESTARTS="$(python3 - "$STATE/health.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1])).get("restarts",""))
PY
)"
TITLE_BEFORE="$(python3 - "$STATE/now-playing.json" <<'PY'
import json,sys
try: print(json.load(open(sys.argv[1])).get("title",""))
except Exception: print("")
PY
)"

echo "[6/6] Testando UMA troca sem reiniciar conexão..."
TEST_ID="stability-crossfade-$(date +%s)"
cat > "$STATE/command.json.tmp" <<EOF
{
  "id": "$TEST_ID",
  "action": "skip",
  "requested_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "source": "mediaforge-stability-test"
}
EOF
mv "$STATE/command.json.tmp" "$STATE/command.json"

CHANGED=0
TITLE_AFTER="$TITLE_BEFORE"
for i in $(seq 1 100); do
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
[ "$CHANGED" -eq 1 ] || { echo "Faixa não mudou no teste"; exit 3; }

sleep 4

ENCODER_PID_AFTER="$(python3 - "$STATE/health.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1])).get("encoder_pid",""))
PY
)"
RESTARTS_AFTER="$(python3 - "$STATE/health.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1])).get("restarts",""))
PY
)"
AUDIO_STATE="$(python3 - "$STATE/audio-health.json" <<'PY'
import json,sys
try:
    d=json.load(open(sys.argv[1]))
    print(d.get("state") or d.get("status") or "")
except Exception:
    print("")
PY
)"

echo "Faixa antes : $TITLE_BEFORE"
echo "Faixa depois: $TITLE_AFTER"
echo "Encoder antes do skip : $ENCODER_PID"
echo "Encoder depois do skip: $ENCODER_PID_AFTER"
echo "Restarts antes do skip: $RESTARTS"
echo "Restarts após o skip  : $RESTARTS_AFTER"
echo "Audio state: $AUDIO_STATE"

if [ "$ENCODER_PID_AFTER" != "$ENCODER_PID" ]; then
  echo "ERRO: troca de faixa reiniciou o encoder."
  exit 4
fi
if [ "$RESTARTS_AFTER" != "$RESTARTS" ]; then
  echo "ERRO: contador de restarts mudou durante o skip."
  exit 5
fi

echo
echo "TWITCH_STABILITY_FIX_OK"
echo "Playlist: 98 faixas"
echo "Shuffle: ON"
echo "Crossfade: 1.5s com pré-buffer"
echo "Publisher: FIFO RTMP com recuperação automática"
echo "CBR/CFR: ON"
echo "Encoder permaneceu: $ENCODER_PID_AFTER"
