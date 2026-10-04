import base64
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from kaggle_secrets import UserSecretsClient

REPO_URL = "https://github.com/thebusinessflowtv/theofficemusic.git"
REPO_DIR = Path("/kaggle/working/theofficemusic")
SA3_DIR = Path("/kaggle/working/stable-audio-3")
OUTPUT_DIR = Path("/kaggle/working/output")
REQUEST_ID = "UNSET"
REFERENCE_INDEX = 0
ATTEMPT = 1
REFERENCE_PAYLOAD_B64 = "UNSET"


def run(cmd, cwd=None, env=None):
    print("$", " ".join(map(str, cmd)))
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def load_hf_token():
    try:
        token = UserSecretsClient().get_secret("HF_TOKEN")
        if token:
            return token.strip()
    except Exception:
        pass
    root = Path("/kaggle/input")
    if root.exists():
        for p in root.rglob("hf_token.txt"):
            token = p.read_text(encoding="utf-8").strip()
            if token:
                return token
    raise RuntimeError("HF_TOKEN unavailable")


def request_seed():
    if REQUEST_ID == "UNSET" or REFERENCE_INDEX <= 0:
        raise RuntimeError("Reference generation constants were not injected")
    raw = f"{REQUEST_ID}:ref:{REFERENCE_INDEX}:attempt:{ATTEMPT}:stable-audio-3-medium"
    return int.from_bytes(hashlib.sha256(raw.encode()).digest()[:8], "big") % 2_000_000_000 + 1


def main():
    payload = json.loads(base64.b64decode(REFERENCE_PAYLOAD_B64).decode("utf-8"))
    profile = payload["profile"]

    token = load_hf_token()
    os.environ["HF_TOKEN"] = token
    os.environ["HUGGING_FACE_HUB_TOKEN"] = token
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["SA3_TARGET_MODEL"] = "medium"

    for p in (REPO_DIR, SA3_DIR, OUTPUT_DIR):
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)

    run(["git", "clone", "--depth", "1", REPO_URL, str(REPO_DIR)])
    prompt_file = REPO_DIR / "src" / "prompt_engine.py"
    text = prompt_file.read_text()
    text = text.replace("Elegant background music that remains interesting without demanding attention.",
                        "Very energetic exciting dance music with strong momentum throughout.")
    prompt_file.write_text(text)
    runtime = REPO_DIR / "config" / "runtime_gaming_reference_profile.json"
    runtime.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")

    run(["bash", "scripts/bootstrap_kaggle.sh"], cwd=REPO_DIR)
    python_bin = SA3_DIR / ".venv" / "bin" / "python"
    if not python_bin.exists():
        raise RuntimeError("Stable Audio 3 runtime not found")

    seed = request_seed()
    batch = f"gaming-ref-{REFERENCE_INDEX:03d}-a{ATTEMPT}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_DIR / "src")

    run([
        str(python_bin),
        str(REPO_DIR / "src" / "generate_tracks.py"),
        "--config", str(runtime),
        "--batch-name", batch,
        "--tracks", "1",
        "--duration-seconds", "300",
        "--master-seed", str(seed),
    ], cwd=REPO_DIR, env=env)

    wavs = sorted(OUTPUT_DIR.glob("*.wav"))
    if len(wavs) != 1:
        raise RuntimeError(f"Expected exactly 1 WAV, got {len(wavs)}")

    (OUTPUT_DIR / "request_id.txt").write_text(REQUEST_ID + "\n", encoding="utf-8")
    (OUTPUT_DIR / "reference_index.txt").write_text(str(REFERENCE_INDEX) + "\n", encoding="utf-8")
    (OUTPUT_DIR / "generation_request.json").write_text(json.dumps({
        "request_id": REQUEST_ID,
        "reference_index": REFERENCE_INDEX,
        "attempt": ATTEMPT,
        "model": "medium",
        "master_seed": seed,
        "track_duration_seconds": 300,
        "pure_text_to_audio": True,
        "reference_audio_used": False,
        "reference_artist_or_title_in_prompt": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    shutil.rmtree(SA3_DIR, ignore_errors=True)
    shutil.rmtree(REPO_DIR, ignore_errors=True)
    print(f"Gaming reference {REFERENCE_INDEX}/30 attempt {ATTEMPT} complete")


if __name__ == "__main__":
    main()
