#!/usr/bin/env python3
import argparse
import hashlib
import json
import random
import time
from pathlib import Path

import requests

REFS = Path("control/gaming-reference-production/references.json")

TITLE_LEFT = [
    "Neon", "Midnight", "Static", "Nova", "Pixel", "Velocity", "Signal", "Electric",
    "Night", "Hyper", "Echo", "Digital", "Zero", "Pulse", "After", "Arcade",
]
TITLE_RIGHT = [
    "Drift", "Run", "Circuit", "Horizon", "Rush", "Lobby", "Drive", "Phase",
    "Flow", "Skyline", "Vector", "Mode", "Current", "Grid", "Session", "Orbit",
]


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def find_features(obj):
    keys = {
        "acousticness", "danceability", "energy", "instrumentalness", "liveness",
        "loudness", "speechiness", "tempo", "valence", "key", "mode",
    }
    if isinstance(obj, dict):
        if sum(1 for k in keys if k in obj) >= 5:
            return obj
        for value in obj.values():
            got = find_features(value)
            if got:
                return got
    elif isinstance(obj, list):
        for value in obj:
            got = find_features(value)
            if got:
                return got
    return None


def get_reccobeats_features(spotify_id):
    urls = [
        f"https://api.reccobeats.com/v1/track/{spotify_id}/audio-features",
        f"https://api.reccobeats.com/v1/audio-features?ids={spotify_id}",
    ]
    s = requests.Session()
    s.headers.update({"Accept": "application/json", "User-Agent": "MediaForge-Peter-Lofi/1.0"})
    errors = []
    for url in urls:
        for attempt in range(3):
            try:
                r = s.get(url, timeout=25)
                if r.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                if r.status_code == 404:
                    errors.append(f"{url}:404")
                    break
                r.raise_for_status()
                data = r.json()
                features = find_features(data)
                if features:
                    clean = {}
                    for key in (
                        "acousticness", "danceability", "energy", "instrumentalness",
                        "liveness", "loudness", "speechiness", "tempo", "valence",
                        "key", "mode",
                    ):
                        if key in features:
                            try:
                                clean[key] = float(features[key])
                            except Exception:
                                pass
                    if len(clean) >= 5:
                        return clean, "reccobeats"
            except Exception as exc:
                errors.append(f"{url}:{type(exc).__name__}")
                time.sleep(1 + attempt)
    return None, "unavailable:" + ",".join(errors[-3:])


def fallback_features(reference):
    seed = int.from_bytes(
        hashlib.sha256(
            f"{reference.get('spotify_id')}:{reference.get('title')}:{reference.get('artists')}".encode()
        ).digest()[:8],
        "big",
    )
    rng = random.Random(seed)
    return {
        "tempo": rng.uniform(92, 148),
        "energy": rng.uniform(0.52, 0.88),
        "danceability": rng.uniform(0.45, 0.78),
        "valence": rng.uniform(0.28, 0.78),
        "acousticness": rng.uniform(0.02, 0.25),
        "instrumentalness": rng.uniform(0.0, 0.35),
        "speechiness": rng.uniform(0.02, 0.12),
        "liveness": rng.uniform(0.05, 0.22),
        "loudness": rng.uniform(-10.5, -5.5),
    }


def describe(features, reference, attempt):
    sid = str(reference["spotify_id"])
    seed = int.from_bytes(
        hashlib.sha256(f"{sid}:gaming-reference:{attempt}".encode()).digest()[:8], "big"
    )
    rng = random.Random(seed)

    raw_tempo = float(features.get("tempo", 110.0))
    if raw_tempo > 176:
        raw_tempo /= 2.0
    bpm = int(round(clamp(raw_tempo + rng.choice([-7, -5, -3, 3, 5, 7]), 82, 156)))

    energy = clamp(float(features.get("energy", 0.65)), 0, 1)
    dance = clamp(float(features.get("danceability", 0.6)), 0, 1)
    valence = clamp(float(features.get("valence", 0.5)), 0, 1)
    acoustic = clamp(float(features.get("acousticness", 0.1)), 0, 1)
    speech = clamp(float(features.get("speechiness", 0.05)), 0, 1)

    if energy >= 0.78 and dance >= 0.62:
        style = "energetic melodic electronic gaming music with future-bass influence and a polished modern pulse"
    elif energy >= 0.72:
        style = "melodic bass-driven electronic gaming music with atmospheric synth layers and controlled impact"
    elif dance >= 0.68:
        style = "rhythmic electronic gaming chill with clean dance-pop movement and modern synth production"
    elif energy <= 0.45:
        style = "atmospheric chill electronic gaming music with spacious textures and restrained rhythmic motion"
    else:
        style = "modern melodic electronic gaming music with a focused groove and immersive synth atmosphere"

    if valence < 0.32:
        mood = ["moody", "emotional", "nocturnal", "focused"]
    elif valence < 0.58:
        mood = ["immersive", "confident", "atmospheric", "focused"]
    else:
        mood = ["uplifting", "bright", "energizing", "flow-state"]

    if energy >= 0.8:
        intensity = "high but controlled energy, strong forward motion, impactful transitions without festival-scale drops"
    elif energy >= 0.58:
        intensity = "medium-high energy, steady momentum, satisfying rises and releases without becoming aggressive"
    else:
        intensity = "restrained energy, smooth movement, gentle builds and subtle releases for long listening"

    if dance >= 0.7:
        groove = "tight danceable electronic groove, clear pulse, crisp syncopation and smooth continuous momentum"
    elif dance >= 0.5:
        groove = "steady modern electronic groove with subtle syncopation and a comfortable gaming pulse"
    else:
        groove = "loose atmospheric electronic groove with restrained drums and spacious rhythmic breathing room"

    if acoustic >= 0.38:
        instruments = [
            "warm electric piano or soft piano accents",
            "clean electronic drums",
            "rounded synth bass",
            "wide atmospheric pads",
            "tasteful organic plucks blended with synth textures",
        ]
    else:
        instruments = [
            "layered modern synthesizers",
            "clean punchy electronic drums",
            "rounded controlled synth bass",
            "wide atmospheric pads",
            "short melodic plucks and evolving arpeggios",
        ]

    if speech > 0.25:
        vocal_guard = "absolutely no speech, rap, vocal chops or intelligible human voice"
    else:
        vocal_guard = "instrumental only, no intelligible singing, spoken words or vocal hooks"

    title_rng = random.Random(seed ^ 0xA5A5A5A5)
    title = f"{title_rng.choice(TITLE_LEFT)} {title_rng.choice(TITLE_RIGHT)}"
    if attempt > 1:
        title = f"{title} {attempt}"

    return {
        "target_bpm": bpm,
        "style": style,
        "mood": mood,
        "intensity": intensity,
        "groove": groove,
        "instruments": instruments,
        "vocal_guard": vocal_guard,
        "title": title,
        "feature_summary": {
            "reference_tempo": round(raw_tempo, 3),
            "generated_target_bpm": bpm,
            "energy": round(energy, 4),
            "danceability": round(dance, 4),
            "valence": round(valence, 4),
            "acousticness": round(acoustic, 4),
            "speechiness": round(speech, 4),
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, required=True)
    ap.add_argument("--attempt", type=int, default=1)
    ap.add_argument("--out", default="build/reference_profile.json")
    args = ap.parse_args()

    refs = json.loads(REFS.read_text(encoding="utf-8"))
    tracks = refs.get("tracks") or []
    if not 1 <= args.index <= len(tracks):
        raise SystemExit(f"Invalid reference index {args.index}; available 1..{len(tracks)}")
    reference = tracks[args.index - 1]

    features, source = get_reccobeats_features(reference["spotify_id"])
    if not features:
        features = fallback_features(reference)
        source = "deterministic_metadata_fallback"

    d = describe(features, reference, args.attempt)
    bpm = d["target_bpm"]

    profile = {
        "channel": {
            "name": "Peter Lofi",
            "concept": "Gaming Reference 215 — original instrumental gaming catalog",
        },
        "generation": {
            "model": "medium",
            "tracks_per_batch": 1,
            "track_duration_seconds": 300,
            "steps": 8,
            "cfg_scale": 1.0,
            "chunked_decode": True,
            "pure_text_to_audio": True,
        },
        "music_dna": {
            "instrumental_only": True,
            "energy": round(float(features.get("energy", 0.65)) * 10),
            "bpm_min": max(78, bpm - 2),
            "bpm_max": min(160, bpm + 2),
            "listening_context": "gaming streams, long play sessions, exploration, competitive matches and focused gameplay",
            "mood": d["mood"],
            "style_pool": [d["style"]],
            "preferred_instruments": d["instruments"],
            "avoid_instruments": [
                "recognizable melodies from existing songs",
                "direct imitation of any named artist or recording",
                "copyrighted samples",
                "festival EDM supersaws",
                "harsh distorted bass",
                "cinematic trailer orchestra",
            ],
            "groove": d["groove"],
            "percussion": "clean modern electronic drums with controlled transients, satisfying movement and no harsh over-compression",
            "bass": "rounded controlled electronic bass, deep and present but comfortable for long gaming sessions",
            "melody_density": "medium, with one original memorable motif that evolves gradually and never quotes an existing hook",
            "brightness": "polished modern high end, warm mids and controlled low end with no harshness",
            "arrangement": (
                "five-minute coherent original instrumental composition with a clear intro, gradual development, "
                "musical contrast, smooth transitions and a satisfying ending; maintain a unified theme across the full track; "
                + d["intensity"]
            ),
            "production": (
                "premium release-ready stereo mix for streaming, detailed but not fatiguing, immersive width, "
                "clean low end, no clipping, no abrupt edits, no dead air"
            ),
            "title_left": [d["title"].split(" ", 1)[0]],
            "title_right": [d["title"].split(" ", 1)[1]],
        },
        "negative_prompt": [
            "poor quality", "distortion", "clipping", "harsh treble", "long silence",
            "abrupt cut", "recognizable copyrighted melody", "existing song hook",
            "artist imitation", "cover version", "remix", "sampled commercial recording",
            "intelligible vocals", "spoken words", "rap", "choir", "crowd noise",
        ],
        "output": {
            "format": "wav",
            "sample_rate": 44100,
            "directory": "/kaggle/working/output",
            "write_manifest": True,
        },
    }

    payload = {
        "reference_index": args.index,
        "attempt": args.attempt,
        "reference": reference,
        "reference_copyright_check": {
            "status": reference.get("copyright_status") or "presumed_copyrighted",
            "basis": reference.get("copyright_basis"),
            "generation_rule": (
                "Reference identity and audio are excluded from the model prompt. Only non-expressive numerical "
                "audio features plus broad genre-level descriptors are used."
            ),
        },
        "feature_source": source,
        "audio_features": features,
        "derived_traits": d,
        "profile": profile,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "index": args.index,
        "attempt": args.attempt,
        "reference": f"{reference.get('artists')} — {reference.get('title')}",
        "copyright": payload["reference_copyright_check"]["status"],
        "feature_source": source,
        "target_bpm": bpm,
        "style": d["style"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
