#!/usr/bin/env bash
set -euo pipefail

REPO="/home/ubuntu/theofficemusic"
CONTROL_ROOT="/opt/mediaforge-control"
LOCAL_API="http://127.0.0.1:8790"

if [ "$(id -u)" -ne 0 ]; then
  echo "Execute com sudo."
  exit 1
fi

for name in peter-lofi-twitch peter-lofi-kick peter-lofi-ovh-agent; do
  docker inspect "$name" >/dev/null 2>&1 || { echo "Container ausente: $name"; exit 1; }
done
test -s "$CONTROL_ROOT/worker.dev.vars" || { echo "Secrets locais ausentes"; exit 1; }

state_src() {
  docker inspect "$1" --format '{{range .Mounts}}{{if eq .Destination "/state"}}{{.Source}}{{end}}{{end}}'
}

TWITCH_STATE="$(state_src peter-lofi-twitch)"
KICK_STATE="$(state_src peter-lofi-kick)"
AGENT_STATE="$(state_src peter-lofi-ovh-agent 2>/dev/null || true)"
[ -n "$TWITCH_STATE" ] || { echo "Não foi possível descobrir /state da Twitch"; exit 1; }
[ "$TWITCH_STATE" = "$KICK_STATE" ] || { echo "ERRO: Twitch/Kick usam states diferentes"; exit 1; }

LIVE_STATE="$TWITCH_STATE"
LIVE_DIR="$(dirname "$LIVE_STATE")"
ENV_FILE="$LIVE_DIR/.env"

echo "=== CORRIGIR HEARTBEAT + NEXT/PREVIOUS (ZERO RTMP RESTART) ==="
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

echo "[1/7] Atualizando código..."
runuser -u ubuntu -- git -C "$REPO" fetch origin main
runuser -u ubuntu -- git -C "$REPO" reset --hard origin/main

echo "[2/7] Sincronizando URL + token do agente com a API local..."
readarray -t CREDS < <(python3 - "$CONTROL_ROOT/worker.dev.vars" <<'PY'
import json,pathlib,sys
vals={}
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    if "=" not in line or line.lstrip().startswith("#"): continue
    k,v=line.split("=",1)
    try: vals[k.strip()]=json.loads(v)
    except Exception: vals[k.strip()]=v
for k in ("OVH_AGENT_TOKEN","ADMIN_EMAIL","ADMIN_PASSWORD"):
    print(vals.get(k,""))
PY
)
AGENT_TOKEN="${CREDS[0]:-}"
ADMIN_EMAIL="${CREDS[1]:-}"
ADMIN_PASSWORD="${CREDS[2]:-}"
[ -n "$AGENT_TOKEN" ] || { echo "OVH_AGENT_TOKEN vazio"; exit 1; }
[ -n "$ADMIN_EMAIL" ] || { echo "ADMIN_EMAIL vazio"; exit 1; }
[ -n "$ADMIN_PASSWORD" ] || { echo "ADMIN_PASSWORD vazio"; exit 1; }

python3 - "$ENV_FILE" "$AGENT_TOKEN" <<'PY'
import pathlib,sys
p=pathlib.Path(sys.argv[1]); token=sys.argv[2]
updates={
 "MEDIAFORGE_API_URL":"http://host.docker.internal:8790",
 "MEDIAFORGE_AGENT_TOKEN":token,
}
lines=p.read_text().splitlines() if p.exists() else []
seen=set(); out=[]
for line in lines:
    key=line.split("=",1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
    if key in updates:
        out.append(f"{key}={updates[key]}"); seen.add(key)
    else:
        out.append(line)
for k,v in updates.items():
    if k not in seen: out.append(f"{k}={v}")
p.write_text("\n".join(out)+"\n")
PY
chmod 600 "$ENV_FILE"

echo "[3/7] Recriando SOMENTE o ovh-agent..."
cd "$LIVE_DIR"
docker compose build ovh-agent
docker compose up -d --no-deps --force-recreate ovh-agent
sleep 8

NEW_AGENT_STATE="$(state_src peter-lofi-ovh-agent 2>/dev/null || true)"
echo "State do agente após correção: ${NEW_AGENT_STATE:-desconhecido}"
[ "$NEW_AGENT_STATE" = "$LIVE_STATE" ] || { echo "ERRO: state do agente divergente"; exit 2; }

echo "[4/7] Testando autenticação do agente contra a API LOCAL..."
docker exec peter-lofi-ovh-agent python - <<'PY'
import json,os,urllib.request
base=os.environ.get("MEDIAFORGE_API_URL","").rstrip("/")
tok=os.environ.get("MEDIAFORGE_AGENT_TOKEN","")
req=urllib.request.Request(base+"/api/ovh/agent/commands?limit=1",headers={"x-ovh-agent-token":tok,"user-agent":"MediaForge-Diagnostic"})
with urllib.request.urlopen(req,timeout=10) as r:
    print("agent_api_http =",r.status)
    print("agent_api_body =",r.read().decode()[:500])
PY

sleep 5

echo "[5/7] Verificando heartbeat realmente novo..."
python3 - "$LIVE_STATE" <<'PY'
import json,pathlib,sys,datetime
p=pathlib.Path(sys.argv[1])/"agent"/"status.json"
d=json.loads(p.read_text())
raw=d.get("reported_at") or ""
dt=datetime.datetime.fromisoformat(raw.replace("Z","+00:00"))
age=(datetime.datetime.now(datetime.timezone.utc)-dt).total_seconds()
print("reported_at =",raw)
print("heartbeat_age_seconds =",round(age,1))
print("last_command =",d.get("last_command"))
print("twitch_now =",((d.get("services") or {}).get("twitch") or {}).get("now_playing"))
if age>35:
    raise SystemExit("ERRO: heartbeat local continua antigo")
PY

echo "[6/7] Teste ponta a ponta igual ao botão PRÓXIMA FAIXA..."
BEFORE_TITLE="$(python3 - "$LIVE_STATE" <<'PY'
import json,pathlib,sys
p=pathlib.Path(sys.argv[1])/"twitch"/"now-playing.json"
try: print(json.loads(p.read_text()).get("title",""))
except Exception: print("")
PY
)"
AUTH_JSON="$(python3 - "$ADMIN_EMAIL" "$ADMIN_PASSWORD" <<'PY'
import json,sys
print(json.dumps({"email":sys.argv[1],"password":sys.argv[2]}))
PY
)"
LOGIN="$(curl -fsS -X POST "$LOCAL_API/api/auth/login" -H 'content-type: application/json' --data "$AUTH_JSON")"
USER_TOKEN="$(python3 - "$LOGIN" <<'PY'
import json,sys
print(json.loads(sys.argv[1]).get("token",""))
PY
)"
[ -n "$USER_TOKEN" ] || { echo "Falha ao obter sessão local"; exit 3; }

SENT="$(curl -fsS -X POST "$LOCAL_API/api/ovh/control"   -H "authorization: Bearer $USER_TOKEN"   -H 'content-type: application/json'   --data '{"action":"skip","runtime_slot":"twitch"}')"
CMD_ID="$(python3 - "$SENT" <<'PY'
import json,sys
d=json.loads(sys.argv[1]); print(((d.get("command") or {}).get("id")) or "")
PY
)"
[ -n "$CMD_ID" ] || { echo "API não retornou command.id"; echo "$SENT"; exit 4; }
echo "Comando de teste criado: $CMD_ID"

STATUS=""
STATUS_JSON=""
for i in $(seq 1 25); do
  sleep 1
  STATUS_JSON="$(curl -fsS "$LOCAL_API/api/ovh/status" -H "authorization: Bearer $USER_TOKEN")"
  STATUS="$(python3 - "$STATUS_JSON" "$CMD_ID" <<'PY'
import json,sys
d=json.loads(sys.argv[1]); cid=sys.argv[2]
for row in d.get("recent_commands") or []:
    if str(row.get("id"))==cid:
        print(row.get("status",""))
        break
PY
)"
  [ "$STATUS" = "completed" ] && break
  [ "$STATUS" = "failed" ] && { echo "$STATUS_JSON"; exit 5; }
done
[ "$STATUS" = "completed" ] || { echo "ERRO: comando não foi consumido pelo agente (status=$STATUS)"; echo "$STATUS_JSON"; docker logs --tail 120 peter-lofi-ovh-agent 2>&1; exit 6; }

sleep 3
AFTER_TITLE="$(python3 - "$LIVE_STATE" <<'PY'
import json,pathlib,sys
p=pathlib.Path(sys.argv[1])/"twitch"/"now-playing.json"
try: print(json.loads(p.read_text()).get("title",""))
except Exception: print("")
PY
)"
echo "Twitch antes : $BEFORE_TITLE"
echo "Twitch depois: $AFTER_TITLE"
[ -n "$AFTER_TITLE" ] || { echo "ERRO: now-playing vazio"; exit 7; }
[ "$AFTER_TITLE" != "$BEFORE_TITLE" ] || { echo "ERRO: comando completou mas a música não mudou"; exit 8; }

echo "[7/7] Confirmando ZERO restart dos encoders..."
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
echo "=== PAINEL LOCAL ==="
curl -fsS "$LOCAL_API/api/ovh/public-health" | python3 -m json.tool | head -80 || true
echo
echo "MEDIAFORGE_NEXT_HEARTBEAT_FIXED"
