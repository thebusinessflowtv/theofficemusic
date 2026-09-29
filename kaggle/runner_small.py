import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from kaggle_secrets import UserSecretsClient

REPO_URL = "https://github.com/thebusinessflowtv/theofficemusic.git"
REPO_DIR = Path("/kaggle/working/theofficemusic")
SA3_DIR = Path("/kaggle/working/stable-audio-3")
OUTPUT_DIR = Path("/kaggle/working/output")
TRACK_COUNT = 30
TRACK_DURATION_SECONDS = 120
BATCH_PREFIX = "office-small"
REQUEST_ID = "UNSET"


def run(cmd, cwd=None, env=None):
    print("$", " ".join(map(str, cmd)))
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def load_hf_token() -> str:
    try:
        token = UserSecretsClient().get_secret("HF_TOKEN")
        if token:
            return token.strip()
    except Exception:
        pass
    for candidate in Path("/kaggle/input").rglob("hf_token.txt") if Path("/kaggle/input").exists() else []:
        token = candidate.read_text(encoding="utf-8").strip()
        if token:
            return token
    raise RuntimeError("HF_TOKEN is unavailable")


def request_seed(request_id: str) -> int:
    if not request_id or request_id == "UNSET":
        raise RuntimeError("REQUEST_ID was not injected by the production workflow")
    digest = hashlib.sha256(f"{request_id}:small-music:fallback".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 2_000_000_000 + 1


def main():
    token = load_hf_token()
    os.environ["HF_TOKEN"] = token
    os.environ["HUGGING_FACE_HUB_TOKEN"] = token
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["SA3_TARGET_MODEL"] = "small-music"

    for p in (REPO_DIR, SA3_DIR, OUTPUT_DIR):
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
    run(["git", "clone", "--depth", "1", REPO_URL, str(REPO_DIR)])

    profile = REPO_DIR / "config" / "channel_profile.yaml"
    text = profile.read_text(encoding="utf-8")
    text = re.sub(r'(?m)^(\s*model:\s*)["\']?[^"\'\s#]+["\']?', r'\1"small-music"', text, count=1)
    text = re.sub(r'(?m)^(\s*track_duration_seconds:\s*)\d+', rf'\g<1>{TRACK_DURATION_SECONDS}', text, count=1)
    profile.write_text(text, encoding="utf-8")

    run(["bash", "scripts/bootstrap_kaggle.sh"], cwd=REPO_DIR)
    python_bin = SA3_DIR / ".venv" / "bin" / "python"
    if not python_bin.exists():
        raise RuntimeError("Stable Audio runtime not found")

    seed = request_seed(REQUEST_ID)
    batch_name = BATCH_PREFIX + "-" + REQUEST_ID[:8] + "-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_DIR / "src")
    print(f"Request ID: {REQUEST_ID}")
    print(f"Request-bound master seed: {seed}")
    run([
        str(python_bin), str(REPO_DIR / "src" / "generate_tracks.py"),
        "--config", str(profile),
        "--batch-name", batch_name,
        "--tracks", str(TRACK_COUNT),
        "--duration-seconds", str(TRACK_DURATION_SECONDS),
        "--master-seed", str(seed),
    ], cwd=REPO_DIR, env=env)

    wavs = list(OUTPUT_DIR.glob("*.wav"))
    if len(wavs) != TRACK_COUNT:
        raise RuntimeError(f"Expected {TRACK_COUNT} WAV files, generated {len(wavs)}")

    (OUTPUT_DIR / "request_id.txt").write_text(REQUEST_ID + "\n", encoding="utf-8")
    (OUTPUT_DIR / "generation_request.json").write_text(json.dumps({
        "request_id": REQUEST_ID,
        "model": "small-music",
        "master_seed": seed,
        "track_count": TRACK_COUNT,
        "track_duration_seconds": TRACK_DURATION_SECONDS,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")

    shutil.rmtree(SA3_DIR, ignore_errors=True)
    shutil.rmtree(REPO_DIR, ignore_errors=True)
    print(f"Fallback generation complete: {len(wavs)} fresh tracks for request {REQUEST_ID}")


if __name__ == "__main__":
    main()
