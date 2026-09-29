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

# Small-Music does not require Flash Attention and is compatible with Kaggle T4.
uv sync
uv pip install pyyaml
uv sync --inexact

uv run python - <<'PY'
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
PY
