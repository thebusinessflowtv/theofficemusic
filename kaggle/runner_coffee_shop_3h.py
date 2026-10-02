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
TRACK_COUNT = 15
FINAL_TRACK_COUNT = 6
SHARD_INDEX = 1
TRACK_DURATION_SECONDS = 120
MIN_FINAL_TRACK_SECONDS = 300
BATCH_PREFIX = "peter-lofi-coffee-shop"
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
    root = Path("/kaggle/input")
    if root.exists():
        for candidate in root.rglob("hf_token.txt"):
            token = candidate.read_text(encoding="utf-8").strip()
            if token:
                return token
    raise RuntimeError("HF_TOKEN is unavailable")


def prepare_profile() -> Path:
    profile = {
        "channel": {
            "name": "Peter Lofi",
            "concept": "Coffee Shop — cozy lofi for work, study, reading and relaxed focus"
        },
        "generation": {
            "model": "small-music",
            "tracks_per_batch": TRACK_COUNT,
            "track_duration_seconds": TRACK_DURATION_SECONDS,
            "steps": 8,
            "cfg_scale": 1.0,
            "chunked_decode": True,
            "pure_text_to_audio": True
        },
        "music_dna": {
            "instrumental_only": True,
            "energy": 4,
            "bpm_min": 76,
            "bpm_max": 88,
            "listening_context": "cozy cafe work, studying, journaling, writing, relaxed reading and long focus sessions",
            "mood": ["cozy", "warm", "gentle", "creative", "focused", "relaxed"],
            "style_pool": [
                "cozy coffee-shop lofi with warm jazz-influenced electric keys and a clean modern mix",
                "mellow cafe lofi with soft boom-bap-inspired drums, warm chords and unobtrusive groove",
                "warm study lofi with Rhodes-style keys, mellow bass and restrained melodic motifs",
                "quiet city cafe lofi with gentle electric piano, soft drums and subtle atmospheric pads",
                "premium coffeehouse lofi with warm electric keys, muted guitar-like plucks and polished stereo production"
            ],
            "preferred_instruments": [
                "soft upright-like bass",
                "gentle brushed electronic drums",
                "soft restrained hi-hats with relaxed groove",
                "warm Rhodes-style electric keys",
                "soft atmospheric pads",
                "muted guitar-like plucks",
                "subtle harmonic textures with no sampled cafe ambience"
            ],
            "avoid_instruments": [
                "cinematic orchestra",
                "epic trailer brass",
                "aggressive dubstep bass",
                "hard techno drums",
                "festival EDM supersaws",
                "distorted guitars",
                "trap percussion"
            ],
            "percussion": "gentle brushed-style electronic groove, soft and steady, never aggressive or distracting",
            "bass": "warm mellow bassline, rounded and supportive, comfortable for long listening",
            "melody_density": "low to medium-low, warm chord movement and short gentle motifs, never overly busy",
            "brightness": "soft warm high end with rich mids and controlled low end",
            "arrangement": "continuous coffee-shop-friendly arrangement with smooth evolution, restrained transitions, no huge drops, no abrupt genre changes and no long silent sections",
            "production": "premium cozy coffeehouse lofi feel, clean stereo image, warm depth, gentle bass, instrumental and suitable for hours of work or study",
            "title_left": ["Corner", "Warm", "Sunday", "Window", "Roasted", "Quiet", "Morning", "Golden", "Cozy", "City", "Soft", "Cafe"],
            "title_right": ["Coffee", "Table", "Cup", "Notebook", "Cafe", "Morning", "Window", "Pages", "Break", "Corner", "Blend", "Hours"]
        },
        "negative_prompt": [
            "poor quality", "distortion", "clipping", "harsh treble", "festival EDM", "hard techno",
            "aggressive dubstep", "trap", "dramatic cinematic trailer music", "intelligible vocals", "spoken words",
            "rap", "choir", "crowd noise", "literal cafe chatter", "espresso machine sounds", "street noise"
        ],
        "output": {
            "format": "wav",
            "sample_rate": 44100,
            "directory": "/kaggle/working/output",
            "write_manifest": True
        }
    }
    runtime = REPO_DIR / "config" / "runtime_gaming_3h_profile.json"
    runtime.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    return runtime


def request_seed(request_id: str) -> int:
    if not request_id or request_id == "UNSET":
        raise RuntimeError("REQUEST_ID was not injected")
    digest = hashlib.sha256(f"{request_id}:coffee-shop-3h:small-music".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 2_000_000_000 + 1


def clean_title(path: Path) -> str:
    stem = re.sub(r"^\d+[\-_ ]*", "", path.stem).strip("-_ ")
    return stem or "coffee-session"


def consolidate_to_five_minute_tracks(raw_wavs):
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

    if not first_params:
        raise RuntimeError("No source audio generated")
    channels, sampwidth, framerate, comptype = first_params
    if comptype != "NONE":
        raise RuntimeError("Expected PCM WAV")

    target_frames = int(framerate * MIN_FINAL_TRACK_SECONDS)
    expected_final = total_frames // target_frames
    if expected_final != FINAL_TRACK_COUNT:
        raise RuntimeError(f"Expected exactly {FINAL_TRACK_COUNT} final 5-minute tracks, calculated {expected_final}")

    shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    src_idx = 0
    src_wave = wave.open(str(metadata[src_idx][0]), "rb")
    src_remaining = src_wave.getnframes()

    def advance_source():
        nonlocal src_idx, src_wave, src_remaining
        src_wave.close()
        src_idx += 1
        if src_idx >= len(metadata):
            raise RuntimeError("Ran out of fresh source audio")
        src_wave = wave.open(str(metadata[src_idx][0]), "rb")
        src_remaining = src_wave.getnframes()

    final_files = []
    try:
        for out_idx in range(1, FINAL_TRACK_COUNT + 1):
            while src_remaining <= 0:
                advance_source()
            source_title = clean_title(metadata[src_idx][0])
            out_path = OUTPUT_DIR / f"s{SHARD_INDEX}-{out_idx:02d}-{source_title}.wav"
            with wave.open(str(out_path), "wb") as out:
                out.setnchannels(channels)
                out.setsampwidth(sampwidth)
                out.setframerate(framerate)
                frames_left = target_frames
                while frames_left > 0:
                    if src_remaining <= 0:
                        advance_source()
                    take = min(frames_left, src_remaining)
                    data = src_wave.readframes(take)
                    if not data:
                        src_remaining = 0
                        continue
                    frame_width = channels * sampwidth
                    frames_read = len(data) // frame_width
                    if frames_read <= 0:
                        raise RuntimeError("Zero-frame PCM read")
                    out.writeframes(data)
                    frames_left -= frames_read
                    src_remaining -= frames_read
            final_files.append(out_path)
            print(f"Final coffee track {out_idx:02d}: {out_path.name} (300s)")
    finally:
        try:
            src_wave.close()
        except Exception:
            pass

    return final_files


def main():
    token = load_hf_token()
    os.environ["HF_TOKEN"] = token
    os.environ["HUGGING_FACE_HUB_TOKEN"] = token
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["SA3_TARGET_MODEL"] = "small-music"
    os.environ["SA3_PREFER_CUDA"] = "1"

    for p in (REPO_DIR, SA3_DIR, OUTPUT_DIR, RAW_DIR):
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)

    run(["git", "clone", "--depth", "1", REPO_URL, str(REPO_DIR)])
    profile = prepare_profile()
    run(["bash", "scripts/bootstrap_kaggle.sh"], cwd=REPO_DIR)

    python_bin = SA3_DIR / ".venv" / "bin" / "python"
    if not python_bin.exists():
        raise RuntimeError("Stable Audio runtime not found")

    seed = request_seed(REQUEST_ID)
    batch_name = BATCH_PREFIX + "-" + REQUEST_ID[:8] + "-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_DIR / "src")

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

    final_wavs = consolidate_to_five_minute_tracks(moved)
    if len(final_wavs) != FINAL_TRACK_COUNT:
        raise RuntimeError(f"Expected {FINAL_TRACK_COUNT} final tracks, got {len(final_wavs)}")

    (OUTPUT_DIR / "request_id.txt").write_text(REQUEST_ID + "\n", encoding="utf-8")
    (OUTPUT_DIR / "generation_request.json").write_text(json.dumps({
        "request_id": REQUEST_ID,
        "series_key": "coffee-shop-3h",
        "model": "small-music",
        "master_seed": seed,
        "raw_track_count": TRACK_COUNT,
        "raw_track_duration_seconds": TRACK_DURATION_SECONDS,
        "final_track_count": len(final_wavs),
        "final_track_duration_seconds": 300,
        "shard_index": SHARD_INDEX,
        "total_duration_seconds": FINAL_TRACK_COUNT * 300,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")

    shutil.rmtree(RAW_DIR, ignore_errors=True)
    shutil.rmtree(SA3_DIR, ignore_errors=True)
    shutil.rmtree(REPO_DIR, ignore_errors=True)
    print(f"Coffee Shop shard {SHARD_INDEX} complete: {len(final_wavs)} fresh 5-minute tracks / 30 minutes")


if __name__ == "__main__":
    main()
