#!/usr/bin/env bash
set -euo pipefail

OFFICE_REPO="/home/ubuntu/theofficemusic"
PANEL_REPO="/home/ubuntu/thebusinessflow"
STREAM_DIR="$OFFICE_REPO/ovh-streaming"
CONTROL_ROOT="/opt/mediaforge-control"
WORKER_DIR="$PANEL_REPO/cloudflare/mediaforge-worker"
SECRETS_FILE="$CONTROL_ROOT/worker.dev.vars"
OPS_ENV="$CONTROL_ROOT/ops.env"
LOCAL_API="http://127.0.0.1:8790"
INTERNAL_API="http://host.docker.internal:8790"
PUBLIC_BASE="https://146.59.156.224:8443"
OLD_API="https://mediaforge-api.guilhermeodsgn.workers.dev"
MUSIC_VENV="/opt/mediaforge-music-agent/venv"
VENDOR_ROOT="/opt/mediaforge-vendor/stable-audio-3"
MIN_FREE_KB=$((11*1024*1024))

if [ "$(id -u)" -ne 0 ]; then
  echo "Execute este instalador com sudo."
  exit 1
fi

for bin in git docker curl python3 openssl systemctl; do
  command -v "$bin" >/dev/null 2>&1 || { echo "Dependência ausente: $bin"; exit 1; }
done

echo "=== MediaForge — finalizar runtime 100% OVH ==="
echo "Esta etapa NÃO recria Kick, Twitch, YouTube Deep House ou YouTube Rainy."
echo "Ela move OAuth/orquestração, biblioteca de áudio e dependências de runtime para a OVH."
echo

mkdir -p "$CONTROL_ROOT" /opt/mediaforge-music-agent /opt/mediaforge-vendor
chmod 700 "$CONTROL_ROOT"

echo "[0/11] Capturando PIDs atuais dos encoders..."
python3 - "$STREAM_DIR/state" > /tmp/mediaforge-encoders-before-final.json <<'PY'
import json,pathlib,sys
root=pathlib.Path(sys.argv[1])
out={}
for slot in ("kick","twitch","youtube-deep-house","youtube-rainy"):
    try: out[slot]=json.loads((root/slot/"health.json").read_text())
    except Exception as exc: out[slot]={"status":"unknown","error":str(exc)}
print(json.dumps(out,indent=2))
PY
cat /tmp/mediaforge-encoders-before-final.json

FREE_KB="$(df -Pk "$CONTROL_ROOT" | awk 'NR==2{print $4}')"
if [ "${FREE_KB:-0}" -lt "$MIN_FREE_KB" ]; then
  python3 - "$FREE_KB" <<'PY'
import sys
free=int(sys.argv[1] or 0)*1024
print(f"ESPAÇO INSUFICIENTE: {free/1024**3:.2f} GiB livres.")
print("A biblioteca ativa ocupa cerca de 8.5 GiB; exijo pelo menos 11 GiB livres para a migração segura.")
PY
  exit 1
fi

echo
echo "[1/11] Sincronizando uma última vez o código de migração..."
runuser -u ubuntu -- git -C "$PANEL_REPO" fetch origin main
runuser -u ubuntu -- git -C "$PANEL_REPO" reset --hard origin/main
runuser -u ubuntu -- git -C "$OFFICE_REPO" fetch origin main
runuser -u ubuntu -- git -C "$OFFICE_REPO" reset --hard origin/main

test -f "$WORKER_DIR/src/index.js"
test -f "$STREAM_DIR/music_agent.py"
test -f "$STREAM_DIR/localize-mediaforge-library.py"
test -f "$OFFICE_REPO/kaggle/runner_small_ovh.py"
test -f "$OFFICE_REPO/scripts/bootstrap_kaggle_ovh.sh"
test -s "$SECRETS_FILE"

echo
echo "IMPORTANTE: secrets do GitHub são write-only e não podem ser lidos de volta."
echo "Use credenciais NOVAS/ROTACIONADAS aqui. Nada será exibido na tela depois de salvo."
echo
read -r -p "YouTube OAuth WEB Client ID: " YOUTUBE_CLIENT_ID
read -r -s -p "YouTube OAuth WEB Client Secret: " YOUTUBE_CLIENT_SECRET
echo
read -r -s -p "YouTube Refresh Token NOVO: " YOUTUBE_REFRESH_TOKEN
echo
read -r -p "Kaggle username: " KAGGLE_USERNAME
read -r -s -p "Kaggle API token NOVO: " KAGGLE_API_TOKEN
echo
read -r -s -p "Hugging Face token NOVO: " HF_TOKEN
echo

for pair in   "YOUTUBE_CLIENT_ID:$YOUTUBE_CLIENT_ID"   "YOUTUBE_CLIENT_SECRET:$YOUTUBE_CLIENT_SECRET"   "YOUTUBE_REFRESH_TOKEN:$YOUTUBE_REFRESH_TOKEN"   "KAGGLE_USERNAME:$KAGGLE_USERNAME"   "KAGGLE_API_TOKEN:$KAGGLE_API_TOKEN"   "HF_TOKEN:$HF_TOKEN"; do
  key="${pair%%:*}"; value="${pair#*:}"
  [ -n "$value" ] || { echo "$key não pode ficar vazio."; exit 1; }
done

echo
echo "[2/11] Validando OAuth do YouTube diretamente a partir da OVH..."
YOUTUBE_CLIENT_ID="$YOUTUBE_CLIENT_ID" YOUTUBE_CLIENT_SECRET="$YOUTUBE_CLIENT_SECRET" YOUTUBE_REFRESH_TOKEN="$YOUTUBE_REFRESH_TOKEN" python3 - > /tmp/mediaforge-youtube-account.json <<'PY'
import json,os,urllib.parse,urllib.request
form=urllib.parse.urlencode({
  "client_id":os.environ["YOUTUBE_CLIENT_ID"],
  "client_secret":os.environ["YOUTUBE_CLIENT_SECRET"],
  "refresh_token":os.environ["YOUTUBE_REFRESH_TOKEN"],
  "grant_type":"refresh_token",
}).encode()
req=urllib.request.Request("https://oauth2.googleapis.com/token",data=form,method="POST",headers={"content-type":"application/x-www-form-urlencoded"})
try:
    with urllib.request.urlopen(req,timeout=30) as r: token=json.load(r)
except Exception as exc:
    raise SystemExit("OAuth YouTube inválido. Gere o refresh token com o MESMO Web OAuth client. "+str(exc))
access=token.get("access_token")
if not access: raise SystemExit("Google não retornou access_token.")
url="https://www.googleapis.com/youtube/v3/channels?part=id%2Csnippet&mine=true"
req=urllib.request.Request(url,headers={"Authorization":"Bearer "+access})
with urllib.request.urlopen(req,timeout=30) as r: data=json.load(r)
items=data.get("items") or []
if not items: raise SystemExit("OAuth válido, mas nenhum canal YouTube foi retornado para esta conta.")
row=items[0]
print(json.dumps({"channel_id":row.get("id"),"channel_title":(row.get("snippet") or {}).get("title")},ensure_ascii=False))
PY
python3 - <<'PY'
import json
d=json.load(open("/tmp/mediaforge-youtube-account.json"))
print("YouTube OK:",d.get("channel_title"),"·",d.get("channel_id"))
PY
YOUTUBE_CHANNEL_ID="$(python3 - <<'PY'
import json
print(json.load(open("/tmp/mediaforge-youtube-account.json"))["channel_id"])
PY
)"

echo
echo "[3/11] Validando Kaggle e Hugging Face..."
apt-get update -y >/dev/null
apt-get install -y --no-install-recommends python3-venv ffmpeg ca-certificates >/dev/null
python3 -m venv "$MUSIC_VENV"
"$MUSIC_VENV/bin/pip" install -q --upgrade pip kaggle
KAGGLE_USERNAME="$KAGGLE_USERNAME" KAGGLE_API_TOKEN="$KAGGLE_API_TOKEN"   "$MUSIC_VENV/bin/kaggle" datasets list --page-size 1 >/tmp/mediaforge-kaggle-check.txt
curl -fsS -H "Authorization: Bearer $HF_TOKEN"   https://huggingface.co/api/whoami-v2 >/tmp/mediaforge-hf-check.json
echo "Kaggle OK · Hugging Face OK"

AGENT_TOKEN="$(python3 - "$SECRETS_FILE" <<'PY'
import json,pathlib,sys
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    if line.startswith("OVH_AGENT_TOKEN="):
        raw=line.split("=",1)[1]
        try: print(json.loads(raw))
        except Exception: print(raw)
        break
PY
)"
[ -n "$AGENT_TOKEN" ] || { echo "OVH_AGENT_TOKEN não encontrado em $SECRETS_FILE"; exit 1; }

echo
echo "[4/11] Gravando credenciais operacionais SOMENTE na OVH..."
YOUTUBE_CLIENT_ID="$YOUTUBE_CLIENT_ID" YOUTUBE_CLIENT_SECRET="$YOUTUBE_CLIENT_SECRET" YOUTUBE_REFRESH_TOKEN="$YOUTUBE_REFRESH_TOKEN" YOUTUBE_CHANNEL_ID="$YOUTUBE_CHANNEL_ID" PUBLIC_BASE="$PUBLIC_BASE" INTERNAL_API="$INTERNAL_API" python3 - "$SECRETS_FILE" <<'PY'
import json,os,pathlib,sys
p=pathlib.Path(sys.argv[1])
updates={
 "YOUTUBE_CLIENT_ID":os.environ["YOUTUBE_CLIENT_ID"],
 "YOUTUBE_CLIENT_SECRET":os.environ["YOUTUBE_CLIENT_SECRET"],
 "YOUTUBE_REFRESH_TOKEN":os.environ["YOUTUBE_REFRESH_TOKEN"],
 "YOUTUBE_CHANNEL_ID":os.environ["YOUTUBE_CHANNEL_ID"],
 "PUBLIC_BASE_URL":os.environ["PUBLIC_BASE"],
 "OVH_INTERNAL_API_URL":os.environ["INTERNAL_API"],
}
rows={};order=[]
for line in p.read_text().splitlines():
    if "=" not in line or line.lstrip().startswith("#"): continue
    k,v=line.split("=",1);k=k.strip()
    try: rows[k]=json.loads(v)
    except Exception: rows[k]=v
    order.append(k)
rows.update(updates)
for k in updates:
    if k not in order: order.append(k)
p.write_text("
".join(f"{k}={json.dumps(rows[k])}" for k in order)+"
",encoding="utf-8")
PY
chmod 600 "$SECRETS_FILE"

KAGGLE_USERNAME="$KAGGLE_USERNAME" KAGGLE_API_TOKEN="$KAGGLE_API_TOKEN" HF_TOKEN="$HF_TOKEN" AGENT_TOKEN="$AGENT_TOKEN" python3 - "$OPS_ENV" <<'PY'
import os,pathlib,shlex,sys
p=pathlib.Path(sys.argv[1])
vals={
 "MEDIAFORGE_API_URL":"http://127.0.0.1:8790",
 "MEDIAFORGE_AGENT_TOKEN":os.environ["AGENT_TOKEN"],
 "KAGGLE_USERNAME":os.environ["KAGGLE_USERNAME"],
 "KAGGLE_API_TOKEN":os.environ["KAGGLE_API_TOKEN"],
 "HF_TOKEN":os.environ["HF_TOKEN"],
 "MEDIAFORGE_REPO":"/home/ubuntu/theofficemusic",
 "MEDIAFORGE_STABLE_AUDIO_VENDOR":"/opt/mediaforge-vendor/stable-audio-3",
 "MEDIAFORGE_MUSIC_POLL_SECONDS":"15",
}
p.write_text("
".join(f"{k}={shlex.quote(v)}" for k,v in vals.items())+"
",encoding="utf-8")
PY
chmod 600 "$OPS_ENV"
unset YOUTUBE_CLIENT_SECRET YOUTUBE_REFRESH_TOKEN KAGGLE_API_TOKEN HF_TOKEN

echo
echo "[5/11] Atualizando somente a API local do MediaForge..."
cd "$CONTROL_ROOT"
docker compose build api
docker compose run --rm api   npx wrangler d1 execute mediaforge-control   --local --persist-to /data --config wrangler.ovh.jsonc   --file=schema.sql --yes
docker compose run --rm api   npx wrangler d1 execute mediaforge-control   --local --persist-to /data --config wrangler.ovh.jsonc   --command "DROP TRIGGER IF EXISTS trg_assets_r2_free_tier_guard;" --yes
docker compose up -d --no-deps --force-recreate api

for i in $(seq 1 60); do
  curl -fsS "$LOCAL_API/api/health" >/tmp/mediaforge-final-health.json 2>/dev/null && break
  sleep 2
done
curl -fsS "$LOCAL_API/api/health" | tee /tmp/mediaforge-final-health.json
echo

echo
echo "[6/11] Vendorizando Stable Audio na OVH para o Kaggle não depender do GitHub..."
if [ ! -f "$VENDOR_ROOT/pyproject.toml" ]; then
  rm -rf "${VENDOR_ROOT}.tmp"
  git clone --depth 1 https://github.com/Stability-AI/stable-audio-3.git "${VENDOR_ROOT}.tmp"
  rm -rf "${VENDOR_ROOT}.tmp/.git"
  rm -rf "$VENDOR_ROOT"
  mv "${VENDOR_ROOT}.tmp" "$VENDOR_ROOT"
fi
test -f "$VENDOR_ROOT/pyproject.toml"
chmod -R a+rX "$VENDOR_ROOT"

echo
echo "[7/11] Migrando ~8.5 GiB da biblioteca GitHub Releases para armazenamento OVH..."
python3 "$STREAM_DIR/localize-mediaforge-library.py"   --api "$LOCAL_API"   --agent-token "$AGENT_TOKEN"   --map "$CONTROL_ROOT/music-url-map.json"   --cache-dir "$CONTROL_ROOT/library-migration-cache"   --stream-state "$STREAM_DIR/state"   --stations-dir "$STREAM_DIR/stations"   --audio-cache "$STREAM_DIR/state/audio-cache"   | tee /tmp/mediaforge-library-localization.log

echo
echo "[8/11] Removendo URLs do Cloudflare dos visuais ativos, sem reiniciar RTMP..."
OLD_API="$OLD_API" INTERNAL_API="$INTERNAL_API" python3 - "$STREAM_DIR/state" "$STREAM_DIR/.env" <<'PY'
import json,os,pathlib,sys
root=pathlib.Path(sys.argv[1]); envfile=pathlib.Path(sys.argv[2])
old=os.environ["OLD_API"]; new=os.environ["INTERNAL_API"]
changed=[]
for slot in ("kick","twitch","youtube-deep-house","youtube-rainy"):
    p=root/slot/"desired.json"
    if not p.is_file(): continue
    try: d=json.loads(p.read_text())
    except Exception: continue
    url=str(d.get("loop_url") or "")
    if url.startswith(old):
        d["loop_url"]=new+url[len(old):]
        try: d["visual_revision"]=int(d.get("visual_revision") or 0)+1
        except Exception: d["visual_revision"]=str(d.get("visual_revision") or "0")+"-ovh"
        p.write_text(json.dumps(d,ensure_ascii=False,indent=2)+"
")
        changed.append(slot)
if envfile.is_file():
    lines=[]
    for line in envfile.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k,v=line.split("=",1)
            if v.startswith(old): v=new+v[len(old):]
            line=k+"="+v
        lines.append(line)
    envfile.write_text("
".join(lines)+"
")
print("Visual URLs localized:",",".join(changed) if changed else "already-local")
PY

# The existing ovh-agent already talks to the local API. Do not recreate it:
# this keeps every streaming-related container untouched during this phase.
echo "OVH command agent mantido em execução sem recriação."

echo
echo "[9/11] Instalando o orquestrador permanente de geração musical na OVH..."
cat > /etc/systemd/system/mediaforge-music-agent.service <<EOF2
[Unit]
Description=MediaForge OVH Music Agent
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=$OFFICE_REPO
EnvironmentFile=$OPS_ENV
ExecStart=$MUSIC_VENV/bin/python $STREAM_DIR/music_agent.py
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF2
systemctl daemon-reload
systemctl enable --now mediaforge-music-agent.service
sleep 3
systemctl is-active --quiet mediaforge-music-agent.service
echo "Music agent: ACTIVE"

echo
echo "[10/11] Validando YouTube local, banco local, biblioteca local e agentes..."
curl -fsS "$LOCAL_API/api/ovh/agent/operational-status"   -H "x-ovh-agent-token: $AGENT_TOKEN"   | tee /tmp/mediaforge-operational-status.json
echo
python3 - /tmp/mediaforge-operational-status.json <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
assert d.get("local_runtime") is True, d
assert d.get("youtube_oauth") is True, d
assert d.get("youtube_channel") is True, d
print("YouTube control credentials: OVH OK")
PY

python3 - "$STREAM_DIR" <<'PY'
import pathlib,sys
root=pathlib.Path(sys.argv[1]);bad=[]
for p in list((root/"state").glob("*/playlist.json"))+list((root/"stations").glob("*.json")):
    try: text=p.read_text(encoding="utf-8")
    except Exception: continue
    if "github.com/thebusinessflowtv/theofficemusic/releases/download/" in text:
        bad.append(str(p))
if bad: raise SystemExit("GitHub audio URLs ainda presentes: "+", ".join(bad))
print("Runtime audio library: OVH LOCAL · GitHub Release URLs remaining=0")
PY

echo
echo "[11/11] Confirmando que nenhum encoder RTMP foi recriado..."
sleep 8
python3 - /tmp/mediaforge-encoders-before-final.json "$STREAM_DIR/state" <<'PY'
import json,pathlib,sys
before=json.load(open(sys.argv[1])); root=pathlib.Path(sys.argv[2]); bad=[]
for slot in ("kick","twitch","youtube-deep-house","youtube-rainy"):
    try: after=json.loads((root/slot/"health.json").read_text())
    except Exception as exc:
        print(f"{slot}: ERRO {exc}");bad.append(slot);continue
    bp=before.get(slot,{}).get("encoder_pid"); ap=after.get("encoder_pid"); st=after.get("status")
    same=(bp==ap and bp is not None)
    print(f"{slot}: status={st} encoder_pid_before={bp} encoder_pid_after={ap} preserved={same}")
    if st!="live" or not same: bad.append(slot)
if bad:
    print("ATENÇÃO: houve mudança de encoder em: "+",".join(bad))
    raise SystemExit(2)
PY

echo
echo "============================================================"
echo "MEDIAFORGE_RUNTIME_OVH_COMPLETE"
echo "PANEL=OVH"
echo "DATABASE=OVH_LOCAL"
echo "STATUS_QUEUE=OVH_LOCAL"
echo "COMMAND_QUEUE=OVH_LOCAL"
echo "OBJECT_STORAGE=OVH_LOCAL"
echo "YOUTUBE_CONTROL=OVH_LOCAL"
echo "MUSIC_GENERATION_ORCHESTRATOR=OVH"
echo "KAGGLE_TRIGGER=OVH"
echo "MUSIC_LIBRARY=OVH_LOCAL"
echo "GITHUB_ACTIONS_REQUIRED_FOR_RUNTIME=false"
echo "GITHUB_RELEASES_REQUIRED_FOR_LIVE_PLAYBACK=false"
echo "CLOUDFLARE_REQUIRED_FOR_PANEL=false"
echo "LIVE_ENCODERS_RECREATED=false"
echo "============================================================"
echo
echo "GitHub agora pode permanecer apenas como repositório de código/backup."
echo "Depois de confirmar este bloco, revogue os tokens antigos de Google/Kaggle/Hugging Face e remova os secrets antigos do GitHub."
