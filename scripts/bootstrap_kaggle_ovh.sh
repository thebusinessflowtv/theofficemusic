#!/usr/bin/env bash
set -euo pipefail

WORK=/kaggle/working
BUNDLE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR_SRC="$BUNDLE_ROOT/vendor/stable-audio-3"
SA3_DIR="$WORK/stable-audio-3"
TARGET_MODEL="${SA3_TARGET_MODEL:-small-music}"

python -m pip install -q --upgrade pip uv

test -f "$VENDOR_SRC/pyproject.toml" || {
  echo "Bundled Stable Audio 3 source is missing. MediaForge will not fall back to GitHub."
  exit 1
}

rm -rf "$SA3_DIR"
cp -a "$VENDOR_SRC" "$SA3_DIR"
rm -rf "$SA3_DIR/.git" || true
cd "$SA3_DIR"

echo "Stable Audio target model: $TARGET_MODEL"
echo "Runtime source: MediaForge OVH-vendored bundle (no GitHub fetch)"

if [[ "$TARGET_MODEL" == small-* ]]; then
  uv sync --no-install-package torch --no-install-package torchaudio
  VENV_PY="$SA3_DIR/.venv/bin/python"

  if [[ "${SA3_PREFER_CUDA:-0}" =~ ^(1|true|yes|on)$ ]] && command -v nvidia-smi >/dev/null 2>&1; then
    uv pip install --python "$VENV_PY"       torch==2.7.1 torchaudio==2.7.1       --index-url https://download.pytorch.org/whl/cu126
  else
    uv pip install --python "$VENV_PY"       torch==2.7.1 torchaudio==2.7.1       --index-url https://download.pytorch.org/whl/cpu
  fi
  uv pip install --python "$VENV_PY" pyyaml
else
  uv sync
  VENV_PY="$SA3_DIR/.venv/bin/python"
  uv pip install --python "$VENV_PY" pyyaml ninja

  "$VENV_PY" - <<'PY'
import torch
if not torch.cuda.is_available():
    raise SystemExit("Stable Audio 3 Medium requested but CUDA is unavailable")
major, minor = torch.cuda.get_device_capability(0)
if major < 8:
    raise SystemExit("Stable Audio 3 Medium requires an Ampere-or-newer GPU")
PY

  uv pip install --python "$VENV_PY"     "https://github.com/mjun0812/flash-attention-prebuild-wheels/releases/download/v0.7.16/flash_attn-2.6.3+cu126torch2.7-cp310-cp310-linux_x86_64.whl"
fi
