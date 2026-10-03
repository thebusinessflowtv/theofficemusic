#!/usr/bin/env bash
set -euo pipefail

REPO="/home/ubuntu/theofficemusic"

if [ "$(id -u)" -ne 0 ]; then
  echo "Execute com sudo."
  exit 1
fi

for name in peter-lofi-twitch peter-lofi-kick; do
  docker inspect "$name" >/dev/null 2>&1 || { echo "Container ausente: $name"; exit 1; }
done

state_src() {
  docker inspect "$1" --format '{{range .Mounts}}{{if eq .Destination "/state"}}{{.Source}}{{end}}{{end}}'
}

TWITCH_STATE="$(state_src peter-lofi-twitch)"
KICK_STATE="$(state_src peter-lofi-kick)"
AGENT_STATE="$(state_src peter-lofi-ovh-agent 2>/dev/null || true)"

[ -n "$TWITCH_STATE" ] || { echo "Não foi possível descobrir /state da Twitch"; exit 1; }
[ "$TWITCH_STATE" = "$KICK_STATE" ] || {
  echo "ERRO: Twitch e Kick usam states diferentes:"
  echo "twitch=$TWITCH_STATE"
  echo "kick=$KICK_STATE"
  exit 1
}

LIVE_STATE="$TWITCH_STATE"
LIVE_DIR="$(dirname "$LIVE_STATE")"

echo "=== FIX MEDIAFORGE AGENT -> STATE REAL DAS LIVES ==="
echo "State real das lives: $LIVE_STATE"
echo "State atual do agente: ${AGENT_STATE:-desconhecido}"

python3 - "$LIVE_STATE" > /tmp/mf-live-pids-before-agent-fix.json <<'PY'
import json,pathlib,sys
root=pathlib.Path(sys.argv[1]); out={}
for slot in ("kick","twitch","youtube-deep-house","youtube-rainy"):
    try: out[slot]=json.loads((root/slot/"health.json").read_text())
    except Exception as e: out[slot]={"error":str(e)}
print(json.dumps(out))
PY

echo "[1/5] Atualizando apenas código do agente..."
runuser -u ubuntu -- git -C "$REPO" fetch origin main
runuser -u ubuntu -- git -C "$REPO" reset --hard origin/main

test -d "$LIVE_DIR/app" || { echo "Diretório live inválido: $LIVE_DIR"; exit 1; }
SRC_AGENT="$REPO/ovh-streaming/app/ovh_agent.py"
DST_AGENT="$LIVE_DIR/app/ovh_agent.py"
if [ "$(readlink -f "$SRC_AGENT")" != "$(readlink -f "$DST_AGENT")" ]; then
  cp "$SRC_AGENT" "$DST_AGENT"
else
  echo "Agente já está no diretório de produção; cópia desnecessária."
fi

echo "[2/5] Garantindo que o agente fale com a API local da OVH..."
ENV_FILE="$LIVE_DIR/.env"
touch "$ENV_FILE"
if grep -q '^MEDIAFORGE_API_URL=' "$ENV_FILE"; then
  sed -i 's#^MEDIAFORGE_API_URL=.*#MEDIAFORGE_API_URL=http://host.docker.internal:8790#' "$ENV_FILE"
else
  printf '\nMEDIAFORGE_API_URL=http://host.docker.internal:8790\n' >> "$ENV_FILE"
fi

echo "[3/5] Recriando SOMENTE peter-lofi-ovh-agent..."
cd "$LIVE_DIR"
docker compose build ovh-agent
docker compose up -d --no-deps --force-recreate ovh-agent

sleep 12

NEW_AGENT_STATE="$(state_src peter-lofi-ovh-agent 2>/dev/null || true)"
echo "State do agente após correção: ${NEW_AGENT_STATE:-desconhecido}"
[ "$NEW_AGENT_STATE" = "$LIVE_STATE" ] || {
  echo "ERRO: agente ainda não está montado no state real."
  exit 2
}

echo "[4/5] Verificando logs + heartbeat novo..."
echo "--- últimos logs do agente ---"
docker logs --tail 80 peter-lofi-ovh-agent 2>&1 || true
echo "--- status local ---"
python3 - "$LIVE_STATE" <<'PY'
import json,pathlib,sys
p=pathlib.Path(sys.argv[1])/"agent"/"status.json"
d=json.loads(p.read_text())
print("reported_at =",d.get("reported_at"))
print("last_command =",d.get("last_command"))
print("twitch_now =",((d.get("services") or {}).get("twitch") or {}).get("now_playing"))
print("twitch_updated_at =",((d.get("services") or {}).get("twitch") or {}).get("updated_at"))
PY

echo "[5/5] Confirmando que nenhum encoder RTMP reiniciou..."
python3 - /tmp/mf-live-pids-before-agent-fix.json "$LIVE_STATE" <<'PY'
import json,pathlib,sys
before=json.load(open(sys.argv[1])); root=pathlib.Path(sys.argv[2]); bad=[]
for slot in ("kick","twitch","youtube-deep-house","youtube-rainy"):
    try: after=json.loads((root/slot/"health.json").read_text())
    except Exception as e:
        print(f"{slot}: erro={e}"); bad.append(slot); continue
    bp=before.get(slot,{}).get("encoder_pid"); ap=after.get("encoder_pid")
    print(f"{slot}: status={after.get('status')} encoder_before={bp} encoder_after={ap} preserved={bp is not None and bp==ap}")
    if bp is not None and ap != bp: bad.append(slot)
if bad:
    raise SystemExit("ATENÇÃO: encoder PID mudou em "+",".join(bad))
PY

echo
echo "MEDIAFORGE_AGENT_STATE_FIXED"
echo "Atualize a página OVH VPS e teste Próxima faixa."
