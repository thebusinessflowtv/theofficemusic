#!/usr/bin/env bash
set -euo pipefail

OFFICE_REPO="/home/ubuntu/theofficemusic"
PANEL_REPO="/home/ubuntu/thebusinessflow"
CONTROL_ROOT="/opt/mediaforge-control"
STREAM_DIR="$OFFICE_REPO/ovh-streaming"
PUBLIC_IP="146.59.156.224"
PUBLIC_PORT="8443"

if [ "$(id -u)" -ne 0 ]; then
  echo "Execute com sudo."
  exit 1
fi

for bin in git docker curl openssl python3; do
  command -v "$bin" >/dev/null 2>&1 || { echo "Dependência ausente: $bin"; exit 1; }
done

echo "=== PUBLICAR MEDIAFORGE NA OVH (SEM REINICIAR LIVES) ==="

python3 - "$STREAM_DIR/state" > /tmp/mf-pids-before-public.json <<'PY'
import json,pathlib,sys
root=pathlib.Path(sys.argv[1]); out={}
for slot in ("kick","twitch","youtube-deep-house","youtube-rainy"):
    try: out[slot]=json.loads((root/slot/"health.json").read_text())
    except Exception as e: out[slot]={"error":str(e)}
print(json.dumps(out))
PY

echo "[1/7] Atualizando painel..."
runuser -u ubuntu -- git -C "$PANEL_REPO" fetch origin main
runuser -u ubuntu -- git -C "$PANEL_REPO" reset --hard origin/main

echo "[2/7] Copiando frontend para a OVH..."
mkdir -p "$CONTROL_ROOT/site" "$CONTROL_ROOT/certs" "$CONTROL_ROOT/caddy-data" "$CONTROL_ROOT/caddy-config"
rm -rf "$CONTROL_ROOT/site"/*
cp -a "$PANEL_REPO/control-center/." "$CONTROL_ROOT/site/"
cp "$CONTROL_ROOT/site/secure.html" "$CONTROL_ROOT/site/index.html"
cat > "$CONTROL_ROOT/site/mediaforge-config.js" <<'JS'
window.MEDIAFORGE_CONFIG = { API_URL: window.location.origin };
JS

echo "[3/7] Gerando certificado HTTPS da OVH para o IP..."
if [ ! -s "$CONTROL_ROOT/certs/mediaforge.key" ] || [ ! -s "$CONTROL_ROOT/certs/mediaforge.crt" ]; then
  openssl req -x509 -nodes -newkey rsa:2048     -keyout "$CONTROL_ROOT/certs/mediaforge.key"     -out "$CONTROL_ROOT/certs/mediaforge.crt"     -days 825     -subj "/CN=$PUBLIC_IP"     -addext "subjectAltName=IP:$PUBLIC_IP"
  chmod 600 "$CONTROL_ROOT/certs/mediaforge.key"
fi

echo "[4/7] Configurando Caddy HTTPS..."
cat > "$CONTROL_ROOT/Caddyfile" <<EOF
https://$PUBLIC_IP:$PUBLIC_PORT {
    tls /certs/mediaforge.crt /certs/mediaforge.key
    encode gzip

    @backend path /api/* /media/*
    handle @backend {
        reverse_proxy 127.0.0.1:8790
    }

    handle {
        root * /srv
        try_files {path} {path}/ /index.html
        file_server
    }
}
EOF

echo "[5/7] Recriando SOMENTE o web do MediaForge..."
docker rm -f mediaforge-web-ovh >/dev/null 2>&1 || true
docker run -d   --name mediaforge-web-ovh   --restart unless-stopped   --network host   -v "$CONTROL_ROOT/site:/srv:ro"   -v "$CONTROL_ROOT/Caddyfile:/etc/caddy/Caddyfile:ro"   -v "$CONTROL_ROOT/certs:/certs:ro"   -v "$CONTROL_ROOT/caddy-data:/data"   -v "$CONTROL_ROOT/caddy-config:/config"   caddy:2-alpine >/tmp/mf-web-container-id.txt

if command -v ufw >/dev/null 2>&1; then
  ufw allow "$PUBLIC_PORT/tcp" >/dev/null || true
fi

sleep 4

echo "[6/7] Validando painel e API..."
ss -lntp | grep ":$PUBLIC_PORT" || true
curl -kfsS --resolve "$PUBLIC_IP:$PUBLIC_PORT:127.0.0.1" "https://$PUBLIC_IP:$PUBLIC_PORT/api/health"
echo
curl -kfsS --resolve "$PUBLIC_IP:$PUBLIC_PORT:127.0.0.1" "https://$PUBLIC_IP:$PUBLIC_PORT/secure.html" >/dev/null
echo "HTTPS local OK"

echo "[7/7] Confirmando que nenhuma live foi reiniciada..."
python3 - /tmp/mf-pids-before-public.json "$STREAM_DIR/state" <<'PY'
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
echo "MEDIAFORGE_OVH_PUBLIC_OK"
echo "Abra no navegador: https://$PUBLIC_IP:$PUBLIC_PORT/"
