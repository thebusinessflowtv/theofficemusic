import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from kaggle_secrets import UserSecretsClient

REPO_URL = "https://github.com/thebusinessflowtv/theofficemusic.git"
REPO_DIR = Path("/kaggle/working/theofficemusic")
SA3_DIR = Path("/kaggle/working/stable-audio-3")
CONFIG_FILE = REPO_DIR / "config" / "reference_test.yaml"


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

    raise RuntimeError("HF_TOKEN is unavailable to the Kaggle calibration job.")


def main():
    hf_token = load_hf_token()
    os.environ["HF_TOKEN"] = hf_token
    os.environ["HUGGING_FACE_HUB_TOKEN"] = hf_token
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["SA3_TARGET_MODEL"] = "small-music"

    if REPO_DIR.exists():
        run(["git", "-C", str(REPO_DIR), "pull", "--ff-only"])
    else:
        run(["git", "clone", "--depth", "1", REPO_URL, str(REPO_DIR)])

    run(["bash", "scripts/bootstrap_kaggle.sh"], cwd=REPO_DIR)

    python_bin = SA3_DIR / ".venv" / "bin" / "python"
    if not python_bin.exists():
        raise RuntimeError(f"Stable Audio Python runtime not found at {python_bin}")

    batch_name = "reference-test-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_DIR / "src")

    run(
        [
            str(python_bin),
            str(REPO_DIR / "src" / "generate_tracks.py"),
            "--config",
            str(CONFIG_FILE),
            "--batch-name",
            batch_name,
            "--tracks",
            "1",
        ],
        cwd=REPO_DIR,
        env=env,
    )

    output_dir = Path("/kaggle/working/output")
    wavs = list(output_dir.glob("*.wav"))
    if not wavs:
        raise RuntimeError("Generation command finished without producing a WAV file.")

    print(f"Reference test complete. Generated WAV: {wavs[0].name}")


if __name__ == "__main__":
    main()
