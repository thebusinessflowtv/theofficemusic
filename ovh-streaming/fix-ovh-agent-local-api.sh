#!/usr/bin/env bash
set -euo pipefail

STREAM_DIR="/home/ubuntu/theofficemusic/ovh-streaming"
ENV_FILE="$STREAM_DIR/.env"
LOCAL_API="http://127.0.0.1:8790"

if [ "$(id -u)" -ne 0 ]; then
  echo "Execute com sudo."
  exit 1
fi

echo "=== FIX OVH AGENT LOCAL API ==="

python3 - "$ENV_FILE" <<'PY'
import pathlib,sys
p=pathlib.Path(sys.argv[1])
lines=p.read_text().splitlines() if p.exists() else []
out=[]; found=False
for line in lines:
    if line.startswith("MEDIAFORGE_API_URL="):
        out.append("MEDIAFORGE_API_URL=http://127.0.0.1:8790")
        found=True
    else:
        out.append(line)
if not found:
    out.append("MEDIAFORGE_API_URL=http://127.0.0.1:8790")
p.write_text("\n".join(out)+"\n")
PY

cd "$STREAM_DIR"
echo "[1/4] Recriando somente ovh-agent..."
docker compose up -d --no-deps --force-recreate ovh-agent

sleep 6

echo "[2/4] Rede e API vistas pelo agente..."
docker inspect peter-lofi-ovh-agent --format 'network_mode={{.HostConfig.NetworkMode}}'
docker exec peter-lofi-ovh-agent python - <<'PY'
import os,urllib.request
base=os.environ.get("MEDIAFORGE_API_URL","").rstrip("/")
print("MEDIAFORGE_API_URL =",base)
with urllib.request.urlopen(base+"/api/health",timeout=10) as r:
    print("API HTTP =",r.status)
    print(r.read().decode()[:300])
PY

echo "[3/4] Últimos logs do agente..."
docker logs --tail 40 peter-lofi-ovh-agent 2>&1

echo "[4/4] Heartbeat..."
sleep 6
curl -fsS "$LOCAL_API/api/ovh/public-health" | python3 -m json.tool | head -100

echo
echo "OVH_AGENT_LOCAL_API_FIXED"
