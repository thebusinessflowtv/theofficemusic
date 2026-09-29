import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from kaggle_secrets import UserSecretsClient

REPO_URL = "https://github.com/thebusinessflowtv/theofficemusic.git"
REPO_DIR = Path("/kaggle/working/theofficemusic")
SA3_DIR = Path("/kaggle/working/stable-audio-3")
CONFIG_FILE = REPO_DIR / "config" / "reference_test_t4.yaml"


def run(cmd, cwd=None, env=None):
    print("$", " ".join(map(str, cmd)))
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def load_hf_token() -> str:
    try:
        token = UserSecretsClient().get_secret("HF_TOKEN")
        if token:
            return token.strip()
    except Exception as exc:
        print(f"Kaggle UserSecretsClient unavailable: {type(exc).__name__}")

    input_root = Path("/kaggle/input")
    if input_root.exists():
        for candidate in input_root.rglob("hf_token.txt"):
            token = candidate.read_text(encoding="utf-8").strip()
            if token:
                print(f"HF token loaded from private Kaggle input: {candidate.parent.name}")
                return token

    raise RuntimeError("HF_TOKEN unavailable")


def main():
    run(["nvidia-smi", "--query-gpu=name,compute_cap,memory.total", "--format=csv,noheader"])

    hf_token = load_hf_token()
    os.environ["HF_TOKEN"] = hf_token
    os.environ["HUGGING_FACE_HUB_TOKEN"] = hf_token
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    if REPO_DIR.exists():
        run(["git", "-C", str(REPO_DIR), "pull", "--ff-only"])
    else:
        run(["git", "clone", "--depth", "1", REPO_URL, str(REPO_DIR)])

    run(["bash", "scripts/bootstrap_kaggle_small.sh"], cwd=REPO_DIR)

    python_bin = SA3_DIR / ".venv" / "bin" / "python"
    batch_name = "reference-t4-small-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

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

    print("T4 Small-Music calibration complete. Output: /kaggle/working/output")


if __name__ == "__main__":
    main()
