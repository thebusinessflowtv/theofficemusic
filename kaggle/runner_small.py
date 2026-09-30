import hashlib
import json
import os
import re
import shutil
import subprocess
import wave
from datetime import datetime, timezone
from pathlib import Path

from kaggle_secrets import UserSecretsClient

REPO_URL = "https://github.com/thebusinessflowtv/theofficemusic.git"
REPO_DIR = Path("/kaggle/working/theofficemusic")
SA3_DIR = Path("/kaggle/working/stable-audio-3")
OUTPUT_DIR = Path("/kaggle/working/output")
RAW_DIR = Path("/kaggle/working/output-raw")
TRACK_COUNT = 30
TRACK_DURATION_SECONDS = 120
MIN_FINAL_TRACK_SECONDS = 300
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


def clean_title(path: Path) -> str:
    stem = re.sub(r"^\d+[\-_ ]*", "", path.stem).strip("-_ ")
    return stem or "office-session"


def consolidate_to_long_tracks(raw_wavs):
    """Turn fresh <=120s generations into fresh songs of >=5 minutes without looping/reuse."""
    if not raw_wavs:
        raise RuntimeError("No raw WAVs available for consolidation")

    metadata = []
    first_params = None
    total_frames = 0
    for p in raw_wavs:
        with wave.open(str(p), "rb") as w:
            params = (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getcomptype())
            if first_params is None:
                first_params = params
            elif params != first_params:
                raise RuntimeError(f"WAV format mismatch in {p.name}")
            frames = w.getnframes()
            metadata.append((p, frames))
            total_frames += frames

    channels, sampwidth, framerate, comptype = first_params
    if comptype != "NONE":
        raise RuntimeError("Expected uncompressed PCM WAV output")

    min_frames = int(framerate * MIN_FINAL_TRACK_SECONDS)
    if total_frames < min_frames:
        raise RuntimeError(
            f"Fresh fallback audio totals only {total_frames / framerate:.1f}s; "
            f"at least {MIN_FINAL_TRACK_SECONDS}s is required for one song"
        )

    # Use as many final songs as possible while guaranteeing every one is >= 5 min.
    final_count = max(1, total_frames // min_frames)
    base_frames = total_frames // final_count
    extra_frames = total_frames % final_count
    target_frames = [base_frames + (1 if i < extra_frames else 0) for i in range(final_count)]

    shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    src_idx = 0
    src_wave = wave.open(str(metadata[src_idx][0]), "rb")
    src_remaining = src_wave.getnframes()
    final_files = []

    def advance_source():
        nonlocal src_idx, src_wave, src_remaining
        try:
            src_wave.close()
        except Exception:
            pass
        src_idx += 1
        if src_idx >= len(metadata):
            raise RuntimeError("Ran out of fresh source audio")
        src_wave = wave.open(str(metadata[src_idx][0]), "rb")
        src_remaining = src_wave.getnframes()

    try:
        for out_idx, needed in enumerate(target_frames, start=1):
            # A previous final song can end exactly on a source-file boundary.
            # Advance before deriving the next title or trying to read zero frames.
            while src_remaining <= 0:
                advance_source()

            source_title = clean_title(metadata[src_idx][0])
            out_path = OUTPUT_DIR / f"{out_idx:02d}-{source_title}.wav"
            with wave.open(str(out_path), "wb") as out:
                out.setnchannels(channels)
                out.setsampwidth(sampwidth)
                out.setframerate(framerate)
                frames_left = needed

                while frames_left > 0:
                    if src_remaining <= 0:
                        advance_source()

                    take = min(frames_left, src_remaining)
                    if take <= 0:
                        raise RuntimeError("Invalid zero-frame read while building long songs")

                    data = src_wave.readframes(take)
                    if not data:
                        # Defensive recovery for a WAV that reports frames but reaches EOF early.
                        src_remaining = 0
                        continue

                    frame_width = channels * sampwidth
                    if len(data) % frame_width != 0:
                        raise RuntimeError("Corrupt PCM frame alignment while building long songs")
                    frames_read = len(data) // frame_width
                    if frames_read <= 0:
                        raise RuntimeError("Unexpected zero-frame PCM block while building long songs")

                    out.writeframes(data)
                    frames_left -= frames_read
                    src_remaining -= frames_read

            duration = needed / framerate
            if duration < MIN_FINAL_TRACK_SECONDS:
                raise RuntimeError(f"Generated final song shorter than 5 minutes: {out_path.name}")
            final_files.append(out_path)
            print(f"Final song {out_idx:02d}: {out_path.name} ({duration:.1f}s)")
    finally:
        try:
            src_wave.close()
        except Exception:
            pass

    if len(final_files) != final_count:
        raise RuntimeError(f"Expected {final_count} consolidated songs, produced {len(final_files)}")

    return final_files


def main():
    token = load_hf_token()
    os.environ["HF_TOKEN"] = token
    os.environ["HUGGING_FACE_HUB_TOKEN"] = token
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["SA3_TARGET_MODEL"] = "small-music"

    for p in (REPO_DIR, SA3_DIR, OUTPUT_DIR, RAW_DIR):
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

    raw_wavs = sorted(OUTPUT_DIR.glob("*.wav"))
    if len(raw_wavs) != TRACK_COUNT:
        raise RuntimeError(f"Expected {TRACK_COUNT} raw WAV files, generated {len(raw_wavs)}")

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    moved = []
    for p in raw_wavs:
        dest = RAW_DIR / p.name
        shutil.move(str(p), str(dest))
        moved.append(dest)

    final_wavs = consolidate_to_long_tracks(moved)

    (OUTPUT_DIR / "request_id.txt").write_text(REQUEST_ID + "\n", encoding="utf-8")
    (OUTPUT_DIR / "generation_request.json").write_text(json.dumps({
        "request_id": REQUEST_ID,
        "model": "small-music",
        "master_seed": seed,
        "raw_track_count": TRACK_COUNT,
        "raw_track_duration_seconds": TRACK_DURATION_SECONDS,
        "final_track_count": len(final_wavs),
        "minimum_final_track_seconds": MIN_FINAL_TRACK_SECONDS,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")

    shutil.rmtree(RAW_DIR, ignore_errors=True)
    shutil.rmtree(SA3_DIR, ignore_errors=True)
    shutil.rmtree(REPO_DIR, ignore_errors=True)
    print(
        f"Fallback generation complete: {len(final_wavs)} fresh songs, "
        f"each >= {MIN_FINAL_TRACK_SECONDS}s, for request {REQUEST_ID}"
    )


if __name__ == "__main__":
    main()
