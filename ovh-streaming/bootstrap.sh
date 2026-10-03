#!/usr/bin/env bash
set -euo pipefail

# Production has already been migrated to the git-backed OVH runtime.
# Never fall back to the legacy /opt full-stack deployment from this hook:
# a shared rebuild would unnecessarily interrupt Twitch/YouTube.
REPO_DIR="${MEDIAFORGE_REPO:-$HOME/theofficemusic}"
OVH_DIR="$REPO_DIR/ovh-streaming"

if [ ! -d "$REPO_DIR/.git" ] || [ ! -f "$OVH_DIR/docker-compose.yml" ]; then
  echo "::error::Git-backed MediaForge OVH runtime not found at $REPO_DIR" >&2
  exit 1
fi

cd "$REPO_DIR"
OLD_HEAD="$(git rev-parse HEAD)"
git fetch origin main
git reset --hard origin/main
NEW_HEAD="$(git rev-parse HEAD)"

echo "MediaForge targeted deploy: $OLD_HEAD -> $NEW_HEAD"
cd "$OVH_DIR"

compose() {
  if docker compose version >/dev/null 2>&1; then
    sudo -n docker compose "$@"
  else
    sudo -n docker-compose "$@"
  fi
}

# Build first while all existing streams stay online.
compose build kick ovh-agent

# The Kick runtime needs one controlled replacement to install the persistent
# RTMP/video-feeder split. OVH agent is replaced alongside it so set_visual no
# longer increments the full-stream generation. No other streaming service is touched.
compose up -d --no-deps --force-recreate ovh-agent kick

# Wait for the new Kick encoder to report the hot-swap capability.
ok=0
for i in $(seq 1 48); do
  if python3 - <<'PY'
import json, pathlib, sys
p=pathlib.Path('state/kick/health.json')
try:
    d=json.loads(p.read_text())
except Exception:
    sys.exit(1)
good = d.get('status') == 'live' and d.get('hot_swap') is True and d.get('encoder_pid')
sys.exit(0 if good else 1)
PY
  then
    ok=1
    break
  fi
  sleep 5
done

if [ "$ok" -ne 1 ]; then
  echo "::error::Kick hot-swap runtime did not become live" >&2
  cat state/kick/health.json 2>/dev/null || true
  compose ps
  exit 1
fi

# Verify a visual-only revision does not change the RTMP encoder PID or the
# full-stream generation. The visible loop URL itself is kept unchanged.
BEFORE_PID="$(python3 - <<'PY'
import json
print(json.load(open('state/kick/health.json'))['encoder_pid'])
PY
)"
BEFORE_GEN="$(python3 - <<'PY'
import json
print(json.load(open('state/kick/desired.json')).get('generation',1))
PY
)"

python3 - <<'PY'
import json, pathlib
from datetime import datetime, timezone
p=pathlib.Path('state/kick/desired.json')
d=json.loads(p.read_text())
d['visual_revision']=int(d.get('visual_revision') or 0)+1
d['updated_at']=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
tmp=p.with_suffix('.json.tmp')
tmp.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
tmp.replace(p)
PY

sleep 8
AFTER_PID="$(python3 - <<'PY'
import json
print(json.load(open('state/kick/health.json'))['encoder_pid'])
PY
)"
AFTER_GEN="$(python3 - <<'PY'
import json
print(json.load(open('state/kick/desired.json')).get('generation',1))
PY
)"

test "$BEFORE_PID" = "$AFTER_PID"
test "$BEFORE_GEN" = "$AFTER_GEN"

echo "KICK_HOT_SWAP_VERIFIED encoder_pid=$BEFORE_PID generation=$BEFORE_GEN"
compose ps kick ovh-agent
