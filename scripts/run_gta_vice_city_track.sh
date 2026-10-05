#!/usr/bin/env bash
set -euo pipefail

mkdir -p build
if test -f "control/gta-vi-vice-city/$(printf '%02d' "${INDEX}").json"; then
  echo "Track ${INDEX} already completed; skipping."
  exit 0
fi

python scripts/build_gta_vice_city_profile.py --index "$INDEX" --out build/reference_profile.json

SECRET_DIR="$(mktemp -d)"
trap 'rm -rf "$SECRET_DIR"' EXIT
printf '%s' "$HF_TOKEN" > "$SECRET_DIR/hf_token.txt"
chmod 600 "$SECRET_DIR/hf_token.txt"

python - "$SECRET_DIR" <<'PY'
import json, os, pathlib, sys
p=pathlib.Path(sys.argv[1])
(p/"dataset-metadata.json").write_text(json.dumps({
    "title":"Peter Lofi Music Generation Secrets",
    "id":f"{os.environ['KAGGLE_USERNAME']}/peter-lofi-gaming-dj30-secrets",
    "licenses":[{"name":"other"}],
    "description":"Private CI credential input for Peter Lofi Stable Audio generation."
},indent=2))
PY

DATASET="$KAGGLE_USERNAME/$SECRET_DATASET_SLUG"
if kaggle datasets status "$DATASET" >/dev/null 2>&1; then
  kaggle datasets version -p "$SECRET_DIR" -m "Refresh HF token for GTA VI - Vice City" -q
else
  kaggle datasets create -p "$SECRET_DIR" -q
fi

READY=0
for n in $(seq 1 60); do
  STATUS="$(kaggle datasets status "$DATASET" 2>&1)"
  echo "$STATUS"
  if printf '%s' "$STATUS" | grep -Eqi '\bready\b'; then READY=1; break; fi
  if printf '%s' "$STATUS" | grep -Eqi 'error|failed|denied|not found'; then exit 1; fi
  sleep 5
done
test "$READY" -eq 1
kaggle datasets files "$DATASET" | grep -q hf_token.txt

TMP="$(mktemp -d)"
cp kaggle/runner_gta_vice_city.py "$TMP/runner.py"
REQUEST_ID="gta-vc-${INDEX}-a${ATTEMPT}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
PROFILE_B64="$(base64 -w0 build/reference_profile.json)"

python - "$TMP/runner.py" "$REQUEST_ID" "$INDEX" "$ATTEMPT" "$PROFILE_B64" <<'PY'
import pathlib,re,sys
p=pathlib.Path(sys.argv[1])
req=sys.argv[2]
idx=int(sys.argv[3])
attempt=int(sys.argv[4])
payload=sys.argv[5]
t=p.read_text(encoding="utf-8")
t=re.sub(r'REQUEST_ID = "[^"]*"', f'REQUEST_ID = "{req}"', t, count=1)
t=re.sub(r'TRACK_INDEX = \d+', f'TRACK_INDEX = {idx}', t, count=1)
t=re.sub(r'ATTEMPT = \d+', f'ATTEMPT = {attempt}', t, count=1)
t=re.sub(r'PROFILE_PAYLOAD_B64 = "[^"]*"', f'PROFILE_PAYLOAD_B64 = "{payload}"', t, count=1)
p.write_text(t,encoding="utf-8")
PY

KERNEL_NAME="peter-lofi-gta-vc-${INDEX}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
python - "$TMP" "$KERNEL_NAME" <<'PY'
import json,os,pathlib,sys
d=pathlib.Path(sys.argv[1]); name=sys.argv[2]
(d/"kernel-metadata.json").write_text(json.dumps({
    "id":f"{os.environ['KAGGLE_USERNAME']}/{name}",
    "title":name,
    "code_file":"runner.py",
    "language":"python",
    "kernel_type":"script",
    "is_private":True,
    "enable_gpu":True,
    "machine_shape":"NvidiaTeslaT4",
    "enable_internet":True,
    "dataset_sources":[f"{os.environ['KAGGLE_USERNAME']}/peter-lofi-gaming-dj30-secrets"],
    "competition_sources":[],
    "kernel_sources":[]
},indent=2))
PY

if ! PUSH_OUTPUT="$(kaggle kernels push -p "$TMP" --timeout 7200 --accelerator NvidiaTeslaT4 2>&1)"; then
  printf '%s\n' "$PUSH_OUTPUT"
  exit 1
fi
printf '%s\n' "$PUSH_OUTPUT"

KERNEL_REF="$(printf '%s\n' "$PUSH_OUTPUT" | python -c 'import re,sys; m=re.search(r"https://www[.]kaggle[.]com/code/([A-Za-z0-9_-]+/[A-Za-z0-9_-]+)",sys.stdin.read()); print(m.group(1) if m else "")')"
test -n "$KERNEL_REF" || { echo 'Kaggle did not return a canonical kernel URL'; exit 1; }
sleep 20

for i in $(seq 1 120); do
  STATUS="$(kaggle kernels status "$KERNEL_REF" 2>&1 || true)"
  echo "$STATUS"
  if echo "$STATUS" | grep -Eqi 'complete|completed'; then
    break
  fi
  if echo "$STATUS" | grep -Eqi 'error|failed|cancel|denied|Cannot access|not found'; then
    kaggle kernels logs "$KERNEL_REF" 2>&1 | tail -n 250 || true
    exit 1
  fi
  if [ "$i" -eq 120 ]; then
    kaggle kernels logs "$KERNEL_REF" 2>&1 | tail -n 250 || true
    exit 1
  fi
  sleep 60
done

rm -rf generated
mkdir -p generated
kaggle kernels output "$KERNEL_REF" -p generated

MARKER="$(find generated -type f -name request_id.txt -print -quit)"
test -n "$MARKER"
test "$(tr -d '\r\n ' < "$MARKER")" = "$REQUEST_ID"
WAV="$(find generated -type f -name '*.wav' -print -quit)"
test -n "$WAV"

python scripts/quality_gate_audio.py "$WAV" \
  --reference-profile build/reference_profile.json \
  --out build/qc.json \
  --normalized build/approved.mp3

PAD="$(printf '%03d' "$INDEX")"
MP3_NAME="peter-lofi-gta-vice-city-${PAD}.mp3"
mv build/approved.mp3 "build/$MP3_NAME"
TAG="peter-lofi-gta-vice-city-${PAD}-${GITHUB_RUN_ID}"

gh release create "$TAG" "build/$MP3_NAME" build/qc.json build/reference_profile.json \
  --repo "$GITHUB_REPOSITORY" --target main \
  --title "GTA VI - Vice City — Original Track ${PAD}" \
  --notes "Original 5-minute retro-futurist neon night-drive track. Pure text-to-audio; supplied reference recordings were not used as model input. Passed automated technical Quality Gate."

export RELEASE_TAG="$TAG"
export MP3_NAME="$MP3_NAME"

for push_attempt in 1 2 3 4 5; do
  git fetch origin main
  git reset --hard origin/main
  python scripts/publish_gta_vice_city.py
  git add control/music-library.json control/gta-vi-vice-city
  git diff --cached --quiet || git commit -m "gta: append Vice City track $INDEX"
  if git push origin HEAD:main; then
    exit 0
  fi
  sleep 3
done

exit 1
