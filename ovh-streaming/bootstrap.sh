#!/usr/bin/env bash
set -euo pipefail

# Rolling migration to persistent RTMP publishers.
# Future visual changes update only visual_revision and MUST preserve encoder_pid.
REPO_DIR="${MEDIAFORGE_REPO:-$HOME/theofficemusic}"
OVH_DIR="$REPO_DIR/ovh-streaming"
SLOTS=(kick twitch youtube-deep-house youtube-rainy)

if [ ! -d "$REPO_DIR/.git" ] || [ ! -f "$OVH_DIR/docker-compose.yml" ]; then
  echo "::error::Git-backed MediaForge OVH runtime not found at $REPO_DIR" >&2
  exit 1
fi

cd "$REPO_DIR"
OLD_HEAD="$(git rev-parse HEAD)"
git fetch origin main
git reset --hard origin/main
NEW_HEAD="$(git rev-parse HEAD)"
echo "MediaForge rolling hot-swap deploy: $OLD_HEAD -> $NEW_HEAD"

cd "$OVH_DIR"

compose() {
  if sudo -n docker compose version >/dev/null 2>&1; then
    sudo -n docker compose "$@"
  else
    sudo -n docker-compose "$@"
  fi
}

container_name() {
  printf 'peter-lofi-%s' "$1"
}

is_hotswap_ready() {
  local slot="$1"
  SLOT="$slot" python3 - <<'PY'
import json, os, pathlib, sys
slot=os.environ["SLOT"]
p=pathlib.Path("state")/slot/"health.json"
try:
    d=json.loads(p.read_text())
except Exception:
    sys.exit(1)
ok=(d.get("status")=="live" and d.get("hot_swap") is True and bool(d.get("encoder_pid")) and d.get("visual_status")=="streaming")
sys.exit(0 if ok else 1)
PY
}

wait_hotswap_ready() {
  local slot="$1"
  local tries="${2:-72}"
  for _ in $(seq 1 "$tries"); do
    if is_hotswap_ready "$slot"; then
      return 0
    fi
    sleep 5
  done
  return 1
}

health_field() {
  local slot="$1" field="$2"
  SLOT="$slot" FIELD="$field" python3 - <<'PY'
import json, os, pathlib
d=json.loads((pathlib.Path("state")/os.environ["SLOT"]/"health.json").read_text())
v=d.get(os.environ["FIELD"])
print("" if v is None else v)
PY
}

desired_field() {
  local slot="$1" field="$2"
  SLOT="$slot" FIELD="$field" python3 - <<'PY'
import json, os, pathlib
d=json.loads((pathlib.Path("state")/os.environ["SLOT"]/"desired.json").read_text())
v=d.get(os.environ["FIELD"])
print("" if v is None else v)
PY
}

bump_visual_revision() {
  local slot="$1"
  SLOT="$slot" python3 - <<'PY'
import json, os, pathlib
from datetime import datetime, timezone
p=pathlib.Path("state")/os.environ["SLOT"]/"desired.json"
d=json.loads(p.read_text())
d["visual_revision"]=int(d.get("visual_revision") or 0)+1
d["updated_at"]=datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
tmp=p.with_suffix(".json.tmp")
tmp.write_text(json.dumps(d,ensure_ascii=False,indent=2)+"\n")
tmp.replace(p)
PY
}

declare -A OLD_IMAGE_NAME
declare -A BACKUP_TAG

STAMP="$(date -u +%Y%m%d%H%M%S)"
for slot in "${SLOTS[@]}"; do
  name="$(container_name "$slot")"
  OLD_IMAGE_NAME["$slot"]="$(sudo -n docker inspect -f '{{.Config.Image}}' "$name" 2>/dev/null || true)"
  old_id="$(sudo -n docker inspect -f '{{.Image}}' "$name" 2>/dev/null || true)"
  if [ -n "$old_id" ]; then
    BACKUP_TAG["$slot"]="mediaforge-pre-hotswap-${slot}:${STAMP}"
    sudo -n docker tag "$old_id" "${BACKUP_TAG[$slot]}"
  fi
done

# Build while every current live remains online.
compose build ovh-agent "${SLOTS[@]}"

# Agent is control-plane only; replacing it does not touch RTMP publishers.
compose up -d --no-deps --force-recreate ovh-agent

rollback_slot() {
  local slot="$1"
  local image_name="${OLD_IMAGE_NAME[$slot]:-}"
  local backup="${BACKUP_TAG[$slot]:-}"
  if [ -n "$image_name" ] && [ -n "$backup" ]; then
    echo "::warning::Rolling back $slot to previous image"
    sudo -n docker tag "$backup" "$image_name"
    compose up -d --no-deps --force-recreate --no-build "$slot" || true
  fi
}

verify_visual_swap() {
  local slot="$1"
  local before_pid before_gen after_pid after_gen
  before_pid="$(health_field "$slot" encoder_pid)"
  before_gen="$(desired_field "$slot" generation)"
  bump_visual_revision "$slot"
  sleep 10

  if ! wait_hotswap_ready "$slot" 24; then
    echo "::error::$slot lost hot-swap readiness during visual revision test" >&2
    return 1
  fi

  after_pid="$(health_field "$slot" encoder_pid)"
  after_gen="$(desired_field "$slot" generation)"
  if [ "$before_pid" != "$after_pid" ]; then
    echo "::error::$slot encoder PID changed during visual-only swap: $before_pid -> $after_pid" >&2
    return 1
  fi
  if [ "$before_gen" != "$after_gen" ]; then
    echo "::error::$slot generation changed during visual-only swap: $before_gen -> $after_gen" >&2
    return 1
  fi
  echo "HOT_SWAP_VERIFIED slot=$slot encoder_pid=$after_pid generation=$after_gen"
}

RESULTS=()
for slot in "${SLOTS[@]}"; do
  if is_hotswap_ready "$slot"; then
    echo "$slot already uses persistent RTMP hot-swap; no publisher restart needed."
  else
    echo "Migrating $slot to persistent RTMP hot-swap..."
    compose up -d --no-deps --force-recreate "$slot"
    if ! wait_hotswap_ready "$slot"; then
      echo "::error::$slot did not become hot-swap ready" >&2
      cat "state/$slot/health.json" 2>/dev/null || true
      rollback_slot "$slot"
      exit 1
    fi
  fi

  if ! verify_visual_swap "$slot"; then
    rollback_slot "$slot"
    exit 1
  fi

  RESULTS+=("$slot")
done

python3 - <<'PY'
import json, pathlib
from datetime import datetime, timezone
slots=["kick","twitch","youtube-deep-house","youtube-rainy"]
services={}
for slot in slots:
    base=pathlib.Path("state")/slot
    h=json.loads((base/"health.json").read_text())
    d=json.loads((base/"desired.json").read_text())
    services[slot]={
        "status":h.get("status"),
        "hot_swap":h.get("hot_swap"),
        "encoder_pid":h.get("encoder_pid"),
        "visual_pid":h.get("visual_pid"),
        "visual_status":h.get("visual_status"),
        "fps":h.get("fps"),
        "video_bitrate_kbps":h.get("video_bitrate_kbps"),
        "restarts":h.get("restarts"),
        "generation":d.get("generation"),
        "visual_revision":d.get("visual_revision"),
    }
payload={
    "ok":all(v.get("status")=="live" and v.get("hot_swap") is True and v.get("encoder_pid") and v.get("visual_status")=="streaming" for v in services.values()),
    "verified_at":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
    "services":services,
}
pathlib.Path("state/hot-swap-rollout-result.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n")
print(json.dumps(payload,ensure_ascii=False,indent=2))
if not payload["ok"]:
    raise SystemExit(1)
PY

echo "ALL_LIVE_HOT_SWAP_VERIFIED"
compose ps ovh-agent "${SLOTS[@]}"
