#!/usr/bin/env bash
set -euo pipefail

SA3_DIR=/kaggle/working/stable-audio-3
SA3_REVISION=3a82c807b69cf4b7c5c05270011a5d5e47abac18

python -m pip install -q --upgrade uv
git clone https://github.com/Stability-AI/stable-audio-3.git "$SA3_DIR"
git -C "$SA3_DIR" checkout --detach "$SA3_REVISION"
cd "$SA3_DIR"
uv sync --locked --no-dev
VENV_PY="$SA3_DIR/.venv/bin/python"
uv pip install --python "$VENV_PY" pyyaml

# Use the model's native SDPA fallback. Flash Attention 2 does not support T4.
"$VENV_PY" - <<'PY'
import torch
import torch.nn.functional as F
from stable_audio_3.models import transformer
if not torch.cuda.is_available():
    raise SystemExit('GPU_ALLOCATION_FAILED: CUDA unavailable in generation runtime')
if torch.cuda.get_device_capability(0) < (7, 5):
    raise SystemExit('GPU_INCOMPATIBLE: this batch requires T4 or newer')
transformer.flex_attention_available = False
transformer.flex_attention_compiled = None
assert transformer.flash_attn_func is None, 'Unexpected Flash Attention installation'
q, k, v = [torch.randn(1, 2, 257, 64, device='cuda', dtype=torch.float16) for _ in range(3)]
actual = transformer._sliding_window_chunked_halo_sdpa(q, k, v, 17, 17, chunk_size=64)
mask = transformer._sliding_window_additive_mask(257, 257, 17, 17, q.device, q.dtype)
expected = F.scaled_dot_product_attention(q, k, v, attn_mask=mask)
assert torch.isfinite(actual).all().item(), 'Non-finite GPU attention output'
torch.testing.assert_close(actual, expected, atol=0.004, rtol=0.004)
print('GPU_SDPA_PREFLIGHT_OK:', torch.cuda.get_device_name(0), torch.__version__, flush=True)
PY
