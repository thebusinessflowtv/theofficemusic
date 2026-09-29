#!/usr/bin/env bash
set -euo pipefail

WORK=/kaggle/working
SA3_DIR="$WORK/stable-audio-3"
TARGET_MODEL="${SA3_TARGET_MODEL:-small-music}"

python -m pip install -q --upgrade pip uv

if [ ! -d "$SA3_DIR/.git" ]; then
  git clone --depth 1 https://github.com/Stability-AI/stable-audio-3.git "$SA3_DIR"
else
  git -C "$SA3_DIR" pull --ff-only
fi

cd "$SA3_DIR"

echo "Stable Audio target model: $TARGET_MODEL"

if [[ "$TARGET_MODEL" == small-* ]]; then
  # Small-Music is officially CPU-capable. Use the CPU PyTorch build so this
  # path is completely independent of Kaggle GPU provisioning.
  uv sync --no-install-package torch --no-install-package torchaudio
  VENV_PY="$SA3_DIR/.venv/bin/python"
  uv pip install --python "$VENV_PY" \
    torch==2.7.1 torchaudio==2.7.1 \
    --index-url https://download.pytorch.org/whl/cpu
  uv pip install --python "$VENV_PY" pyyaml

  "$VENV_PY" - <<'PY'
import sys
import torch
print("Python:", sys.version)
print("Torch:", torch.__version__)
print("Calibration runtime: CPU")
PY
else
  # Medium requires CUDA + Flash Attention 2 on an Ampere-or-newer GPU.
  uv sync
  VENV_PY="$SA3_DIR/.venv/bin/python"
  uv pip install --python "$VENV_PY" pyyaml ninja

  "$VENV_PY" - <<'PY'
import torch
if not torch.cuda.is_available():
    raise SystemExit("Stable Audio 3 Medium requested but CUDA is unavailable")
major, minor = torch.cuda.get_device_capability(0)
print("GPU:", torch.cuda.get_device_name(0))
print("Compute capability:", f"{major}.{minor}")
if major < 8:
    raise SystemExit("Stable Audio 3 Medium requires an Ampere-or-newer GPU")
PY

  uv pip install --python "$VENV_PY" \
    "https://github.com/mjun0812/flash-attention-prebuild-wheels/releases/download/v0.7.16/flash_attn-2.6.3+cu126torch2.7-cp310-cp310-linux_x86_64.whl"

  "$VENV_PY" - <<'PY'
import flash_attn
import torch
print("Torch:", torch.__version__)
print("Flash Attention:", flash_attn.__version__)
PY
fi
