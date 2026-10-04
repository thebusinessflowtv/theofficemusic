#!/usr/bin/env bash
set -euo pipefail

OFFICE_REPO="/home/ubuntu/theofficemusic"
PANEL_REPO="/home/ubuntu/thebusinessflow"
STREAM_DIR="$OFFICE_REPO/ovh-streaming"
CONTROL_ROOT="/opt/mediaforge-control"
LOCAL_API="http://127.0.0.1:8790"
PUBLIC_URL="https://146.59.156.224:8443"

if [ "$(id -u)" -ne 0 ]; then
  echo "Execute com sudo."
  exit 1
fi

for bin in git docker curl python3; do
  command -v "$bin" >/dev/null 2>&1 || { echo "Dependência ausente: $bin"; exit 1; }
done

test -d "$OFFICE_REPO/.git" || { echo "Repo ausente: $OFFICE_REPO"; exit 1; }
test -d "$PANEL_REPO/.git" || { echo "Repo ausente: $PANEL_REPO"; exit 1; }
test -f "$CONTROL_ROOT/docker-compose.yml" || { echo "Control plane ainda não foi instalado em $CONTROL_ROOT"; exit 1; }
test -s "$CONTROL_ROOT/worker.dev.vars" || { echo "Secrets locais ausentes em $CONTROL_ROOT/worker.dev.vars"; exit 1; }

echo "=== MediaForge control-plane recovery (ZERO RTMP RESTART) ==="
echo "Streaming containers NÃO serão recriados nem reiniciados."

python3 - "$STREAM_DIR/state" > /tmp/mf-encoders-before-recovery.json <<'PY'
import json,pathlib,sys
root=pathlib.Path(sys.argv[1]); out={}
for slot in ("kick","twitch","youtube-deep-house","youtube-rainy"):
    try: out[slot]=json.loads((root/slot/"health.json").read_text())
    except Exception as e: out[slot]={"error":str(e)}
print(json.dumps(out))
PY

echo "[1/6] Atualizando somente código de painel e agente..."
runuser -u ubuntu -- git -C "$PANEL_REPO" fetch origin main
runuser -u ubuntu -- git -C "$PANEL_REPO" reset --hard origin/main
runuser -u ubuntu -- git -C "$OFFICE_REPO" fetch origin main
runuser -u ubuntu -- git -C "$OFFICE_REPO" reset --hard origin/main

echo "[2/6] Restaurando site local da OVH..."
rm -rf "$CONTROL_ROOT/site"/*
cp -a "$PANEL_REPO/control-center/." "$CONTROL_ROOT/site/"
cp "$CONTROL_ROOT/site/secure.html" "$CONTROL_ROOT/site/index.html"
cat > "$CONTROL_ROOT/site/mediaforge-config.js" <<'JS'
window.MEDIAFORGE_CONFIG = { API_URL: window.location.origin };
JS

echo "[3/6] Subindo SOMENTE API + web do MediaForge..."
cd "$CONTROL_ROOT"
docker compose up -d --no-deps api web

for i in $(seq 1 40); do
  if curl -fsS "$LOCAL_API/api/health" >/tmp/mf-local-health.json 2>/dev/null; then break; fi
  sleep 2
done
curl -fsS "$LOCAL_API/api/health" | tee /tmp/mf-local-health.json
echo

echo "[4/6] Fixando ovh-agent na API LOCAL e recriando SOMENTE o agente de controle..."
python3 - "$STREAM_DIR/.env" <<'PY'
import pathlib,sys
p=pathlib.Path(sys.argv[1])
lines=p.read_text().splitlines() if p.exists() else []
out=[]; seen=False
for line in lines:
    if line.startswith("MEDIAFORGE_API_URL="):
        out.append("MEDIAFORGE_API_URL=http://host.docker.internal:8790"); seen=True
    else:
        out.append(line)
if not seen: out.append("MEDIAFORGE_API_URL=http://host.docker.internal:8790")
p.write_text("\n".join(out)+"\n")
PY
cd "$STREAM_DIR"
docker compose build ovh-agent
docker compose up -d --no-deps --force-recreate ovh-agent

echo "[5/6] Mantendo deploy-agent independente da API local..."
if [ -f /etc/mediaforge-control-agent.env ]; then
  python3 - /etc/mediaforge-control-agent.env <<'PY'
import pathlib,sys
p=pathlib.Path(sys.argv[1])
lines=p.read_text().splitlines()
out=[]; seen=False
for line in lines:
    if line.startswith("MEDIAFORGE_API_URL="):
        out.append("MEDIAFORGE_API_URL=https://mediaforge-api.guilhermeodsgn.workers.dev"); seen=True
    else:
        out.append(line)
if not seen: out.append("MEDIAFORGE_API_URL=https://mediaforge-api.guilhermeodsgn.workers.dev")
p.write_text("\n".join(out)+"\n")
PY
  systemctl daemon-reload
  systemctl restart mediaforge-deploy-agent.service
fi

echo "[5/6] Aguardando heartbeat local...
sleep 12
curl -fsS "$LOCAL_API/api/ovh/public-health" | tee /tmp/mf-local-ovh-health.json
echo
curl -kfsS "$PUBLIC_URL/api/health" | tee /tmp/mf-public-health.json
echo

echo "[6/6] Confirmando que os encoders RTMP permaneceram intactos..."
python3 - /tmp/mf-encoders-before-recovery.json "$STREAM_DIR/state" <<'PY'
import json,pathlib,sys
before=json.load(open(sys.argv[1])); root=pathlib.Path(sys.argv[2]); bad=[]
for slot in ("kick","twitch","youtube-deep-house","youtube-rainy"):
    try: after=json.loads((root/slot/"health.json").read_text())
    except Exception as e:
        print(f"{slot}: erro lendo health: {e}"); bad.append(slot); continue
    bp=before.get(slot,{}).get("encoder_pid"); ap=after.get("encoder_pid")
    print(f"{slot}: status={after.get('status')} encoder_before={bp} encoder_after={ap} preserved={bp is not None and bp==ap}")
    if bp is not None and ap != bp: bad.append(slot)
if bad:
    print("ATENÇÃO: PID mudou em: "+",".join(bad))
    raise SystemExit(2)
PY

echo
echo "MEDIAFORGE_CONTROL_RECOVERED"
echo "Abra: $PUBLIC_URL/"
echo "Nenhum encoder de Kick/Twitch/YouTube foi reiniciado por este script."
