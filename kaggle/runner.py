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
TRACK_COUNT = 5
TRACK_DURATION_SECONDS = 360
BATCH_PREFIX = "office"
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

    input_root = Path("/kaggle/input")
    if input_root.exists():
        for candidate in input_root.rglob("hf_token.txt"):
            token = candidate.read_text(encoding="utf-8").strip()
            if token:
                print("HF token loaded from private Kaggle input.")
                return token

    raise RuntimeError("HF_TOKEN is unavailable to the Kaggle generation job.")


def read_target_model() -> str:
    profile = REPO_DIR / "config" / "channel_profile.yaml"
    text = profile.read_text(encoding="utf-8")
    match = re.search(r"(?m)^\s*model:\s*[\"']?([^\"'\s#]+)", text)
    if not match:
        raise RuntimeError("Could not determine generation.model from channel_profile.yaml")
    return match.group(1)


def cleanup_working_tree() -> None:
    for path in (SA3_DIR, REPO_DIR):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


def request_seed(request_id: str, model: str) -> int:
    if not request_id or request_id == "UNSET":
        raise RuntimeError("REQUEST_ID was not injected by the production workflow")
    digest = hashlib.sha256(f"{request_id}:{model}:medium".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 2_000_000_000 + 1


def main():
    hf_token = load_hf_token()
    os.environ["HF_TOKEN"] = hf_token
    os.environ["HUGGING_FACE_HUB_TOKEN"] = hf_token
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    if REPO_DIR.exists():
        run(["git", "-C", str(REPO_DIR), "pull", "--ff-only"])
    else:
        run(["git", "clone", "--depth", "1", REPO_URL, str(REPO_DIR)])

    target_model = read_target_model()
    os.environ["SA3_TARGET_MODEL"] = target_model
    seed = request_seed(REQUEST_ID, target_model)
    print(f"Configured Stable Audio target model: {target_model}")
    print(f"Request ID: {REQUEST_ID}")
    print(f"Request-bound master seed: {seed}")
    print(f"Track count: {TRACK_COUNT}")
    print(f"Track duration: {TRACK_DURATION_SECONDS}s")

    run(["bash", "scripts/bootstrap_kaggle.sh"], cwd=REPO_DIR)

    python_bin = SA3_DIR / ".venv" / "bin" / "python"
    if not python_bin.exists():
        raise RuntimeError(f"Stable Audio Python runtime not found at {python_bin}")

    batch_name = BATCH_PREFIX + "-" + REQUEST_ID[:8] + "-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_DIR / "src")

    run(
        [
            str(python_bin),
            str(REPO_DIR / "src" / "generate_tracks.py"),
            "--config",
            str(REPO_DIR / "config" / "channel_profile.yaml"),
            "--batch-name",
            batch_name,
            "--tracks",
            str(TRACK_COUNT),
            "--duration-seconds",
            str(TRACK_DURATION_SECONDS),
            "--master-seed",
            str(seed),
        ],
        cwd=REPO_DIR,
        env=env,
    )

    wavs = list(OUTPUT_DIR.glob("*.wav"))
    if len(wavs) != TRACK_COUNT:
        raise RuntimeError(
            f"Expected {TRACK_COUNT} WAV files but generated {len(wavs)}."
        )

    (OUTPUT_DIR / "request_id.txt").write_text(REQUEST_ID + "\n", encoding="utf-8")
    (OUTPUT_DIR / "generation_request.json").write_text(json.dumps({
        "request_id": REQUEST_ID,
        "model": target_model,
        "master_seed": seed,
        "track_count": TRACK_COUNT,
        "track_duration_seconds": TRACK_DURATION_SECONDS,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")

    cleanup_working_tree()
    print(f"Generation complete. Produced {len(wavs)} WAV files for request {REQUEST_ID}.")
    print("Cleaned temporary model/repository files before Kaggle output export.")


if __name__ == "__main__":
    main()
