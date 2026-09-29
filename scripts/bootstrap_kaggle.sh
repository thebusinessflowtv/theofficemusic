#!/usr/bin/env bash
set -euo pipefail

WORK=/kaggle/working
SA3_DIR="$WORK/stable-audio-3"

python -m pip install -q --upgrade pip uv

if [ ! -d "$SA3_DIR/.git" ]; then
  git clone --depth 1 https://github.com/Stability-AI/stable-audio-3.git "$SA3_DIR"
else
  git -C "$SA3_DIR" pull --ff-only
fi

cd "$SA3_DIR"

# Stable Audio 3 pins Python 3.10 / torch 2.7.1 in its project environment.
uv sync

VENV_PY="$SA3_DIR/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  echo "Stable Audio virtualenv Python not found at $VENV_PY"
  exit 1
fi

uv pip install --python "$VENV_PY" pyyaml ninja

GPU_MAJOR="$($VENV_PY - <<'PY'
import torch
if not torch.cuda.is_available():
    print(-1)
else:
    print(torch.cuda.get_device_capability(0)[0])
PY
)"

if [ "$GPU_MAJOR" -ge 8 ]; then
  echo "Ampere-or-newer GPU detected. Installing Flash Attention 2 for Medium support."
  uv pip install --python "$VENV_PY" \
    "https://github.com/mjun0812/flash-attention-prebuild-wheels/releases/download/v0.7.16/flash_attn-2.6.3+cu126torch2.7-cp310-cp310-linux_x86_64.whl"
else
  echo "Pre-Ampere GPU detected. Skipping Flash Attention; Small-Music remains supported."
fi

"$VENV_PY" - <<'PY'
import sys
import torch

print("Python:", sys.version)
if not torch.cuda.is_available():
    raise SystemExit("CUDA GPU not available")

major, minor = torch.cuda.get_device_capability(0)
print("GPU:", torch.cuda.get_device_name(0))
print("Compute capability:", f"{major}.{minor}")
print("CUDA:", torch.version.cuda)
print("Torch:", torch.__version__)

if major >= 8:
    import flash_attn
    print("Flash Attention:", flash_attn.__version__)
else:
    print("Flash Attention: skipped (not required for Small-Music)")
PY
