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
BATCH_PREFIX = "peter-lofi"
REQUEST_ID = "UNSET"
SERIES_KEY = ""


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


def deep_merge(base, override):
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for key, value in override.items():
            out[key] = deep_merge(out.get(key), value) if key in out else value
        return out
    return override


def prepare_profile() -> Path:
    if not SERIES_KEY:
        return REPO_DIR / "config" / "channel_profile.yaml"
    plan_path = REPO_DIR / "config" / "peter_lofi_series.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    item = next((x for x in plan.get("series", []) if x.get("key") == SERIES_KEY), None)
    if not item:
        raise RuntimeError(f"Unknown Peter Lofi series key: {SERIES_KEY}")
    profile = deep_merge(plan["defaults"], {"music_dna": item.get("music_dna", {})})
    profile["channel"] = dict(profile.get("channel", {}))
    profile["channel"]["name"] = "Peter Lofi"
    profile["channel"]["concept"] = item["name"]
    runtime = REPO_DIR / "config" / "runtime_series_profile.json"
    runtime.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Series profile selected: {item['name']} ({SERIES_KEY})")
    return runtime


def read_target_model(profile: Path) -> str:
    text = profile.read_text(encoding="utf-8")
    if profile.suffix.lower() == ".json":
        return str(json.loads(text)["generation"]["model"])
    match = re.search(r"(?m)^\s*model:\s*[\"']?([^\"'\s#]+)", text)
    if not match:
        raise RuntimeError(f"Could not determine generation.model from {profile.name}")
    return match.group(1)


def cleanup_working_tree() -> None:
    for path in (SA3_DIR, REPO_DIR):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


def request_seed(request_id: str, model: str) -> int:
    if not request_id or request_id == "UNSET":
        raise RuntimeError("REQUEST_ID was not injected by the production workflow")
    digest = hashlib.sha256(f"{request_id}:{SERIES_KEY}:{model}:medium".encode("utf-8")).digest()
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

    profile_path = prepare_profile()
    target_model = read_target_model(profile_path)
    os.environ["SA3_TARGET_MODEL"] = target_model
    seed = request_seed(REQUEST_ID, target_model)
    print(f"Configured Stable Audio target model: {target_model}")
    print(f"Request ID: {REQUEST_ID}")
    print(f"Series key: {SERIES_KEY or 'default'}")
    print(f"Request-bound master seed: {seed}")
    print(f"Track count: {TRACK_COUNT}")
    print(f"Track duration: {TRACK_DURATION_SECONDS}s")

    run(["bash", "scripts/bootstrap_kaggle.sh"], cwd=REPO_DIR)

    python_bin = SA3_DIR / ".venv" / "bin" / "python"
    if not python_bin.exists():
        raise RuntimeError(f"Stable Audio Python runtime not found at {python_bin}")

    batch_name = BATCH_PREFIX + "-" + (SERIES_KEY or "default") + "-" + REQUEST_ID[:8] + "-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_DIR / "src")

    run(
        [
            str(python_bin),
            str(REPO_DIR / "src" / "generate_tracks.py"),
            "--config",
            str(profile_path),
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
        raise RuntimeError(f"Expected {TRACK_COUNT} WAV files but generated {len(wavs)}.")

    (OUTPUT_DIR / "request_id.txt").write_text(REQUEST_ID + "\n", encoding="utf-8")
    (OUTPUT_DIR / "generation_request.json").write_text(json.dumps({
        "request_id": REQUEST_ID,
        "series_key": SERIES_KEY,
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
