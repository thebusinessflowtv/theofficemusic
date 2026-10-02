#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"
INGEST_JSON="$ROOT/control/youtube-ovh-ingest.json"
ENV_FILE=".env"

if [ ! -f "$INGEST_JSON" ]; then
  echo "Missing $INGEST_JSON. Run git pull first." >&2
  exit 1
fi

readarray -t URLS < <(python3 - "$INGEST_JSON" <<'PY'
import json,sys
d=json.load(open(sys.argv[1],encoding="utf-8"))
for name in ("deep-house","rainy"):
    st=d["stations"][name]
    url=st.get("rtmps_backup_ingestion_address") or st.get("rtmp_backup_ingestion_address")
    if not url:
        raise SystemExit(f"Missing backup ingest URL for {name}")
    print(url)
PY
)
DEEP_URL="${URLS[0]}"
RAIN_URL="${URLS[1]}"

echo
echo "Peter Lofi — YouTube OVH zero-downtime handoff"
echo "Current YouTube primary encoders will remain online during this preparation."
echo
read -r -s -p "Deep House current YouTube Stream Key: " DEEP_KEY
echo
read -r -s -p "Rainy current YouTube Stream Key: " RAIN_KEY
echo

if [ -z "$DEEP_KEY" ] || [ -z "$RAIN_KEY" ]; then
  echo "Both YouTube Stream Keys are required." >&2
  exit 1
fi

python3 - "$ENV_FILE" "$DEEP_URL" "$DEEP_KEY" "$RAIN_URL" "$RAIN_KEY" <<'PY'
import pathlib,sys
path=pathlib.Path(sys.argv[1])
updates={
    "YOUTUBE_DEEP_HOUSE_STREAM_URL":sys.argv[2],
    "YOUTUBE_DEEP_HOUSE_STREAM_KEY":sys.argv[3],
    "YOUTUBE_RAINY_STREAM_URL":sys.argv[4],
    "YOUTUBE_RAINY_STREAM_KEY":sys.argv[5],
}
lines=path.read_text(encoding="utf-8").splitlines() if path.exists() else []
out=[]
seen=set()
for line in lines:
    if "=" in line and not line.lstrip().startswith("#"):
        key=line.split("=",1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
            continue
    out.append(line)
for key,val in updates.items():
    if key not in seen:
        out.append(f"{key}={val}")
path.write_text("\n".join(out).rstrip()+"\n",encoding="utf-8")
PY
chmod 600 "$ENV_FILE"
mkdir -p state/youtube-deep-house state/youtube-rainy

DC=(sudo docker compose)

echo
echo "Building YouTube OVH services..."
"${DC[@]}" build youtube-deep-house youtube-rainy control-api

echo
echo "Refreshing local control API..."
"${DC[@]}" up -d --no-deps --force-recreate control-api

wait_live() {
  local platform="$1"
  local seconds=0
  while [ "$seconds" -lt 240 ]; do
    status="$(curl -fsS http://127.0.0.1:8787/health 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print((d.get('$platform') or {}).get('status','unknown'))" 2>/dev/null || true)"
    restarts="$(curl -fsS http://127.0.0.1:8787/health 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print((d.get('$platform') or {}).get('restarts','?'))" 2>/dev/null || true)"
    echo "$platform status=$status restarts=$restarts"
    if [ "$status" = "live" ]; then
      return 0
    fi
    sleep 10
    seconds=$((seconds+10))
  done
  return 1
}

echo
echo "Starting Deep House on YouTube BACKUP ingest; current GitHub primary remains online..."
"${DC[@]}" up -d --no-deps --force-recreate youtube-deep-house
if ! wait_live youtube-deep-house; then
  echo "Deep House OVH backup did not stabilize. Current YouTube primary remains untouched." >&2
  "${DC[@]}" logs --tail=80 youtube-deep-house || true
  exit 1
fi

echo
echo "Deep House backup stable. Starting Rainy on YouTube BACKUP ingest..."
"${DC[@]}" up -d --no-deps --force-recreate youtube-rainy
if ! wait_live youtube-rainy; then
  echo "Rainy OVH backup did not stabilize. Current YouTube primary remains untouched." >&2
  "${DC[@]}" logs --tail=80 youtube-rainy || true
  exit 1
fi

echo
echo "YOUTUBE_OVH_BACKUPS_READY"
curl -fsS http://127.0.0.1:8787/health
echo
echo
echo "Do NOT stop the GitHub runners manually. Return to ChatGPT for the controlled cutover."
