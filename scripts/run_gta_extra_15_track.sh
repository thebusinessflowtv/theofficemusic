#!/usr/bin/env bash
set -euo pipefail

mkdir -p build
if test -f "control/gta-vi-extra-15/$(printf '%02d' "$INDEX").json"; then
  echo "Track $INDEX already completed; skipping."
  exit 0
fi

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
  kaggle datasets version -p "$SECRET_DIR" -m "Refresh HF token for GTA VI - Vice City revision 2" -q
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

APPROVED=0
MAX_VARIANTS=4

# Recover the already-generated first approved candidate from the failed 2026-10-06
# Extra 15 run. That run produced technically valid/diverse audio, but an inverted
# shell condition mislabeled exit code 0 as rejection. Revalidate against the
# CURRENT playlist so recovered extras are also checked against extras inserted
# earlier in this corrected run.
RECOVERY_RUN_ID="37416684072"
RECOVERY_KERNEL="$KAGGLE_USERNAME/peter-lofi-gta-extra15-$INDEX-v1-$RECOVERY_RUN_ID"
echo "Trying approved-audio recovery from $RECOVERY_KERNEL"
rm -rf generated
mkdir -p generated
if kaggle kernels output "$RECOVERY_KERNEL" -p generated >/tmp/gta-extra-recovery.log 2>&1; then
  cat /tmp/gta-extra-recovery.log
  RECOVERY_WAV="$(find generated -type f -name '*.wav' -print -quit)"
  RECOVERY_INDEX_FILE="$(find generated -type f -name 'track_index.txt' -print -quit)"
  if test -n "$RECOVERY_WAV" && test -n "$RECOVERY_INDEX_FILE" && test "$(tr -d '\r\n ' < "$RECOVERY_INDEX_FILE")" = "$INDEX"; then
    rm -f build/reference_profile.json build/qc.json build/diversity.json build/approved.mp3
    python scripts/build_gta_extra_15_profile.py \
      --index "$INDEX" \
      --variant-attempt 1 \
      --out build/reference_profile.json

    if python scripts/quality_gate_audio.py "$RECOVERY_WAV" \
        --reference-profile build/reference_profile.json \
        --out build/qc.json \
        --normalized build/approved.mp3; then
      if git fetch origin main; then
        git show origin/main:control/music-library.json > build/current-music-library.json
        if python scripts/audio_diversity_gate.py build/approved.mp3 \
            --library build/current-music-library.json \
            --playlist-key gta-vi-vice-city \
            --out build/diversity.json; then
          echo "Recovered candidate for track $INDEX passed current QC + diversity gates."
          APPROVED=1
        else
          echo "Recovered candidate for track $INDEX is too close to the updated playlist; generate a fresh replacement."
        fi
      fi
    fi
  fi
else
  cat /tmp/gta-extra-recovery.log || true
fi

if test "$APPROVED" -ne 1; then
for VARIANT_ATTEMPT in $(seq 1 "$MAX_VARIANTS"); do
  echo "=== GTA extra family track $INDEX | diversity attempt $VARIANT_ATTEMPT/$MAX_VARIANTS ==="
  rm -rf generated
  rm -f build/reference_profile.json build/qc.json build/diversity.json build/approved.mp3

  python scripts/build_gta_extra_15_profile.py \
    --index "$INDEX" \
    --variant-attempt "$VARIANT_ATTEMPT" \
    --out build/reference_profile.json

  TMP="$(mktemp -d)"
  cp kaggle/runner_gta_vice_city.py "$TMP/runner.py"
  REQUEST_ID="gta-extra15-$INDEX-v$VARIANT_ATTEMPT-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"
  PROFILE_B64="$(base64 -w0 build/reference_profile.json)"

  python - "$TMP/runner.py" "$REQUEST_ID" "$INDEX" "$VARIANT_ATTEMPT" "$PROFILE_B64" <<'PY'
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

  KERNEL_NAME="peter-lofi-gta-extra15-$INDEX-v$VARIANT_ATTEMPT-$GITHUB_RUN_ID"
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
    rm -rf "$TMP"
    continue
  fi
  printf '%s\n' "$PUSH_OUTPUT"

  KERNEL_REF="$(printf '%s\n' "$PUSH_OUTPUT" | python -c 'import re,sys; m=re.search(r"https://www[.]kaggle[.]com/code/([A-Za-z0-9_-]+/[A-Za-z0-9_-]+)",sys.stdin.read()); print(m.group(1) if m else "")')"
  if test -z "$KERNEL_REF"; then
    rm -rf "$TMP"
    continue
  fi
  sleep 20

  COMPLETE=0
  for i in $(seq 1 120); do
    STATUS="$(kaggle kernels status "$KERNEL_REF" 2>&1 || true)"
    echo "$STATUS"
    if echo "$STATUS" | grep -Eqi 'complete|completed'; then
      COMPLETE=1
      break
    fi
    if echo "$STATUS" | grep -Eqi 'error|failed|cancel|denied|Cannot access|not found'; then
      kaggle kernels logs "$KERNEL_REF" 2>&1 | tail -n 250 || true
      break
    fi
    sleep 60
  done
  if test "$COMPLETE" -ne 1; then
    rm -rf "$TMP"
    continue
  fi

  rm -rf generated
  mkdir -p generated
  if ! kaggle kernels output "$KERNEL_REF" -p generated; then
    rm -rf "$TMP"
    continue
  fi

  MARKER="$(find generated -type f -name request_id.txt -print -quit)"
  WAV="$(find generated -type f -name '*.wav' -print -quit)"
  if test -z "$MARKER" || test -z "$WAV"; then
    rm -rf "$TMP"
    continue
  fi
  if test "$(tr -d '\r\n ' < "$MARKER")" != "$REQUEST_ID"; then
    rm -rf "$TMP"
    continue
  fi

  if ! python scripts/quality_gate_audio.py "$WAV" \
      --reference-profile build/reference_profile.json \
      --out build/qc.json \
      --normalized build/approved.mp3; then
    echo "Technical QC rejected variant $VARIANT_ATTEMPT"
    rm -rf "$TMP"
    continue
  fi

  if ! git fetch origin main; then
    echo "Could not refresh current GTA VI library before diversity check."
    rm -rf "$TMP"
    continue
  fi
  git show origin/main:control/music-library.json > build/current-music-library.json

  if ! python scripts/audio_diversity_gate.py build/approved.mp3 \
      --library build/current-music-library.json \
      --playlist-key gta-vi-vice-city \
      --out build/diversity.json; then
    echo "Diversity gate rejected variant $VARIANT_ATTEMPT; generating a structurally different replacement."
    rm -rf "$TMP"
    continue
  fi

  echo "Diversity gate approved variant $VARIANT_ATTEMPT."
  APPROVED=1
  rm -rf "$TMP"
  break
done
fi

if test "$APPROVED" -ne 1; then
  echo "Track $INDEX failed to produce a sufficiently distinct approved version after $MAX_VARIANTS attempts."
  exit 1
fi

PAD="$(printf '%03d' "$INDEX")"
MP3_NAME="peter-lofi-gta-extra15-$PAD.mp3"
mv build/approved.mp3 "build/$MP3_NAME"
TAG="peter-lofi-gta-extra15-$PAD-$GITHUB_RUN_ID"

if gh release view "$TAG" --repo "$GITHUB_REPOSITORY" >/dev/null 2>&1; then
  echo "Release $TAG already exists; refreshing assets instead of failing the queue."
  gh release upload "$TAG" "build/$MP3_NAME" build/qc.json build/diversity.json build/reference_profile.json \
    --repo "$GITHUB_REPOSITORY" --clobber
else
  gh release create "$TAG" "build/$MP3_NAME" build/qc.json build/diversity.json build/reference_profile.json \
    --repo "$GITHUB_REPOSITORY" --target main \
    --title "GTA VI - Vice City Extra — Track $PAD" \
    --notes "Original 5-minute GTA VI family track based only on broad musical traits. Source recording was not used as model input. Passed technical QC and cross-track audio diversity gate."
fi

export RELEASE_TAG="$TAG"
export MP3_NAME="$MP3_NAME"

for push_attempt in 1 2 3 4 5; do
  git fetch origin main
  git reset --hard origin/main
  python scripts/publish_gta_extra_15.py
  git add control/music-library.json control/gta-vi-extra-15
  git diff --cached --quiet || git commit -m "gta: append diverse Vice City R2 track $INDEX"
  if git push origin HEAD:main; then
    exit 0
  fi
  sleep 3
done

exit 1
