#!/usr/bin/env bash
set -euo pipefail

OFFICE_REPO="/home/ubuntu/theofficemusic"
PANEL_REPO="/home/ubuntu/thebusinessflow"
CONTROL_ROOT="/opt/mediaforge-control"
REMOTE_API="https://mediaforge-api.guilhermeodsgn.workers.dev"
PUBLIC_IP="146.59.156.224"
LOCAL_API="http://127.0.0.1:8790"
WORKER_DIR="$PANEL_REPO/cloudflare/mediaforge-worker"
STREAM_DIR="$OFFICE_REPO/ovh-streaming"
SECRETS_FILE="$CONTROL_ROOT/worker.dev.vars"
AGENT_ENV="/etc/mediaforge-control-agent.env"

if [ "$(id -u)" -ne 0 ]; then
  echo "Execute este instalador com sudo."
  exit 1
fi

echo "=== MediaForge Control Plane -> OVH ==="
echo "Nenhum container de Kick, Twitch ou YouTube será recriado."
echo

for bin in git docker curl python3 openssl; do
  command -v "$bin" >/dev/null 2>&1 || { echo "Dependência ausente: $bin"; exit 1; }
done

mkdir -p "$CONTROL_ROOT"/{data,site,caddy-data,caddy-config}
chmod 700 "$CONTROL_ROOT"

# Capture live encoder PIDs before touching only the non-streaming control agent.
python3 - "$STREAM_DIR/state" > /tmp/mediaforge-streams-before.json <<'PY'
import json, pathlib, sys
root=pathlib.Path(sys.argv[1])
out={}
for slot in ("kick","twitch","youtube-deep-house","youtube-rainy"):
    try:
        out[slot]=json.loads((root/slot/"health.json").read_text())
    except Exception as exc:
        out[slot]={"status":"unknown","error":str(exc)}
print(json.dumps(out,indent=2))
PY

echo "[1/9] Sincronizando o código do painel/backend para a OVH..."
if [ ! -d "$PANEL_REPO/.git" ]; then
  runuser -u ubuntu -- git clone https://github.com/thebusinessflowtv/thebusinessflow.git "$PANEL_REPO"
else
  runuser -u ubuntu -- git -C "$PANEL_REPO" fetch origin main
  runuser -u ubuntu -- git -C "$PANEL_REPO" reset --hard origin/main
fi

test -f "$WORKER_DIR/src/index.js"
test -f "$WORKER_DIR/schema.sql"
test -f "$WORKER_DIR/Dockerfile.ovh"
test -f "$WORKER_DIR/wrangler.ovh.jsonc"

echo "[2/9] Preparando o MediaForge web local..."
rm -rf "$CONTROL_ROOT/site"/*
cp -a "$PANEL_REPO/control-center/." "$CONTROL_ROOT/site/"
cp "$CONTROL_ROOT/site/secure.html" "$CONTROL_ROOT/site/index.html"
cat > "$CONTROL_ROOT/site/mediaforge-config.js" <<'JS'
window.MEDIAFORGE_CONFIG = { API_URL: window.location.origin };
JS

# Remove visible Cloudflare branding from the local copy only.
python3 - "$CONTROL_ROOT/site" <<'PY'
import pathlib, sys
root=pathlib.Path(sys.argv[1])
repl={
    "Cloudflare R2 + D1":"OVH · armazenamento local",
    "Cloudflare R2":"armazenamento local OVH",
    "Cloudflare D1":"banco local OVH",
    "R2 + D1":"OVH LOCAL",
    "R2 / D1":"OVH LOCAL",
    "Cloudflare Worker":"API local na OVH",
    "Backend Cloudflare":"Backend OVH",
    "backend Cloudflare":"backend OVH",
    "Cloudflare":"OVH",
}
for p in root.rglob("*"):
    if p.is_file() and p.suffix.lower() in {".html",".js",".css"}:
        try: text=p.read_text(encoding="utf-8")
        except Exception: continue
        for a,b in repl.items(): text=text.replace(a,b)
        text=text.replace("const DEFAULT_API='https://mediaforge-api.guilhermeodsgn.workers.dev';","const DEFAULT_API=window.location.origin;")
        p.write_text(text,encoding="utf-8")
PY

echo "[3/9] Criando credenciais locais e banco persistente..."
if [ ! -s "$SECRETS_FILE" ]; then
  echo
  read -r -p "E-mail de administrador do MediaForge: " ADMIN_EMAIL
  while [ -z "$ADMIN_EMAIL" ]; do
    read -r -p "E-mail não pode ficar vazio. Digite novamente: " ADMIN_EMAIL
  done
  read -r -s -p "Senha do MediaForge na OVH: " ADMIN_PASSWORD
  echo
  while [ -z "$ADMIN_PASSWORD" ]; do
    read -r -s -p "Senha não pode ficar vazia. Digite novamente: " ADMIN_PASSWORD
    echo
  done
  SESSION_SECRET="$(openssl rand -hex 48)"
  AGENT_TOKEN="$(openssl rand -hex 32)"
  MIGRATION_TOKEN="$(openssl rand -hex 32)"
  ADMIN_EMAIL="$ADMIN_EMAIL" ADMIN_PASSWORD="$ADMIN_PASSWORD" SESSION_SECRET="$SESSION_SECRET" AGENT_TOKEN="$AGENT_TOKEN" MIGRATION_TOKEN="$MIGRATION_TOKEN" \
  python3 - "$SECRETS_FILE" <<'PY'
import json, os, pathlib, sys
p=pathlib.Path(sys.argv[1])
vals={
  "ADMIN_EMAIL":os.environ["ADMIN_EMAIL"],
  "ADMIN_PASSWORD":os.environ["ADMIN_PASSWORD"],
  "SESSION_SECRET":os.environ["SESSION_SECRET"],
  "OVH_AGENT_TOKEN":os.environ["AGENT_TOKEN"],
  "LOCAL_MIGRATION_TOKEN":os.environ["MIGRATION_TOKEN"],
}
p.write_text("\n".join(f"{k}={json.dumps(v)}" for k,v in vals.items())+"\n",encoding="utf-8")
PY
  chmod 600 "$SECRETS_FILE"
else
  echo "Credenciais locais já existem; mantendo as atuais."
fi

AGENT_TOKEN="$(python3 - "$SECRETS_FILE" <<'PY'
import pathlib,sys,json
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    if line.startswith("OVH_AGENT_TOKEN="):
        print(json.loads(line.split("=",1)[1]));break
PY
)"
MIGRATION_TOKEN="$(python3 - "$SECRETS_FILE" <<'PY'
import pathlib,sys,json
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    if line.startswith("LOCAL_MIGRATION_TOKEN="):
        print(json.loads(line.split("=",1)[1]));break
PY
)"
test -n "$AGENT_TOKEN"
test -n "$MIGRATION_TOKEN"

cat > "$CONTROL_ROOT/docker-compose.yml" <<EOF
services:
  api:
    build:
      context: $WORKER_DIR
      dockerfile: Dockerfile.ovh
    container_name: mediaforge-api-ovh
    restart: unless-stopped
    volumes:
      - $CONTROL_ROOT/data:/data
      - $SECRETS_FILE:/app/.dev.vars:ro
    ports:
      - "127.0.0.1:8790:8790"

  web:
    image: caddy:2-alpine
    container_name: mediaforge-web-ovh
    restart: unless-stopped
    depends_on:
      - api
    ports:
      - "8443:8443"
      - "127.0.0.1:8844:8844"
    volumes:
      - $CONTROL_ROOT/site:/srv:ro
      - $CONTROL_ROOT/Caddyfile:/etc/caddy/Caddyfile:ro
      - $CONTROL_ROOT/caddy-data:/data
      - $CONTROL_ROOT/caddy-config:/config
EOF

cat > "$CONTROL_ROOT/Caddyfile" <<'CADDY'
:8443 {
  tls internal
  encode gzip
  @backend path /api/* /media/*
  handle @backend {
    reverse_proxy api:8790
  }
  handle {
    root * /srv
    try_files {path} {path}/ /index.html
    file_server
  }
}

:8844 {
  bind 127.0.0.1
  encode gzip
  @backend path /api/* /media/*
  handle @backend {
    reverse_proxy api:8790
  }
  handle {
    root * /srv
    try_files {path} {path}/ /index.html
    file_server
  }
}
CADDY

cd "$CONTROL_ROOT"
docker compose build api

# Apply the exact D1 schema to the local persistent SQLite binding.
docker compose run --rm api \
  npx wrangler d1 execute mediaforge-control \
  --local --persist-to /data --config wrangler.ovh.jsonc \
  --file=schema.sql --yes

# The 9 GB R2 free-tier guard is not relevant on local OVH storage.
docker compose run --rm api \
  npx wrangler d1 execute mediaforge-control \
  --local --persist-to /data --config wrangler.ovh.jsonc \
  --command "DROP TRIGGER IF EXISTS trg_assets_r2_free_tier_guard;" --yes

docker compose up -d api web

echo "Aguardando a API local..."
for i in $(seq 1 60); do
  if curl -fsS "$LOCAL_API/api/health" >/tmp/mediaforge-local-health.json 2>/dev/null; then
    break
  fi
  sleep 2
done
curl -fsS "$LOCAL_API/api/health" | tee /tmp/mediaforge-local-health.json
echo

echo "[4/9] Exportando o D1 atual antes do limite diário..."
SNAPSHOT=/tmp/mediaforge-cloudflare-snapshot.json
ok=0
for i in $(seq 1 30); do
  if curl -fsS "$REMOTE_API/api/migration/export" -o "$SNAPSHOT"; then
    ok=1
    break
  fi
  echo "Endpoint de exportação ainda propagando no Worker; tentativa $i/30..."
  sleep 4
done
if [ "$ok" -ne 1 ]; then
  echo "Não foi possível exportar o D1 remoto. Nada das lives foi alterado."
  echo "Tente este mesmo instalador novamente em alguns minutos."
  exit 1
fi

# Refresh local config JSON from the repository already on the OVH, so runtime
# config no longer needs GitHub after cut-over.
python3 - "$SNAPSHOT" "$OFFICE_REPO" <<'PY'
import json, pathlib, sys
snap=pathlib.Path(sys.argv[1])
repo=pathlib.Path(sys.argv[2])
data=json.loads(snap.read_text())
configs=data.setdefault("configs",{})
paths=[
 "control/mediaforge-catalog.json",
 "control/music-library.json",
 "control/youtube-stations.json",
 "config/peter_lofi_series.json",
 "control/gaming-reference-production/references.json",
 "control/twitch-dj-supplied-2026-10-03.json",
 "control/live-analytics.json",
]
for rel in paths:
    p=repo/rel
    if p.is_file():
        try: configs[rel]=json.loads(p.read_text(encoding="utf-8"))
        except Exception: pass
snap.write_text(json.dumps(data,ensure_ascii=False),encoding="utf-8")
PY

echo "[5/9] Importando D1 -> banco SQLite persistente da OVH..."
curl -fsS -X POST "$LOCAL_API/api/local/import-snapshot" \
  -H "x-local-migration-token: $MIGRATION_TOKEN" \
  -H "content-type: application/json" \
  --data-binary @"$SNAPSHOT" | tee /tmp/mediaforge-import-result.json
echo

echo "[6/9] Migrando objetos R2 usados pelo painel para armazenamento local..."
python3 - "$SNAPSHOT" > /tmp/mediaforge-ready-assets.tsv <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
for a in d.get("tables",{}).get("assets",[]):
    if str(a.get("status"))!="ready": continue
    vals=[a.get("id",""),a.get("download_token",""),a.get("r2_key",""),a.get("mime_type") or "application/octet-stream"]
    if all(str(x) for x in vals[:3]):
        print("\t".join(str(x).replace("\t"," ") for x in vals))
PY

TOTAL="$(wc -l </tmp/mediaforge-ready-assets.tsv | tr -d ' ')"
N=0
while IFS=$'\t' read -r ASSET_ID DOWNLOAD_TOKEN R2_KEY MIME; do
  [ -n "$ASSET_ID" ] || continue
  N=$((N+1))
  TMP="/tmp/mediaforge-r2-${ASSET_ID}.bin"
  echo "  [$N/$TOTAL] $R2_KEY"
  curl -fL --retry 4 --retry-delay 2 \
    "$REMOTE_API/media/$ASSET_ID/$DOWNLOAD_TOKEN" \
    -o "$TMP"
  ENC_KEY="$(python3 - "$R2_KEY" <<'PY'
import sys,urllib.parse
print(urllib.parse.quote(sys.argv[1],safe=""))
PY
)"
  curl -fsS -X PUT "$LOCAL_API/api/local/import-object?key=$ENC_KEY" \
    -H "x-local-migration-token: $MIGRATION_TOKEN" \
    -H "content-type: $MIME" \
    --data-binary @"$TMP" >/dev/null
  rm -f "$TMP"
done < /tmp/mediaforge-ready-assets.tsv

echo "[7/9] Mudando status e comandos do painel para a API local da OVH..."
# Preserve all existing streaming secrets in the .env file; update only the two
# MediaForge control-plane keys used by the non-streaming agent.
python3 - "$STREAM_DIR/.env" "$AGENT_TOKEN" <<'PY'
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
        out.append(f"{key}={updates[key]}");seen.add(key)
    else:
        out.append(line)
for k,v in updates.items():
    if k not in seen: out.append(f"{k}={v}")
p.write_text("\n".join(out)+"\n")
PY
chmod 600 "$STREAM_DIR/.env"

cd "$STREAM_DIR"
docker compose build ovh-agent
docker compose up -d --no-deps --force-recreate ovh-agent

cat > "$AGENT_ENV" <<EOF
MEDIAFORGE_API_URL=http://127.0.0.1:8790
MEDIAFORGE_AGENT_TOKEN=$AGENT_TOKEN
EOF
chmod 600 "$AGENT_ENV"
mkdir -p /etc/systemd/system/mediaforge-deploy-agent.service.d
cat > /etc/systemd/system/mediaforge-deploy-agent.service.d/local-mediaforge.conf <<EOF
[Service]
EnvironmentFile=$AGENT_ENV
EOF
systemctl daemon-reload
systemctl restart mediaforge-deploy-agent.service

echo "[8/9] Validando heartbeat e comandos 100% locais..."
sleep 15
curl -fsS "$LOCAL_API/api/ovh/public-health" | tee /tmp/mediaforge-local-ovh-health.json
echo
curl -fsS "$LOCAL_API/api/local/migration-status" \
  -H "x-local-migration-token: $MIGRATION_TOKEN" \
  | tee /tmp/mediaforge-local-migration-status.json
echo

echo "[9/9] Confirmando que os quatro encoders não foram recriados..."
python3 - /tmp/mediaforge-streams-before.json "$STREAM_DIR/state" <<'PY'
import json,pathlib,sys
before=json.load(open(sys.argv[1]))
root=pathlib.Path(sys.argv[2])
bad=[]
for slot in ("kick","twitch","youtube-deep-house","youtube-rainy"):
    try: after=json.loads((root/slot/"health.json").read_text())
    except Exception as exc:
        print(f"{slot}: ERRO lendo health: {exc}");bad.append(slot);continue
    bp=before.get(slot,{}).get("encoder_pid")
    ap=after.get("encoder_pid")
    st=after.get("status")
    same=(bp==ap and bp is not None)
    print(f"{slot}: status={st} encoder_pid_before={bp} encoder_pid_after={ap} preserved={same}")
    if st!="live" or not same: bad.append(slot)
if bad:
    print("ATENÇÃO: verificar manualmente -> "+",".join(bad))
    raise SystemExit(2)
PY

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 8443/tcp >/dev/null || true
fi

echo
echo "============================================================"
echo "MEDIAFORGE_CONTROL_PLANE_OVH_ACTIVE"
echo "PANEL_HOST=OVH"
echo "DATABASE=OVH_LOCAL_SQLITE_D1_COMPATIBLE"
echo "STATUS_COPY=OVH_LOCAL"
echo "COMMAND_QUEUE=OVH_LOCAL"
echo "OBJECT_STORAGE=OVH_LOCAL"
echo "CLOUDFLARE_REQUIRED_FOR_PANEL=false"
echo "GITHUB_REQUIRED_FOR_RUNTIME_COMMAND_QUEUE=false"
echo "LIVE_ENCODERS_RECREATED=false"
echo "PANEL_URL=https://$PUBLIC_IP:8443/"
echo "============================================================"
echo
echo "Observação: o certificado HTTPS é interno da própria OVH/Caddy."
echo "Na primeira abertura do IP, o navegador poderá pedir para aceitar o certificado."
