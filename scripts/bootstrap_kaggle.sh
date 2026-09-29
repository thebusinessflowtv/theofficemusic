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
uv pip install pyyaml ninja

# Stable Audio 3 Medium requires Flash Attention 2. Use the prebuilt wheel
# documented by Stability AI for cu126 + torch 2.7 + Python 3.10.
uv pip install "https://github.com/mjun0812/flash-attention-prebuild-wheels/releases/download/v0.7.16/flash_attn-2.6.3+cu126torch2.7-cp310-cp310-linux_x86_64.whl"
uv sync --inexact

uv run python - <<'PY'
import torch
import flash_attn
from flash_attn import flash_attn_func

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
