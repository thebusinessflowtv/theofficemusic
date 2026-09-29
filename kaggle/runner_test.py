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
    # Preferred path when the secret was attached interactively in Kaggle.
    try:
        token = UserSecretsClient().get_secret("HF_TOKEN")
        if token:
            return token.strip()
    except Exception as exc:
        print(f"Kaggle UserSecretsClient unavailable for this pushed version: {type(exc).__name__}")

    # CI-pushed kernels do not inherit interactive Kaggle secrets. The GitHub
    # workflow therefore mounts a private Kaggle dataset containing only this
    # credential. Never print the token value.
    input_root = Path("/kaggle/input")
    if input_root.exists():
        candidates = list(input_root.rglob("hf_token.txt"))
        for candidate in candidates:
            token = candidate.read_text(encoding="utf-8").strip()
            if token:
                print(f"HF token loaded from private Kaggle input: {candidate.parent.name}")
                return token

    raise RuntimeError("HF_TOKEN is unavailable in Kaggle secrets and private CI secret dataset.")


def main():
    hf_token = load_hf_token()

    os.environ["HF_TOKEN"] = hf_token
    os.environ["HUGGING_FACE_HUB_TOKEN"] = hf_token
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    if REPO_DIR.exists():
        run(["git", "-C", str(REPO_DIR), "pull", "--ff-only"])
    else:
        run(["git", "clone", "--depth", "1", REPO_URL, str(REPO_DIR)])

    run(["bash", "scripts/bootstrap_kaggle.sh"], cwd=REPO_DIR)

    python_bin = SA3_DIR / ".venv" / "bin" / "python"
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

    print("Reference test complete. Output: /kaggle/working/output")


if __name__ == "__main__":
    main()
