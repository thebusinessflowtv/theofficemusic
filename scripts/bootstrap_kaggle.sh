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

# The official repository pins Python 3.10, torch 2.7.1 and CUDA 12.6.
uv sync

# IMPORTANT: Kaggle's system Python is currently 3.12, while the Stable Audio
# project creates its own Python 3.10 virtualenv. Force every extra dependency
# into that venv so the cp310 Flash Attention wheel is installed in the correct
# interpreter instead of /usr Python 3.12.
VENV_PY="$SA3_DIR/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  echo "Stable Audio virtualenv Python not found at $VENV_PY"
  exit 1
fi

uv pip install --python "$VENV_PY" pyyaml ninja

# Stable Audio 3 Medium requires Flash Attention 2. Use the prebuilt wheel
# for CUDA 12.6 + torch 2.7 + Python 3.10.
uv pip install --python "$VENV_PY" \
  "https://github.com/mjun0812/flash-attention-prebuild-wheels/releases/download/v0.7.16/flash_attn-2.6.3+cu126torch2.7-cp310-cp310-linux_x86_64.whl"

# Do not invoke `uv run` here because it may re-sync the environment and remove
# the explicitly installed Flash Attention wheel. Execute the venv interpreter
# directly instead.
"$VENV_PY" - <<'PY'
import sys
import torch
import flash_attn
from flash_attn import flash_attn_func

print("Python:", sys.version)
if not torch.cuda.is_available():
    raise SystemExit("CUDA GPU not available")

major, minor = torch.cuda.get_device_capability(0)
print("GPU:", torch.cuda.get_device_name(0))
print("Compute capability:", f"{major}.{minor}")
print("CUDA:", torch.version.cuda)
print("Torch:", torch.__version__)
print("Flash Attention:", flash_attn.__version__)

if major < 8:
    raise SystemExit(
        "GPU compute capability is below 8.0. Stable Audio 3 Medium + Flash Attention 2 requires an Ampere-or-newer GPU in this pipeline."
    )
PY
