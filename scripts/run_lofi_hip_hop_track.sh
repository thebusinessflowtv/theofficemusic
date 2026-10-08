#!/usr/bin/env bash
set -euo pipefail
mkdir -p build
PAD="$(printf '%02d' "$INDEX")"
if test -f "control/lofi-hip-hop/$PAD.json"; then
  echo "Lofi Hip Hop track $INDEX is already approved. Skipping duplicate generation."
  exit 0
fi
test -n "${KAGGLE_USERNAME:-}"
test -n "${KAGGLE_API_TOKEN:-}"
test -n "${HF_TOKEN:-}"

SECRET_DIR="$(mktemp -d)"
trap 'rm -rf "$SECRET_DIR"' EXIT
printf '%s' "$HF_TOKEN" > "$SECRET_DIR/hf_token.txt"
chmod 600 "$SECRET_DIR/hf_token.txt"
python - "$SECRET_DIR" <<'PY'
import os,json,pathlib,sys
p=pathlib.Path(sys.argv[1])
(p/"dataset-metadata.json").write_text(json.dumps({
  "title":"Peter Lofi Music Generation Secrets",
  "id":f"{os.environ['KAGGLE_USERNAME']}/peter-lofi-gaming-dj30-secrets",
  "licenses":[{"name":"other"}],
  "description":"Private CI credential input for Peter Lofi original tracks."
},indent=2),encoding="utf-8")
PY
DATASET="$KAGGLE_USERNAME/$SECRET_DATASET_SLUG"
if kaggle datasets status "$DATASET" >/dev/null 2>&1; then
  kaggle datasets version -p "$SECRET_DIR" -m "Refresh Lofi Hip Hop production credentials" -q
else
  kaggle datasets create -p "$SECRET_DIR" --private -q
fi
READY=0
for n in $(seq 1 60); do
  STATUS="$(kaggle datasets status "$DATASET" 2>&1 || true)"
  echo "$STATUS"
  if printf '%s' "$STATUS" | grep -Eqi '\bready\b'; then READY=1; break; fi
  if printf '%s' "$STATUS" | grep -Eqi 'error|failed|denied|not found'; then exit 1; fi
  sleep 5
done
test "$READY" -eq 1

APPROVED=0
for VARIANT_ATTEMPT in 1 2 3 4; do
  echo "Lofi Hip Hop $INDEX/36 — attempt $VARIANT_ATTEMPT — 300 seconds — independent musical identity"
  rm -rf generated
  rm -f build/reference_profile.json build/qc.json build/diversity.json build/approved.mp3
  python scripts/build_lofi_hip_hop_profile.py --index "$INDEX" --variant-attempt "$VARIANT_ATTEMPT" --out build/reference_profile.json

  TMP="$(mktemp -d)"
  cp kaggle/runner_gta_vice_city.py "$TMP/runner.py"
  REQUEST_ID="lofi-hip-hop-$INDEX-v$VARIANT_ATTEMPT-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"
  PROFILE_B64="$(base64 -w0 build/reference_profile.json)"

  python - "$TMP/runner.py" "$REQUEST_ID" "$INDEX" "$VARIANT_ATTEMPT" "$PROFILE_B64" <<'PY'
import pathlib,re,sys
p=pathlib.Path(sys.argv[1]); req=sys.argv[2]; idx=int(sys.argv[3]); attempt=int(sys.argv[4]); payload=sys.argv[5]
t=p.read_text(encoding="utf-8")
t=re.sub(r'REQUEST_ID = "[^"]*"',f'REQUEST_ID = "{req}"',t,count=1)
t=re.sub(r'TRACK_INDEX = \d+',f'TRACK_INDEX = {idx}',t,count=1)
t=re.sub(r'ATTEMPT = \d+',f'ATTEMPT = {attempt}',t,count=1)
t=re.sub(r'PROFILE_PAYLOAD_B64 = "[^"]*"',f'PROFILE_PAYLOAD_B64 = "{payload}"',t,count=1)
t=t.replace('gta-vice-city','lofi-hip-hop').replace('GTA VI - Vice City','Lofi Hip Hop')
t=t.replace(
  'Driving original retro-futurist neon night music with a consistent dance pulse and cinematic coastal atmosphere.',
  'Warm original jazzy instrumental lofi hip hop music with melodic Rhodes chords, soulful bass and relaxed swung boom bap drums for study and work.'
)
p.write_text(t,encoding="utf-8")
PY
  KERNEL_NAME="peter-lofi-lofi36-$INDEX-v$VARIANT_ATTEMPT-$GITHUB_RUN_ID"
  python - "$TMP" "$KERNEL_NAME" <<'PY'
import json,os,pathlib,sys
d=pathlib.Path(sys.argv[1]); name=sys.argv[2]
(d/"kernel-metadata.json").write_text(json.dumps({
  "id":f"{os.environ['KAGGLE_USERNAME']}/{name}",
  "title":name,"code_file":"runner.py","language":"python","kernel_type":"script",
  "is_private":True,"enable_gpu":True,"machine_shape":"NvidiaTeslaT4",
  "enable_internet":True,
  "dataset_sources":[f"{os.environ['KAGGLE_USERNAME']}/peter-lofi-gaming-dj30-secrets"],
  "competition_sources":[],"kernel_sources":[]
},indent=2),encoding="utf-8")
PY

  if ! PUSH_OUTPUT="$(kaggle kernels push -p "$TMP" --timeout 7200 --accelerator NvidiaTeslaT4 2>&1)"; then
    printf '%s\n' "$PUSH_OUTPUT"
    rm -rf "$TMP"
    continue
  fi
  printf '%s\n' "$PUSH_OUTPUT"
  KERNEL_REF="$(printf '%s\n' "$PUSH_OUTPUT" | python -c 'import re,sys; m=re.search(r"https://www[.]kaggle[.]com/code/([A-Za-z0-9_-]+/[A-Za-z0-9_-]+)",sys.stdin.read());print(m.group(1) if m else "")')"
  if test -z "$KERNEL_REF"; then
    echo "No canonical Kaggle kernel reference in submit response"
    rm -rf "$TMP"
    continue
  fi
  sleep 20
  COMPLETE=0
  for n in $(seq 1 120); do
    STATUS="$(kaggle kernels status "$KERNEL_REF" 2>&1 || true)"
    echo "$STATUS"
    if echo "$STATUS" | grep -Eqi 'complete|completed'; then COMPLETE=1; break; fi
    if echo "$STATUS" | grep -Eqi 'error|failed|cancel|denied|cannot access|not found'; then
      kaggle kernels logs "$KERNEL_REF" 2>&1 | tail -n 100 || true
      break
    fi
    sleep 60
  done
  if test "$COMPLETE" -ne 1; then rm -rf "$TMP"; continue; fi

  mkdir -p generated
  if ! kaggle kernels output "$KERNEL_REF" -p generated; then rm -rf "$TMP"; continue; fi
  MARKER="$(find generated -type f -name request_id.txt -print -quit)"
  WAV="$(find generated -type f -name '*.wav' -print -quit)"
  if test -z "$MARKER" || test -z "$WAV"; then rm -rf "$TMP"; continue; fi
  if test "$(tr -d '\r\n ' < "$MARKER")" != "$REQUEST_ID"; then
    echo "Request-id mismatch: refusing stale Kaggle output"
    rm -rf "$TMP"
    continue
  fi

  if ! python scripts/quality_gate_audio.py "$WAV" \
      --reference-profile build/reference_profile.json \
      --out build/qc.json \
      --normalized build/approved.mp3; then
    echo "Audio technical quality rejected attempt $VARIANT_ATTEMPT"
    rm -rf "$TMP"
    continue
  fi
  git fetch origin main
  git show origin/main:control/music-library.json > build/current-music-library.json
  if ! python scripts/audio_diversity_gate.py build/approved.mp3 \
      --library build/current-music-library.json \
      --playlist-key lofi-hip-hop \
      --out build/diversity.json; then
    echo "Intro diversity rejected attempt $VARIANT_ATTEMPT"
    rm -rf "$TMP"
    continue
  fi
  APPROVED=1
  rm -rf "$TMP"
  break
done
test "$APPROVED" -eq 1 || { echo "No passing distinct musical candidate for track $INDEX"; exit 1; }

MP3_NAME="peter-lofi-lofi-hip-hop-$(printf '%03d' "$INDEX").mp3"
mv build/approved.mp3 "build/$MP3_NAME"
TAG="peter-lofi-lofi-hip-hop-$(printf '%03d' "$INDEX")-$GITHUB_RUN_ID"
gh release create "$TAG" "build/$MP3_NAME" build/qc.json build/diversity.json build/reference_profile.json \
  --repo "$GITHUB_REPOSITORY" --target main \
  --title "Peter Lofi — Lofi Hip Hop $(printf '%03d' "$INDEX")" \
  --notes "Original instrumental five-minute Lofi Hip Hop track; unique introduction; passed technical and 45-second audio diversity gate."
export RELEASE_TAG="$TAG"
export MP3_NAME
for push_attempt in 1 2 3 4 5; do
  git fetch origin main
  git reset --hard origin/main
  python scripts/publish_lofi_hip_hop.py
  git add control/music-library.json control/lofi-hip-hop
  git diff --cached --quiet || git commit -m "lofi: approve and catalog Hip Hop track $INDEX/36"
  if git push origin HEAD:main; then
    echo "LOFI_TRACK_PUBLISHED=$INDEX"
    exit 0
  fi
  sleep 3
done
echo "Could not persist approved Lofi Hip Hop track after retries"
exit 1
